package com.coremasterkb.serving.operator.api;

import com.coremasterkb.serving.config.ServingProperties;
import com.coremasterkb.serving.cachewarmup.RetrievalCacheWarmupService.CacheWarmingException;
import com.coremasterkb.serving.domainpack.DomainPoolManager;
import com.coremasterkb.serving.domainpack.DomainRegistry;
import com.coremasterkb.serving.domainpack.ServingConfigSnapshot;
import com.coremasterkb.serving.operator.core.ExecContext;
import com.coremasterkb.serving.operator.engine.ParadigmCompiler;
import com.coremasterkb.serving.operator.engine.ParadigmExecutor;
import com.coremasterkb.serving.operator.engine.ParadigmGraph;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class ParadigmExecutionWarmupTest {
    private final List<String> events = new ArrayList<>();
    private final DomainRegistry registry = new DomainRegistry();

    @Test
    void warmingStopsExecutionBeforeAnyOperatorRuns() {
        var warming = new CacheWarmingException("retrieval_cache_warming");
        var service = service(domain -> {
            events.add("warm:" + domain);
            throw warming;
        });

        assertThatThrownBy(() -> service.run(null, args("allowed")))
                .isSameAs(warming);
        assertThat(events).containsExactly("compile", "database:allowed", "warm:allowed");
    }

    @Test
    void everyRequestChecksTheResolvedDomainBeforeExecution() {
        var service = service(domain -> events.add("warm:" + domain));
        service.run(null, args(null));
        service.run(null, args("other"));

        assertThat(events).containsExactly("compile", "database:allowed", "warm:allowed", "execute:allowed",
                "compile", "database:other", "warm:other", "execute:other");
    }

    @Test
    void unknownDomainFailsBeforeDatabaseOrWarmup() {
        registry.apply(new ServingConfigSnapshot(Map.of("allowed",
                new ServingConfigSnapshot.DomainConfig("allowed", true, "prod", null))));
        var service = service(domain -> events.add("warm:" + domain));

        assertThatThrownBy(() -> service.run(null, args("unknown")))
                .isInstanceOf(IllegalArgumentException.class).hasMessage("unknown_domain");
        assertThat(events).isEmpty();
    }

    @Test
    void disabledDomainFailsBeforeDatabaseOrWarmup() {
        registry.apply(new ServingConfigSnapshot(Map.of("allowed",
                new ServingConfigSnapshot.DomainConfig("allowed", false, "prod", null))));
        var service = service(domain -> events.add("warm:" + domain));

        assertThatThrownBy(() -> service.run(null, args("allowed")))
                .isInstanceOf(IllegalArgumentException.class).hasMessage("domain_disabled");
        assertThat(events).isEmpty();
    }

    @Test
    void blankQueryDoesNotStartWarmup() {
        var service = service(domain -> events.add("warm:" + domain));
        assertThatThrownBy(() -> service.run(null,
                new ParadigmExecutionService.RunArgs(" ", "allowed", null, false)))
                .isInstanceOf(IllegalArgumentException.class).hasMessage("query_required");
        assertThat(events).isEmpty();
    }

    private ParadigmExecutionService service(Consumer<String> gate) {
        var compiler = new ParadigmCompiler(null) {
            @Override public ParadigmGraph compile(JsonNode json) {
                events.add("compile");
                return null;
            }
        };
        var executor = new ParadigmExecutor(null, null, Runnable::run) {
            @Override public Object execute(ParadigmGraph graph, ExecContext ctx, Map<String, Object> inputs) {
                events.add("execute:" + ctx.domain());
                return List.of();
            }
        };
        var pools = new DomainPoolManager(registry, null) {
            @Override public DataSource getDataSource(String domain) {
                events.add("database:" + domain);
                return null;
            }
        };
        return new ParadigmExecutionService(compiler, executor, registry, pools,
                new ServingProperties(null, null, "allowed", null, null, null, null), gate);
    }

    private static ParadigmExecutionService.RunArgs args(String domain) {
        return new ParadigmExecutionService.RunArgs("question", domain, null, false);
    }
}
