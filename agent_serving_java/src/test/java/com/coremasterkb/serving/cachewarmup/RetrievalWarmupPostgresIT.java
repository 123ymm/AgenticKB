package com.coremasterkb.serving.cachewarmup;

import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.MethodOrderer.OrderAnnotation;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.datasource.AbstractDataSource;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Real JDBC smoke test, restricted to the explicitly created disposable local database. */
@EnabledIfEnvironmentVariable(named = "CACHE_WARMUP_TEST_JDBC_URL", matches = ".+")
@TestMethodOrder(OrderAnnotation.class)
class RetrievalWarmupPostgresIT {
    private static final String ALLOWED_URL_PATTERN = "jdbc:postgresql://127\\.0\\.0\\.1:[0-9]{1,5}/cachewarm_test";
    private static final String EXPECTED_DATABASE = "cachewarm_test";

    private static Connection connect() throws SQLException {
        // Full matching excludes query parameters that could redirect host, database, or credentials.
        String jdbcUrl = System.getenv("CACHE_WARMUP_TEST_JDBC_URL");
        if (jdbcUrl == null || !jdbcUrl.matches(ALLOWED_URL_PATTERN)) {
            throw new SQLException("Refusing a connection outside the disposable cachewarm_test database");
        }
        Connection connection = DriverManager.getConnection(jdbcUrl, "postgres", "");
        try {
            if (connection.getMetaData().getDatabaseMajorVersion() != 15) {
                throw new SQLException("This integration fixture requires its disposable PostgreSQL 15 server");
            }
            try (Statement statement = connection.createStatement()) {
                statement.setQueryTimeout(5);
                try (ResultSet database = statement.executeQuery("SELECT current_database()")) {
                    if (!database.next() || !EXPECTED_DATABASE.equals(database.getString(1))) {
                        throw new SQLException("Refusing DDL outside the disposable integration database");
                    }
                }
            }
            return connection;
        } catch (SQLException ex) {
            connection.close();
            throw ex;
        }
    }

    @BeforeAll
    static void seedTinyDedicatedFixture() throws Exception {
        try (Connection connection = connect(); Statement statement = connection.createStatement()) {
            statement.setQueryTimeout(15);
            for (String ddl : List.of(
                    "CREATE EXTENSION IF NOT EXISTS vector",
                    "CREATE EXTENSION IF NOT EXISTS pg_prewarm",
                    """
                    CREATE TABLE IF NOT EXISTS public.asset_retrieval_units_v2 (
                      representation_id text PRIMARY KEY, snapshot_id text NOT NULL,
                      content_text text, dense_eligible boolean NOT NULL DEFAULT true)
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS public.asset_retrieval_embeddings_v2 (
                      embedding_id text PRIMARY KEY, representation_id text NOT NULL,
                      snapshot_id text NOT NULL, dimension integer NOT NULL DEFAULT 3,
                      embedding_vector_vec vector(3) NOT NULL)
                    """,
                    """
                    INSERT INTO public.asset_retrieval_units_v2(representation_id,snapshot_id,content_text)
                    VALUES ('warmup-it-a','warmup-it-snapshot','tiny fixture a'),
                           ('warmup-it-b','warmup-it-snapshot','tiny fixture b'),
                           ('warmup-it-c','warmup-it-snapshot','tiny fixture c')
                    ON CONFLICT(representation_id) DO NOTHING
                    """,
                    """
                    INSERT INTO public.asset_retrieval_embeddings_v2
                      (embedding_id,representation_id,snapshot_id,embedding_vector_vec)
                    VALUES ('warmup-it-ea','warmup-it-a','warmup-it-snapshot','[1,0,0]'),
                           ('warmup-it-eb','warmup-it-b','warmup-it-snapshot','[0,1,0]'),
                           ('warmup-it-ec','warmup-it-c','warmup-it-snapshot','[0,0,1]')
                    ON CONFLICT(embedding_id) DO NOTHING
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_are_v2_vec_hnsw
                    ON public.asset_retrieval_embeddings_v2
                    USING hnsw(embedding_vector_vec vector_cosine_ops) WITH(m=4,ef_construction=16)
                    """)) {
                statement.execute(ddl);
            }
        }
        // Deliberately leave these tiny relations for the caller's subsequent native-autoprewarm restart check.
    }

