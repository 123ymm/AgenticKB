package com.coremasterkb.serving.api;

import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService;
import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService.ReadinessSnapshot;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.Objects;
import java.util.function.Supplier;

/** Readiness is distinct from process liveness and exposes no database configuration. */
@RestController
public class RetrievalReadinessController {
    private final Supplier<ReadinessSnapshot> readiness;

    @Autowired
    public RetrievalReadinessController(RetrievalCacheWarmupService warmupService) {
        this(Objects.requireNonNull(warmupService)::readiness);
    }

    RetrievalReadinessController(Supplier<ReadinessSnapshot> readiness) {
        this.readiness = Objects.requireNonNull(readiness);
    }

    @GetMapping("/actuator/health/readiness")
    public ResponseEntity<Map<String, Object>> readiness() {
        boolean ready = readiness.get().ready();
        Map<String, Object> body = Map.of("status", ready ? "ready" : "warming", "ready", ready);
        return ready ? ResponseEntity.ok(body)
                : ResponseEntity.status(503).header("Retry-After", "2").body(body);
    }
}
