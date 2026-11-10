package com.coremasterkb.serving.cachewarmup;

import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.lang.reflect.Proxy;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executor;
import java.util.concurrent.atomic.AtomicLong;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class RetrievalCacheWarmupServiceTest {
    private static final class Queue implements Executor {
        final ArrayDeque<Runnable> tasks = new ArrayDeque<>();
        public void execute(Runnable task) { tasks.add(task); }
        void run() { tasks.remove().run(); }
    }

    private static final class Backend implements RetrievalCacheWarmupService.Backend {
        final Map<DataSource, RetrievalCacheWarmupService.Probe> probes = new java.util.IdentityHashMap<>();
        final List<String> warmed = new ArrayList<>();
        boolean fail;
        boolean inspectFails;
        Runnable duringWarm = () -> { };
        public RetrievalCacheWarmupService.Probe inspect(DataSource source) {
            if (inspectFails) throw new IllegalStateException("sensitive inspection details");
            return probes.get(source);
        }
        public void warm(DataSource source, RetrievalCacheWarmupService.Probe probe) {
            warmed.add(probe.token());
            duringWarm.run();
            if (fail) throw new IllegalStateException("sensitive connection details");
        }
    }

    private static DataSource source() {
        return (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(), new Class<?>[]{DataSource.class},
                (proxy, method, args) -> switch (method.getName()) {
                    case "hashCode" -> System.identityHashCode(proxy);
                    case "equals" -> proxy == args[0];
                    case "toString" -> "test-source";
                    default -> throw new UnsupportedOperationException();
                });
    }

    private static RetrievalCacheWarmupService.Probe probe(String epoch, String signature) {
        return new RetrievalCacheWarmupService.Probe(true, epoch, signature, 8L << 30,
                "public", List.of(new RetrievalCacheWarmupService.Target("public.test", 1)));
    }

    private static final class Fixture {
        final DataSource primary = source();
        final DataSource second = source();
        final Backend backend = new Backend();
        final Queue queue = new Queue();
        final AtomicLong now = new AtomicLong();
        final RetrievalCacheWarmupService service = new RetrievalCacheWarmupService(
                primary, name -> name.equals("a") ? primary : second, () -> Set.of("a", "b"),
                backend, queue, now::get);
        Fixture() {
            backend.probes.put(primary, probe("epoch-1", "db1:index-1"));
            backend.probes.put(second, probe("epoch-1", "db2:index-1"));
        }
        void notReady(String domain) {
            assertThatThrownBy(() -> service.requireReady(domain))
                    .isInstanceOf(RetrievalCacheWarmupService.CacheWarmingException.class)
                    .hasMessageContaining("503");
        }
        void ready(String domain) { notReady(domain); queue.run(); service.requireReady(domain); }
    }

    @Test
    void requestRequiresCompletedWarmupAndReadsEpochAgainOnEveryRequest() {
        Fixture f = new Fixture();
        f.ready("a");
        assertThat(f.service.readiness().ready()).isTrue();
        f.backend.probes.put(f.primary, probe("epoch-2", "db1:index-1"));
        f.notReady("a");
        assertThat(f.service.readiness().ready()).isFalse();
        f.queue.run();
        f.service.requireReady("a");
    }

    @Test
    void concurrentAliasesOfOneDataSourceScheduleOnlyOneTask() {
        Fixture f = new Fixture();
        for (int i = 0; i < 10; i++) { f.notReady("a"); f.notReady(null); }
        assertThat(f.queue.tasks).hasSize(1);
        f.queue.run();
        f.service.requireReady("a");
        f.service.requireReady(null);
        assertThat(f.backend.warmed).hasSize(1);
    }

    @Test
    void failedWarmupRemainsNotReadyAndRetriesAfterBackoffWithoutLeakingDetails() {
        Fixture f = new Fixture();
        f.backend.fail = true;
        f.notReady("a");
        f.queue.run();
        f.notReady("a");
        assertThat(f.queue.tasks).isEmpty();
        assertThat(f.service.readiness().databases().get("a").lastError())
                .isEqualTo("retrieval_cache_warm_failed");
        f.now.set(31_000_000_000L);
        f.backend.fail = false;
        f.notReady("a");
        f.queue.run();
        f.service.requireReady("a");
    }

    @Test
    void oldTaskCannotPublishReadyAfterRestartOrReplaceNewerObservation() {
        Fixture f = new Fixture();
        f.notReady("a");
        f.backend.duringWarm = () -> {
            f.backend.probes.put(f.primary, probe("epoch-2", "db1:index-1"));
            f.notReady("a");
            assertThat(f.queue.tasks).isEmpty();
        };
        f.queue.run();
        assertThat(f.service.readiness().ready()).isFalse();
        f.backend.duringWarm = () -> { };
        f.notReady("a");
        f.queue.run();
        f.service.requireReady("a");
    }

    @Test
    void indexRebuildAndDatabaseIdentityInvalidateReadyWithoutRestart() {
        Fixture f = new Fixture();
        f.ready("a");
        f.backend.probes.put(f.primary, probe("epoch-1", "db1:index-2"));
        f.notReady("a"); f.queue.run(); f.service.requireReady("a");
        f.backend.probes.put(f.primary, probe("epoch-1", "db-new:index-2"));
        f.notReady("a");
        assertThat(f.service.readiness().ready()).isFalse();
    }

    @Test
    void offDoesNotWarmOrBlockAndSnapshotsAreImmutable() {
        Fixture f = new Fixture();
        f.backend.probes.put(f.primary, RetrievalCacheWarmupService.Probe.off());
        f.service.requireReady("a");
        assertThat(f.queue.tasks).isEmpty();
        assertThat(f.service.readiness().ready()).isTrue();
        assertThatThrownBy(() -> f.service.readiness().databases().clear())
                .isInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void backgroundSweepWarmsAllEnabledDatabasesAndTracksIndependentReadiness() {
        Fixture f = new Fixture();
        f.service.refresh();
        assertThat(f.queue.tasks).hasSize(2);
        f.queue.run();
        f.service.requireReady("a");
        f.notReady("b");
        assertThat(f.service.readiness().ready()).isFalse();
        f.queue.run();
        assertThat(f.service.readiness().ready()).isTrue();
    }

    @Test
    void distinctDataSourcesWithSameEpochNeverShareReadyState() {
        Fixture f = new Fixture();
        f.backend.probes.put(f.second, probe("epoch-1", "db1:index-1"));
        f.ready("a");
        f.notReady("b");
        assertThat(f.queue.tasks).hasSize(1);
    }

    @Test
    void guardedCapacityAndExactWarmupOrderAreExplicit() {
        assertThat(JdbcRetrievalCacheWarmupBackend.TARGET_NAMES).containsExactly(
                "public.asset_retrieval_embeddings_v2", "public.asset_retrieval_units_v2",
                "public.asset_retrieval_units_v2_pkey", "public.idx_are_v2_vec_hnsw");
        assertThatThrownBy(() -> JdbcRetrievalCacheWarmupBackend.checkCapacity(8L << 30, 7L << 30))
                .isInstanceOf(IllegalStateException.class);
        JdbcRetrievalCacheWarmupBackend.checkCapacity(8L << 30, 6L << 30);
    }

    @Test
    void negativeNanoTimeOriginDoesNotPreventFirstWarmup() {
        Fixture f = new Fixture();
        f.now.set(-100_000_000_000L);
        f.ready("a");
        assertThat(f.service.readiness().ready()).isTrue();
    }

    @Test
    void staleQueuedTaskDoesNotWarmAReplacedIndex() {
        Fixture f = new Fixture();
        f.notReady("a");
        f.backend.probes.put(f.primary, probe("epoch-1", "db1:index-new"));
        f.queue.run();
        assertThat(f.backend.warmed).isEmpty();
        assertThat(f.service.readiness().ready()).isFalse();
        f.notReady("a");
        f.queue.run();
        f.service.requireReady("a");
    }

    @Test
    void disablingOptinWhileTaskRunsCannotLeaveOffDatabaseBlocked() {
        Fixture f = new Fixture();
        f.notReady("a");
        f.backend.duringWarm = () -> {
            f.backend.probes.put(f.primary, RetrievalCacheWarmupService.Probe.off());
            f.service.requireReady("a");
        };
        f.queue.run();
        assertThat(f.service.readiness().ready()).isTrue();
        f.service.requireReady("a");
    }

    @Test
    void inspectionFailureBlocksRequestsAndBackgroundObserverRecordsIt() {
        Fixture f = new Fixture();
        assertThat(f.service.readiness().ready()).isFalse();
        f.backend.inspectFails = true;
        assertThatThrownBy(() -> f.service.requireReady("a"))
                .isInstanceOf(RetrievalCacheWarmupService.CacheWarmingException.class)
                .hasMessageContaining("retrieval_cache_unavailable");
        f.service.refresh();
        assertThat(f.service.readiness().ready()).isFalse();
        assertThat(f.service.readiness().databases().get("default").lastError())
                .isEqualTo("retrieval_cache_inspection_failed");
        assertThat(f.queue.tasks).isEmpty();
    }

    @Test
    void failedDomainResolutionAndDiscoveryCannotReportGlobalReady() {
        Fixture f = new Fixture();
        f.backend.probes.put(f.primary, RetrievalCacheWarmupService.Probe.off());
        var domainFailure = new RetrievalCacheWarmupService(f.primary,
                domain -> { throw new IllegalStateException(); }, () -> Set.of("unavailable"),
                f.backend, f.queue, f.now::get);
        domainFailure.refresh();
        assertThat(domainFailure.readiness().ready()).isFalse();
        assertThat(domainFailure.readiness().databases().get("unavailable").lastError())
                .isEqualTo("retrieval_database_unavailable");
        var discoveryFailure = new RetrievalCacheWarmupService(f.primary, domain -> f.primary,
                () -> { throw new IllegalStateException(); }, f.backend, f.queue, f.now::get);
        discoveryFailure.refresh();
        assertThat(discoveryFailure.readiness().ready()).isFalse();
        assertThat(discoveryFailure.readiness().databases().get("domain-discovery").epoch()).isEmpty();
    }

    @Test
    void rejectedExecutorCannotLeaveReadinessStuckOrReportSuccess() {
        Fixture f = new Fixture();
        var service = new RetrievalCacheWarmupService(f.primary, domain -> f.primary, Set::of,
                f.backend, task -> { throw new java.util.concurrent.RejectedExecutionException(); }, f.now::get);
        assertThatThrownBy(() -> service.requireReady(null))
                .isInstanceOf(RetrievalCacheWarmupService.CacheWarmingException.class);
        assertThat(service.readiness().databases().get("default").lastError())
                .isEqualTo("retrieval_cache_warm_failed");
        service.close();
    }

    @Test
    void blankDomainUsesDefaultAndSpecificWarmupExceptionKeepsItsStatus() {
        Fixture f = new Fixture();
        f.ready(" ");
        assertThat(f.service.readiness().databases()).containsKey("default");
        var failure = new RetrievalCacheWarmupService.CacheWarmingException("retrieval_cache_warming");
        var service = new RetrievalCacheWarmupService(f.primary, domain -> { throw failure; }, Set::of,
                f.backend, f.queue, f.now::get);
        assertThatThrownBy(() -> service.requireReady("broken")).isSameAs(failure);
    }

    @Test
    void disabledQueuedTaskDoesNoWorkAndSupersededObservationIsRetained() {
        Fixture f = new Fixture();
        f.notReady("a");
        f.backend.probes.put(f.primary, RetrievalCacheWarmupService.Probe.off());
        f.queue.run();
        assertThat(f.backend.warmed).isEmpty();
        assertThat(f.service.readiness().ready()).isTrue();
        f.backend.probes.put(f.primary, probe("epoch-2", "db1:index-2"));
        f.notReady("a");
        f.backend.probes.put(f.primary, probe("epoch-3", "db1:index-3"));
        f.notReady("a");
        f.queue.run();
        assertThat(f.service.readiness().databases().get("a").epoch()).isEqualTo("epoch-3");
        assertThat(f.service.readiness().ready()).isFalse();
    }

    @Test
    void failureAfterOptoutDoesNotPublishFailureForDisabledDatabase() {
        Fixture f = new Fixture();
        f.backend.fail = true;
        f.backend.duringWarm = () -> {
            f.backend.probes.put(f.primary, RetrievalCacheWarmupService.Probe.off());
            f.service.requireReady("a");
        };
        f.notReady("a");
        f.queue.run();
        assertThat(f.service.readiness().ready()).isTrue();
        assertThat(f.service.readiness().databases().get("a").status()).isEqualTo("off");
    }

    @Test
    void taskPostcheckDetectsRestartEvenWithoutAnotherRequest() {
        Fixture f = new Fixture();
        f.backend.duringWarm = () -> f.backend.probes.put(f.primary, probe("epoch-2", "db1:index-1"));
        f.notReady("a");
        f.queue.run();
        assertThat(f.service.readiness().ready()).isFalse();
        f.notReady("a");
        assertThat(f.queue.tasks).hasSize(1);
    }
}
