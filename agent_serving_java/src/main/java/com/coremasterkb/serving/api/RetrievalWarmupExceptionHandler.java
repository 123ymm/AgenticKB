package com.coremasterkb.serving.api;

import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService.CacheWarmingException;
import org.springframework.core.annotation.Order;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.Map;

/** Keep this specific 503 ahead of the application's catch-all exception advice. */
@RestControllerAdvice
@Order(-1)
public class RetrievalWarmupExceptionHandler {
    @ExceptionHandler(CacheWarmingException.class)
    public ResponseEntity<Map<String, Object>> warming(CacheWarmingException ignored) {
        return ResponseEntity.status(503).header("Retry-After", "2").body(Map.of(
                "status", "warming", "error", "retrieval_cache_warming",
                "message", "Search is warming up. Please retry shortly."));
    }
}
