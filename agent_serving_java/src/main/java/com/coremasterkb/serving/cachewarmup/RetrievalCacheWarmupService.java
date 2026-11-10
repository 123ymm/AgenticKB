package com.coremasterkb.serving.cachewarmup;

import com.coremasterkb.serving.domainpack.DomainPoolManager;
import com.coremasterkb.serving.domainpack.DomainRegistry;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ResponseStatusException;

import javax.sql.DataSource;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executor;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

/** Opt-in PostgreSQL cache gate; a successful connection alone never establishes readiness. */
@Component
public class RetrievalCacheWarmupService {
    private static final Logger log = LoggerFactory.getLogger(RetrievalCacheWarmupService.class);
    private static final long RETRY_NANOS = TimeUnit.SECONDS.toNanos(30);
    private final DataSource defaultSource;
    private final Function<String, DataSource> resolver;
    private final Supplier<Set<String>> domains;
    private final Backend backend;
    private final Executor workers;
    private final LongSupplier clock;
    private final Map<DataSource, Cell> cells = Collections.synchronizedMap(new IdentityHashMap<>());
    private final AtomicReference<Map<String, Cell>> aliases = new AtomicReference<>(Map.of());
    private ScheduledExecutorService scheduler;
    private boolean ownsWorkers;

    @Autowired
    public RetrievalCacheWarmupService(@Qualifier("defaultDataSource") DataSource source,
                                      DomainPoolManager pools, DomainRegistry registry) {
        this(source, pools::getDataSource,
                () -> registry.knownDomains().stream()
                        .filter(name -> registry.findEntry(name).map(e -> e.enabled()).orElse(false))
                        .collect(java.util.stream.Collectors.toUnmodifiableSet()),
                new JdbcRetrievalCacheWarmupBackend(), Executors.newFixedThreadPool(2, task -> {
                    Thread thread = new Thread(task, "retrieval-cache-prewarm");
                    thread.setDaemon(true);
                    return thread;
                }), System::nanoTime);
        ownsWorkers = true;
    }

    RetrievalCacheWarmupService(DataSource source, Function<String, DataSource> resolver,
                               Supplier<Set<String>> domains, Backend backend,
                               Executor workers, LongSupplier clock) {
        this.defaultSource = source;
        this.resolver = resolver;
        this.domains = domains;
        this.backend = backend;
        this.workers = workers;
        this.clock = clock;
    }

    /** Inspects the current server epoch and physical relation identity on every request. */
    public void requireReady(String domain) {
        try {
            DataSource source = domain == null || domain.isBlank() ? defaultSource : resolver.apply(domain);
            Cell cell = cell(source);
            register(domain == null || domain.isBlank() ? "default" : domain, cell);
            if (inspect(source, cell)) return;
        } catch (CacheWarmingException ex) {
            throw ex;
        } catch (Exception ex) {
            throw new CacheWarmingException("retrieval_cache_unavailable");
        }
        throw new CacheWarmingException("retrieval_cache_warming");
    }

    public ReadinessSnapshot readiness() {
        Map<String, DatabaseReadiness> snapshots = new LinkedHashMap<>();
        aliases.get().forEach((name, cell) -> {
            State state = cell.state;
            snapshots.put(name, new DatabaseReadiness(state.status,
                    state.probe == null ? "" : state.probe.epoch, state.error));
        });
        boolean ready = !snapshots.isEmpty() && snapshots.values().stream()
                .allMatch(value -> value.status.equals("ready") || value.status.equals("off"));
        return new ReadinessSnapshot(ready, ready ? "ready" : "warming", Map.copyOf(snapshots));
    }

    @EventListener(ApplicationReadyEvent.class)
    public synchronized void start() {
        if (scheduler != null) return;
        scheduler = Executors.newSingleThreadScheduledExecutor(task -> {
            Thread thread = new Thread(task, "retrieval-cache-observer");
            thread.setDaemon(true);
            return thread;
        });
        scheduler.scheduleWithFixedDelay(this::refresh, 0, 5, TimeUnit.SECONDS);
    }

    /** Background discovery warms all enabled databases before their first retrieval. */
    void refresh() {
        Map<String, Cell> active = new LinkedHashMap<>();
        observe("default", defaultSource, active);
        try {
            for (String domain : domains.get()) {
                try { observe(domain, resolver.apply(domain), active); }
                catch (Exception ex) {
                    Cell failed = new Cell();
                    failed.state = new State(null, false, false, 0, "failed", "retrieval_database_unavailable");
                    active.put(domain, failed);
                }
            }
        } catch (Exception ex) {
            Cell failed = new Cell();
            failed.state = new State(null, false, false, 0, "failed", "retrieval_domain_discovery_failed");
            active.put("domain-discovery", failed);
        }
        aliases.set(Map.copyOf(active));
    }

    private void observe(String name, DataSource source, Map<String, Cell> active) {
        Cell cell = cell(source);
        active.put(name, cell);
        try { inspect(source, cell); }
        catch (Exception ex) { /* inspect records the failure; the next bounded sweep retries. */ }
    }

    private Cell cell(DataSource source) {
        synchronized (cells) { return cells.computeIfAbsent(source, ignored -> new Cell()); }
    }

