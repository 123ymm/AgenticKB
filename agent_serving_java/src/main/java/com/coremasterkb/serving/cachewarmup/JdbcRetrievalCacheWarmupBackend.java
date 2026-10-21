package com.coremasterkb.serving.cachewarmup;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/** Bounded read-only PostgreSQL operations; never changes database settings or schema. */
final class JdbcRetrievalCacheWarmupBackend implements RetrievalCacheWarmupService.Backend {
    private static final int WARMUP_TIMEOUT_SECONDS = 900;
    static final List<String> TARGET_NAMES = List.of(
            "public.asset_retrieval_embeddings_v2", "public.asset_retrieval_units_v2",
            "public.asset_retrieval_units_v2_pkey", "public.idx_are_v2_vec_hnsw");
    private static final String INSPECT_SQL = """
            WITH base_targets(name, position) AS (VALUES
              ('public.asset_retrieval_embeddings_v2',1),('public.asset_retrieval_units_v2',2),
              ('public.asset_retrieval_units_v2_pkey',3),('public.idx_are_v2_vec_hnsw',4)),
            toast_heaps AS (
              SELECT parent.reltoastrelid AS oid, base.position
              FROM base_targets base JOIN pg_class parent ON parent.oid=to_regclass(base.name)
              WHERE base.position IN (1,2) AND parent.reltoastrelid<>0
            ), toast_targets AS (
              SELECT format('%I.%I',n.nspname,c.relname) AS name, toast.position*10 AS position
              FROM toast_heaps toast JOIN pg_class c ON c.oid=toast.oid
              JOIN pg_namespace n ON n.oid=c.relnamespace
              UNION ALL
              SELECT format('%I.%I',n.nspname,c.relname),toast.position*10+1
              FROM toast_heaps toast JOIN pg_index idx ON idx.indrelid=toast.oid AND idx.indisvalid AND idx.indisready
              JOIN pg_class c ON c.oid=idx.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            ), targets AS (
              SELECT name,position,'read'::text AS mode FROM toast_targets
              UNION ALL SELECT name,position,'buffer'::text FROM base_targets
            )
            SELECT current_setting('cmkb.retrieval_prewarm',true), pg_postmaster_start_time()::text,
                   concat_ws(':',current_database(),
                     (SELECT oid::text FROM pg_database WHERE datname=current_database()),
                     coalesce(inet_server_addr()::text,'local'),inet_server_port()),
                   pg_size_bytes(current_setting('shared_buffers')),current_setting('block_size')::bigint,
                   (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
                    WHERE e.extname='pg_prewarm'),
                   targets.name,pg_relation_filenode(c.oid)::text,pg_relation_size(c.oid),
                   CASE WHEN targets.mode='read' THEN c.relkind='t' OR (c.relkind='i' AND i.indisvalid AND i.indisready)
                        WHEN targets.position=4 THEN c.relkind='i' AND i.indisvalid AND i.indisready AND a.amname='hnsw'
                        WHEN targets.position=3 THEN c.relkind='i' AND i.indisvalid AND i.indisready AND i.indisprimary
                        ELSE c.relkind='r' END, targets.mode
            FROM targets LEFT JOIN pg_class c ON c.oid=to_regclass(targets.name)
            LEFT JOIN pg_index i ON i.indexrelid=c.oid LEFT JOIN pg_am a ON a.oid=c.relam
            ORDER BY CASE targets.mode WHEN 'read' THEN 0 ELSE 1 END,targets.position,targets.name
            """;

