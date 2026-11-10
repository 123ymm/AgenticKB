package com.coremasterkb.serving.cachewarmup;

import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class JdbcRetrievalCacheWarmupBackendTest {
    private static <T> T proxy(Class<T> type, InvocationHandler handler) {
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[]{type}, handler));
    }

    private static final class Database {
        String product = "PostgreSQL";
        String optin = "on";
        String schema = "public";
        boolean valid = true;
        boolean badCount;
        boolean readOnly;
        long shared = 8L << 30;
        long bytes = 8192;
        long blockSize = 8192;
        int relationRows = 4;
        boolean missingFilenode;
        boolean emptyWarmResult;
        boolean withToast;
        long toastBytes = 8192;
        int probes;
        int rollbacks;
        final List<String> warmed = new ArrayList<>();
        final List<String> modes = new ArrayList<>();
        final List<Integer> timeouts = new ArrayList<>();
        final List<String> commands = new ArrayList<>();

        DataSource source() {
            return proxy(DataSource.class, (p, method, args) -> {
                if (method.getName().equals("getConnection")) return connection();
                throw new UnsupportedOperationException(method.getName());
            });
        }

        Connection connection() {
            return proxy(Connection.class, (p, method, args) -> switch (method.getName()) {
                case "getMetaData" -> proxy(DatabaseMetaData.class, (m, call, a) -> product);
                case "createStatement" -> statement(null);
                case "prepareStatement" -> statement((String) args[0]);
                case "setReadOnly" -> { readOnly = (boolean) args[0]; yield null; }
                case "rollback" -> { rollbacks++; yield null; }
                case "setAutoCommit", "close" -> null;
                default -> throw new UnsupportedOperationException(method.getName());
            });
        }

        Statement statement(String sql) {
            Map<Integer, Object> bindings = new HashMap<>();
            Class<? extends Statement> type = sql == null ? Statement.class : PreparedStatement.class;
            return proxy(type, (p, method, args) -> switch (method.getName()) {
                case "setQueryTimeout" -> { timeouts.add((Integer) args[0]); yield null; }
                case "setString", "setLong" -> { bindings.put((Integer) args[0], args[1]); yield null; }
                case "execute" -> { commands.add((String) args[0]); yield false; }
                case "executeQuery" -> {
                    if (sql == null) {
                        probes++;
                        List<Object[]> rows = new ArrayList<>();
                        if (withToast) {
                            for (String name : List.of("pg_toast.pg_toast_10000", "pg_toast.pg_toast_10000_index",
                                    "pg_toast.pg_toast_20000", "pg_toast.pg_toast_20000_index")) {
                                rows.add(new Object[]{optin, "epoch-1", "db:42:server:5432", shared,
                                        blockSize, schema, name, name + "-node", toastBytes, valid, "read"});
                            }
                        }
                        int filenode = 100;
                        for (String target : JdbcRetrievalCacheWarmupBackend.TARGET_NAMES.subList(0, relationRows)) {
                            rows.add(new Object[]{optin, "epoch-1", "db:42:server:5432", shared,
                                    blockSize, schema, target, missingFilenode ? null : String.valueOf(filenode++), bytes, valid, "buffer"});
                        }
                        yield results(rows);
                    }
                    assertThat(sql).contains("pg_prewarm(?::regclass,?,'main',0,?)");
                    warmed.add((String) bindings.get(1));
                    modes.add((String) bindings.get(2));
                    if (emptyWarmResult) yield results(List.of());
                    long count = (Long) bindings.get(3) + 1 + (badCount ? 1 : 0);
                    yield results(java.util.Collections.singletonList(new Object[]{count}));
                }
                case "close" -> null;
                default -> throw new UnsupportedOperationException(method.getName());
            });
        }

        ResultSet results(List<Object[]> rows) {
            int[] position = {-1};
            return proxy(ResultSet.class, (p, method, args) -> switch (method.getName()) {
                case "next" -> ++position[0] < rows.size();
                case "getString" -> {
                    Object value = rows.get(position[0])[(Integer) args[0] - 1];
                    yield value == null ? null : value.toString();
                }
                case "getLong" -> ((Number) rows.get(position[0])[(Integer) args[0] - 1]).longValue();
                case "getBoolean" -> rows.get(position[0])[(Integer) args[0] - 1];
                case "close" -> null;
                default -> throw new UnsupportedOperationException(method.getName());
            });
        }
    }

    @Test
    void oneRoundTripInspectsDatabaseIdentityEpochAndEveryPhysicalRelation() throws Exception {
        Database database = new Database();
        var probe = new JdbcRetrievalCacheWarmupBackend().inspect(database.source());
        assertThat(probe.enabled()).isTrue();
        assertThat(probe.epoch()).isEqualTo("epoch-1");
        assertThat(probe.signature()).contains("db:42:server:5432", "idx_are_v2_vec_hnsw=103");
        assertThat(probe.targets()).extracting(RetrievalCacheWarmupService.Target::name)
                .containsExactlyElementsOf(JdbcRetrievalCacheWarmupBackend.TARGET_NAMES);
        assertThat(database.probes).isEqualTo(1);
    }

    @Test
    void warmUsesBoundedReadOnlyTransactionExactBlockRangesAndGraphLast() throws Exception {
        Database database = new Database();
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        DataSource source = database.source();
        backend.warm(source, backend.inspect(source));
        assertThat(database.warmed).containsExactlyElementsOf(JdbcRetrievalCacheWarmupBackend.TARGET_NAMES);
        assertThat(database.readOnly).isTrue();
        assertThat(database.rollbacks).isEqualTo(1);
        assertThat(database.timeouts).allMatch(seconds -> seconds > 0 && seconds <= 900);
        assertThat(database.timeouts).anyMatch(seconds -> seconds > 120);
        assertThat(database.commands).containsExactly(
                "SET LOCAL statement_timeout='900s'", "SET LOCAL lock_timeout='2s'");
    }

    @Test
    void unsetOptinAndNonPostgresDoNotRequireTablesOrExtension() throws Exception {
        Database database = new Database();
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        database.optin = null;
        database.schema = null;
        database.valid = false;
        assertThat(backend.inspect(database.source()).enabled()).isFalse();
        database.product = "H2";
        database.optin = "on";
        assertThat(backend.inspect(database.source()).enabled()).isFalse();
        assertThat(database.probes).isEqualTo(1);
        assertThat(database.warmed).isEmpty();
    }

    @Test
    void wrongBlockCountFailsAndReleasesTransaction() throws Exception {
        Database database = new Database();
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        DataSource source = database.source();
        var probe = backend.inspect(source);
        database.badCount = true;
        assertThatThrownBy(() -> backend.warm(source, probe)).isInstanceOf(SQLException.class)
                .hasMessage("prewarm_block_count_mismatch");
        assertThat(database.rollbacks).isEqualTo(1);
    }

    @Test
    void missingExtensionInvalidRelationsAndOverBudgetFailClosed() {
        Database database = new Database();
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        database.schema = null;
        assertThatThrownBy(() -> backend.inspect(database.source())).isInstanceOf(SQLException.class);
        database.schema = "public";
        database.valid = false;
        assertThatThrownBy(() -> backend.inspect(database.source())).isInstanceOf(SQLException.class);
        database.valid = true;
        database.bytes = 2L << 30;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_capacity_exceeded");
    }

    @Test
    void missingMetadataOrRelationRowsCannotPassInspection() {
        Database database = new Database();
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        database.relationRows = 0;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_metadata_missing");
        database.relationRows = 3;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_relation_missing");
    }

    @Test
    void malformedBlockMetadataAndUnknownCapacityFailClosed() {
        Database database = new Database();
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        database.blockSize = 0;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_extension_missing");
        database.blockSize = 8192;
        database.missingFilenode = true;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_relation_invalid");
        database.missingFilenode = false;
        database.bytes = -8192;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_relation_invalid");
        database.bytes = 1;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_relation_invalid");
        assertThatThrownBy(() -> JdbcRetrievalCacheWarmupBackend.checkCapacity(0, 0))
                .hasMessage("prewarm_capacity_exceeded");
        assertThatThrownBy(() -> JdbcRetrievalCacheWarmupBackend.checkCapacity(8L << 30, -1))
                .hasMessage("prewarm_capacity_exceeded");
    }

    @Test
    void zeroLengthRelationsNeedNoWarmCallAndMissingFunctionResultFails() throws Exception {
        Database database = new Database();
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        database.bytes = 0;
        backend.warm(database.source(), backend.inspect(database.source()));
        assertThat(database.warmed).isEmpty();
        database.bytes = 8192;
        database.emptyWarmResult = true;
        assertThatThrownBy(() -> backend.warm(database.source(), backend.inspect(database.source())))
                .hasMessage("prewarm_block_count_mismatch");
    }

    @Test
    void productionLifecycleDiscoversEnabledDomainsAndClosesOwnedThreads() throws Exception {
        Database database = new Database();
        DataSource source = database.source();
        var registry = new com.coremasterkb.serving.domainpack.DomainRegistry();
        registry.apply(new com.coremasterkb.serving.domainpack.ServingConfigSnapshot(Map.of(
                "enabled", new com.coremasterkb.serving.domainpack.ServingConfigSnapshot.DomainConfig("enabled", true, "prod", null),
                "disabled", new com.coremasterkb.serving.domainpack.ServingConfigSnapshot.DomainConfig("disabled", false, "prod", null))));
        var pools = new com.coremasterkb.serving.domainpack.DomainPoolManager(registry, source);
        var service = new RetrievalCacheWarmupService(source, pools, registry);
        try {
            service.start();
            service.start();
            long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(3);
            while (!service.readiness().ready() && System.nanoTime() < deadline) Thread.sleep(10);
            assertThat(service.readiness().ready()).isTrue();
            assertThat(service.readiness().databases()).containsOnlyKeys("default", "enabled");
            assertThat(database.warmed).hasSize(4);
        } finally {
            service.close();
            service.close();
            pools.shutdown();
        }
    }

    @Test
    void derivedToastRelationsUseOsReadsBeforeBufferTargetsAndAffectSignature() throws Exception {
        Database database = new Database();
        database.withToast = true;
        var backend = new JdbcRetrievalCacheWarmupBackend();
        var probe = backend.inspect(database.source());
        assertThat(probe.targets()).hasSize(8);
        assertThat(probe.targets().subList(0, 4)).allMatch(target -> target.mode().equals("read"));
        assertThat(probe.targets().subList(4, 8)).allMatch(target -> target.mode().equals("buffer"));
        assertThat(probe.signature()).contains("pg_toast.pg_toast_10000=pg_toast.pg_toast_10000-node");
        backend.warm(database.source(), probe);
        assertThat(database.modes).containsExactly("read", "read", "read", "read", "buffer", "buffer", "buffer", "buffer");
        assertThat(database.warmed.getLast()).isEqualTo("public.idx_are_v2_vec_hnsw");
        assertThat(new RetrievalCacheWarmupService.Target("public.test", 1).mode()).isEqualTo("buffer");
    }

    @Test
    void readBudgetIsSeparateFromBufferBudgetAndCannotExceedSharedCapacity() throws Exception {
        Database database = new Database();
        database.withToast = true;
        database.toastBytes = 2L << 30;
        var backend = new JdbcRetrievalCacheWarmupBackend();
        assertThat(backend.inspect(database.source()).enabled()).isTrue();
        database.toastBytes = 3L << 30;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_read_capacity_exceeded");
        database.toastBytes = 8192;
        database.bytes = 2L << 30;
        assertThatThrownBy(() -> backend.inspect(database.source())).hasMessage("prewarm_capacity_exceeded");
    }
}