    private void register(String name, Cell cell) {
        aliases.updateAndGet(previous -> {
            Map<String, Cell> next = new LinkedHashMap<>(previous);
            next.put(name, cell);
            return Map.copyOf(next);
        });
    }

    private boolean inspect(DataSource source, Cell cell) throws Exception {
        Probe probe;
        try { probe = backend.inspect(source); }
        catch (Exception ex) {
            synchronized (cell) {
                State old = cell.state;
                cell.state = new State(old.probe, false, old.running, old.retryAt,
                        "failed", "retrieval_cache_inspection_failed");
            }
            throw ex;
        }
        synchronized (cell) {
            State old = cell.state;
            if (!probe.enabled) {
                cell.state = new State(probe, true, old.running, 0, "off", "");
                return true;
            }
            boolean same = old.probe != null && old.probe.token().equals(probe.token());
            if (same && old.ready) return true;
            long retryAt = same ? old.retryAt : 0;
            cell.state = new State(probe, false, old.running, retryAt,
                    old.running ? "warming" : (coolingDown(retryAt) ? "failed" : "checking"),
                    same ? old.error : "");
            if (old.running || coolingDown(retryAt)) return false;
            cell.state = new State(probe, false, true, 0, "warming", "");
        }
        try { workers.execute(() -> warm(source, cell, probe)); }
        catch (RuntimeException ex) { completeFailure(cell, probe); }
        return false;
    }

    private void warm(DataSource source, Cell cell, Probe expected) {
        long started = clock.getAsLong();
        try {
            Probe before = backend.inspect(source);
            if (!before.enabled || !before.token().equals(expected.token())) {
                synchronized (cell) {
                    State old = cell.state;
                    Probe latest = old.probe != null && !old.probe.token().equals(expected.token()) ? old.probe : before;
                    cell.state = new State(latest, !latest.enabled, false, 0,
                            latest.enabled ? "checking" : "off", "");
                }
                return;
            }
            log.info("Retrieval cache prewarm started (epoch={}, relations={})", expected.epoch, expected.targets.size());
            backend.warm(source, expected);
            Probe after = backend.inspect(source);
            synchronized (cell) {
                State old = cell.state;
                boolean matches = old.probe != null && old.probe.enabled && after.enabled
                        && expected.token().equals(old.probe.token()) && expected.token().equals(after.token());
                boolean off = old.probe != null && !old.probe.enabled;
                cell.state = new State(matches ? after : old.probe, matches || off, false, 0,
                        matches ? "ready" : (off ? "off" : "checking"), "");
                if (matches) log.info("Retrieval cache prewarm ready (epoch={}, durationMs={})",
                        expected.epoch, TimeUnit.NANOSECONDS.toMillis(clock.getAsLong() - started));
            }
        } catch (Exception ex) {
            log.warn("Retrieval cache prewarm failed (epoch={}, errorType={})", expected.epoch, ex.getClass().getSimpleName());
            completeFailure(cell, expected);
        }
    }

    private boolean coolingDown(long deadline) {
        return deadline != 0 && clock.getAsLong() - deadline < 0;
    }

    private void completeFailure(Cell cell, Probe expected) {
        synchronized (cell) {
            State old = cell.state;
            boolean same = old.probe != null && old.probe.token().equals(expected.token());
            boolean off = old.probe != null && !old.probe.enabled;
            cell.state = new State(old.probe, off, false, same ? clock.getAsLong() + RETRY_NANOS : 0,
                    off ? "off" : (same ? "failed" : "checking"), same ? "retrieval_cache_warm_failed" : "");
        }
    }

    @PreDestroy
    public synchronized void close() {
        if (scheduler != null) scheduler.shutdownNow();
        if (ownsWorkers && workers instanceof ExecutorService pool) pool.shutdownNow();
    }

    public record ReadinessSnapshot(boolean ready, String status, Map<String, DatabaseReadiness> databases) {
        public ReadinessSnapshot { databases = Map.copyOf(databases); }
    }
    public record DatabaseReadiness(String status, String epoch, String lastError) { }
    public static final class CacheWarmingException extends ResponseStatusException {
        public CacheWarmingException(String reason) { super(HttpStatus.SERVICE_UNAVAILABLE, reason); }
    }
    interface Backend {
        Probe inspect(DataSource source) throws Exception;
        void warm(DataSource source, Probe probe) throws Exception;
    }
    record Probe(boolean enabled, String epoch, String signature, long sharedBytes,
                 String extensionSchema, List<Target> targets) {
        Probe { targets = List.copyOf(targets); }
        static Probe off() { return new Probe(false, "", "off", 0, "", List.of()); }
        String token() { return epoch + ":" + signature; }
    }
    record Target(String name, long blocks, String mode) {
        Target(String name, long blocks) { this(name, blocks, "buffer"); }
        Target {
            if (!"buffer".equals(mode) && !"read".equals(mode)) throw new IllegalArgumentException("invalid_prewarm_mode");
        }
    }
    private record State(Probe probe, boolean ready, boolean running, long retryAt, String status, String error) { }
    private static final class Cell {
        volatile State state = new State(null, false, false, 0, "checking", "");
    }
}
