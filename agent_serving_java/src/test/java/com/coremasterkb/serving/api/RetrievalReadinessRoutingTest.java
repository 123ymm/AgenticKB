package com.coremasterkb.serving.api;

import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService.ReadinessSnapshot;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.test.web.servlet.MockMvc;

import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/** Includes Actuator's real health wildcard mapping, absent from standalone controller tests. */
@SpringBootTest(classes = RetrievalReadinessRoutingTest.App.class,
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"management.health.defaults.enabled=false", "spring.main.banner-mode=off"})
@AutoConfigureMockMvc
@org.springframework.test.context.TestExecutionListeners(listeners = {
        org.springframework.test.context.web.ServletTestExecutionListener.class,
        org.springframework.test.context.support.DependencyInjectionTestExecutionListener.class,
        org.springframework.test.context.support.DirtiesContextTestExecutionListener.class
}, mergeMode = org.springframework.test.context.TestExecutionListeners.MergeMode.REPLACE_DEFAULTS)
class RetrievalReadinessRoutingTest {
    @Autowired private MockMvc mvc;
    @Autowired private AtomicBoolean ready;
    @LocalServerPort private int port;

    @Test
    void customReadinessRouteIsReachableAlongsideActuatorHealth() throws Exception {
        mvc.perform(get("/actuator/health/readiness"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.status").value("warming"));
        assertHeadResponse(503);
        mvc.perform(get("/actuator/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").exists());
        ready.set(true);
        mvc.perform(get("/actuator/health/readiness"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("ready"));
        assertHeadResponse(200);
        mvc.perform(post("/actuator/health/readiness"))
                .andExpect(status().isMethodNotAllowed());
    }

    /** MockMvc retains HEAD content; verify the no-body contract through the real servlet container. */
    private void assertHeadResponse(int expectedStatus) throws Exception {
        try (var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build()) {
            var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port
                            + "/actuator/health/readiness"))
                    .timeout(Duration.ofSeconds(5)).method("HEAD", HttpRequest.BodyPublishers.noBody()).build();
            var response = client.send(request, HttpResponse.BodyHandlers.ofString());
            assertThat(response.statusCode()).isEqualTo(expectedStatus);
            assertThat(response.body()).isEmpty();
            if (expectedStatus == 503) assertThat(response.headers().firstValue("Retry-After")).contains("2");
        }
    }

    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration(exclude = DataSourceAutoConfiguration.class)
    @org.springframework.context.annotation.Import({RetrievalReadinessRoutes.class, GlobalExceptionHandler.class})
    static class App {
        @Bean AtomicBoolean ready() { return new AtomicBoolean(); }
        @Bean RetrievalReadinessController retrievalReadinessController(AtomicBoolean ready) {
            return new RetrievalReadinessController(() -> new ReadinessSnapshot(
                    ready.get(), ready.get() ? "ready" : "warming", Map.of()));
        }
    }
}