    @Override
    public RetrievalCacheWarmupService.Probe inspect(DataSource source) throws SQLException {
        try (Connection connection = source.getConnection()) {
            if (!"PostgreSQL".equalsIgnoreCase(connection.getMetaData().getDatabaseProductName())) {
                return RetrievalCacheWarmupService.Probe.off();
            }
            try (Statement statement = connection.createStatement()) {
                statement.setQueryTimeout(3);
                try (ResultSet metadata = statement.executeQuery(INSPECT_SQL)) {
                    if (!metadata.next()) throw new SQLException("prewarm_metadata_missing");
                    if (!"on".equalsIgnoreCase(metadata.getString(1))) {
                        return RetrievalCacheWarmupService.Probe.off();
                    }
                    String epoch = metadata.getString(2);
                    StringBuilder signature = new StringBuilder(metadata.getString(3));
                    long shared = metadata.getLong(4);
                    long blockSize = metadata.getLong(5);
                    String schema = metadata.getString(6);
                    if (schema == null || blockSize <= 0) throw new SQLException("prewarm_extension_missing");
                    List<RetrievalCacheWarmupService.Target> targets = new ArrayList<>();
                    long bufferBytes = 0;
                    long readBytes = 0;
                    do {
                        String name = metadata.getString(7);
                        String filenode = metadata.getString(8);
                        long bytes = metadata.getLong(9);
                        String mode = metadata.getString(11);
                        if (!metadata.getBoolean(10) || filenode == null || bytes < 0 || bytes % blockSize != 0) {
                            throw new SQLException("prewarm_relation_invalid");
                        }
                        signature.append(':').append(name).append('=').append(filenode);
                        targets.add(new RetrievalCacheWarmupService.Target(name, bytes / blockSize, mode));
                        if (mode.equals("buffer")) bufferBytes = Math.addExact(bufferBytes, bytes);
                        else readBytes = Math.addExact(readBytes, bytes);
                    } while (metadata.next());
                    List<String> bufferNames = targets.stream().filter(target -> target.mode().equals("buffer"))
                            .map(RetrievalCacheWarmupService.Target::name).toList();
                    if (!bufferNames.equals(TARGET_NAMES)) throw new SQLException("prewarm_relation_missing");
                    checkCapacity(shared, bufferBytes);
                    if (readBytes > shared) throw new IllegalStateException("prewarm_read_capacity_exceeded");
                    return new RetrievalCacheWarmupService.Probe(true, epoch, signature.toString(), shared, schema, targets);
                }
            }
        }
    }

    static void checkCapacity(long shared, long bytes) {
        if (shared <= 0 || bytes < 0 || bytes > shared / 4 * 3) {
            throw new IllegalStateException("prewarm_capacity_exceeded");
        }
    }

    @Override
    public void warm(DataSource source, RetrievalCacheWarmupService.Probe expected) throws SQLException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(WARMUP_TIMEOUT_SECONDS);
        String schema = expected.extensionSchema().replace("\"", "\"\"");
        String sql = "SELECT \"" + schema + "\".pg_prewarm(?::regclass,?,'main',0,?)";
        try (Connection connection = source.getConnection()) {
            connection.setReadOnly(true);
            connection.setAutoCommit(false);
            try {
                try (Statement setup = connection.createStatement()) {
                    setup.setQueryTimeout(3);
                    setup.execute("SET LOCAL statement_timeout='900s'");
                    setup.execute("SET LOCAL lock_timeout='2s'");
                }
                try (PreparedStatement statement = connection.prepareStatement(sql)) {
                    for (RetrievalCacheWarmupService.Target target : expected.targets()) {
                        if (target.blocks() == 0) continue;
                        long seconds = TimeUnit.NANOSECONDS.toSeconds(deadline - System.nanoTime());
                        if (seconds <= 0) throw new SQLException("prewarm_timeout");
                        statement.setQueryTimeout((int) seconds);
                        statement.setString(1, target.name());
                        statement.setString(2, target.mode());
                        statement.setLong(3, target.blocks() - 1);
                        try (ResultSet result = statement.executeQuery()) {
                            if (!result.next() || result.getLong(1) != target.blocks()) {
                                throw new SQLException("prewarm_block_count_mismatch");
                            }
                        }
                    }
                }
            } finally {
                // Reading pages changes caches only; end the read transaction and release its locks.
                connection.rollback();
            }
        }
    }
}
