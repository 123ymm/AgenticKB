package com.coremasterkb.serving.api;

import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService.CacheWarmingException;
import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService.ReadinessSnapshot;
import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService;
import com.coremasterkb.serving.config.ServingProperties;
import com.coremasterkb.serving.domainpack.DomainPoolManager;
import com.coremasterkb.serving.domainpack.DomainRegistry;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.lang.reflect.Proxy;
import javax.sql.DataSource;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class RetrievalReadinessControllerTest {
    @Test
    void productionConstructorDelegatesToInjectedWarmupService() throws Exception {
        var snapshot = new AtomicReference<>(new ReadinessSnapshot(false, "warming", Map.of()));
        var registry = new DomainRegistry();
        var source = (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(),
                new Class<?>[]{DataSource.class}, (proxy, method, args) -> {
                    throw new AssertionError("Readiness snapshot must not open a database connection");
                });
        var warmup = new RetrievalCacheWarmupService(source, new DomainPoolManager(registry, source), registry) {
            @Override public ReadinessSnapshot readiness() { return snapshot.get(); }
        };
        try {
            var mvc = MockMvcBuilders.standaloneSetup(new RetrievalReadinessController(warmup)).build();
            mvc.perform(get("/actuator/health/readiness"))
                    .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.ready").value(false));
            snapshot.set(new ReadinessSnapshot(true, "ready", Map.of()));
            mvc.perform(get("/actuator/health/readiness"))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.ready").value(true));
        } finally {
            warmup.close();
        }
    }

    @Test
    void readinessChangesFrom503To200WithoutExposingDatabaseDetails() throws Exception {
        var snapshot = new AtomicReference<>(new ReadinessSnapshot(false, "warming", Map.of()));
        var mvc = MockMvcBuilders.standaloneSetup(new RetrievalReadinessController(snapshot::get)).build();
        mvc.perform(get("/actuator/health/readiness"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(header().string("Retry-After", "2"))
                .andExpect(jsonPath("$.status").value("warming"))
                .andExpect(jsonPath("$.databases").doesNotExist());
        snapshot.set(new ReadinessSnapshot(true, "ready", Map.of()));
        mvc.perform(get("/actuator/health/readiness"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("ready"));
    }

    @Test
    void warmingExceptionEscapesGlobalCatchAllAsSanitized503() throws Exception {
        var mvc = MockMvcBuilders.standaloneSetup(new WarmingEndpoint())
                .setControllerAdvice(new RetrievalWarmupExceptionHandler(), new GlobalExceptionHandler()).build();
        mvc.perform(get("/test-warming"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(header().string("Retry-After", "2"))
                .andExpect(jsonPath("$.error").value("retrieval_cache_warming"))
                .andExpect(jsonPath("$.status").value("warming"));
    }

    @Test
    void localAnonymousHealthNeedsNoJwtAndIdentityFilterRemainsEnforced() throws Exception {
        var mvc = MockMvcBuilders.standaloneSetup(new RetrievalReadinessController(
                        () -> new ReadinessSnapshot(true, "ready", Map.of())))
                .addFilters(new TrustedIdentityHeaderFilter(new ServingProperties(
                        null, null, null, null, null, null, new ServingProperties.InternalAuth("test-secret"))))
                .build();
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
        mvc.perform(get("/actuator/health/readiness").header("X-KB-User", "forged-user"))
                .andExpect(status().isUnauthorized());
    }

    @RestController
    static class WarmingEndpoint {
        @GetMapping("/test-warming")
        void warming() { throw new CacheWarmingException("retrieval_cache_unavailable"); }
    }
}