    @Test
    @Order(1)
    void actualPostgresInspectsPhysicalIdentityAndWarmsEveryBoundBlockRange() throws Exception {
        Source source = new Source(true);
        JdbcRetrievalCacheWarmupBackend backend = new JdbcRetrievalCacheWarmupBackend();
        var before = backend.inspect(source);
        assertThat(before.enabled()).isTrue();
        assertThat(before.epoch()).isNotBlank();
        assertThat(before.signature()).contains(EXPECTED_DATABASE, "idx_are_v2_vec_hnsw");
        assertThat(before.targets().stream().filter(target -> target.mode().equals("buffer")).toList())
                .extracting(RetrievalCacheWarmupService.Target::name)
                .containsExactlyElementsOf(JdbcRetrievalCacheWarmupBackend.TARGET_NAMES);
        assertThat(before.targets()).anyMatch(target -> target.mode().equals("read")
                && target.name().startsWith("pg_toast."));

        backend.warm(source, before);

        var expectedCalls = before.targets().stream().filter(target -> target.blocks() > 0).toList();
        assertThat(source.calls).hasSize(expectedCalls.size());
        assertThat(source.calls).anyMatch(call -> call.mode().equals("read"));
        for (int index = 0; index < expectedCalls.size(); index++) {
            var target = expectedCalls.get(index);
            var call = source.calls.get(index);
            assertThat(call.name()).isEqualTo(target.name());
            assertThat(call.mode()).isEqualTo(target.mode());
            assertThat(call.lastBlock() + 1).isEqualTo(target.blocks());
            assertThat(call.timeoutSeconds()).isBetween(121, 900);
        }
        assertThat(backend.inspect(source).token()).isEqualTo(before.token());
    }

    @Test
    @Order(2)
    void unsetOrDisabledOptinDoesNotSubmitAnyWarmup() {
        Source source = new Source(false);
        List<Runnable> queued = new ArrayList<>();
        RetrievalCacheWarmupService service = new RetrievalCacheWarmupService(source,
                ignored -> source, Set::of, new JdbcRetrievalCacheWarmupBackend(), queued::add, System::nanoTime);
        service.requireReady(null);
        assertThat(service.readiness().ready()).isTrue();
        assertThat(service.readiness().databases().get("default").status()).isEqualTo("off");
        assertThat(queued).isEmpty();
        assertThat(source.calls).isEmpty();
    }

    @Test
    @Order(3)
    void missingGraphIndexFailsClosedAndFixtureIsRestored() throws Exception {
        try (Connection connection = connect(); Statement statement = connection.createStatement()) {
            statement.setQueryTimeout(5);
            statement.execute("ALTER INDEX public.idx_are_v2_vec_hnsw RENAME TO idx_are_v2_vec_hnsw_it_hidden");
            try {
                assertThatThrownBy(() -> new JdbcRetrievalCacheWarmupBackend().inspect(new Source(true)))
                        .isInstanceOf(SQLException.class).hasMessage("prewarm_relation_invalid");
            } finally {
                statement.execute("ALTER INDEX public.idx_are_v2_vec_hnsw_it_hidden RENAME TO idx_are_v2_vec_hnsw");
            }
        }
        assertThat(new JdbcRetrievalCacheWarmupBackend().inspect(new Source(true)).enabled()).isTrue();
    }

    private record Call(String name, String mode, long lastBlock, int timeoutSeconds) { }

    private static final class Source extends AbstractDataSource {
        private final boolean enabled;
        private final List<Call> calls = new ArrayList<>();

        Source(boolean enabled) { this.enabled = enabled; }

        @Override
        public Connection getConnection() throws SQLException {
            Connection connection = connect();
            try (Statement settings = connection.createStatement()) {
                settings.setQueryTimeout(5);
                settings.execute("SET cmkb.retrieval_prewarm='" + (enabled ? "on" : "off") + "'");
                settings.execute("SET statement_timeout='60s'");
            }
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(),
                    new Class<?>[]{Connection.class}, (proxy, method, args) -> {
                        try {
                            Object result = method.invoke(connection, args);
                            if (method.getName().equals("prepareStatement")
                                    && ((String) args[0]).contains(".pg_prewarm(")) {
                                return tracked((PreparedStatement) result);
                            }
                            return result;
                        } catch (InvocationTargetException ex) { throw ex.getCause(); }
                    });
        }

        private PreparedStatement tracked(PreparedStatement actual) {
            String[] name = {null};
            String[] mode = {null};
            long[] lastBlock = {-1};
            int[] timeout = {0};
            return (PreparedStatement) Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(),
                    new Class<?>[]{PreparedStatement.class}, (proxy, method, args) -> {
                        if (method.getName().equals("setString") && (Integer) args[0] == 1) name[0] = (String) args[1];
                        if (method.getName().equals("setString") && (Integer) args[0] == 2) mode[0] = (String) args[1];
                        if (method.getName().equals("setLong") && (Integer) args[0] == 3) lastBlock[0] = (Long) args[1];
                        if (method.getName().equals("setQueryTimeout")) timeout[0] = (Integer) args[0];
                        if (method.getName().equals("executeQuery")) calls.add(new Call(name[0], mode[0], lastBlock[0], timeout[0]));
                        try { return method.invoke(actual, args); }
                        catch (InvocationTargetException ex) { throw ex.getCause(); }
                    });
        }

        @Override
        public Connection getConnection(String username, String password) throws SQLException {
            if (!"postgres".equals(username) || !password.isEmpty()) throw new SQLException("Unexpected fixture credentials");
            return getConnection();
        }
    }
}
