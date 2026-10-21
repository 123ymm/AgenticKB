package com.coremasterkb.serving.api;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.web.servlet.handler.SimpleUrlHandlerMapping;

import java.util.Map;

/** Actuator's health wildcard precedes MVC controllers; override this exact URL only. */
@Configuration(proxyBeanMethods = false)
public class RetrievalReadinessRoutes {
    @Bean
    public SimpleUrlHandlerMapping retrievalReadinessRoute(RetrievalReadinessController controller)
            throws NoSuchMethodException {
        var handler = new HandlerMethod(controller, "readiness");
        var mapping = new SimpleUrlHandlerMapping(Map.of("/actuator/health/readiness", handler), -101);
        mapping.setInterceptors(new HandlerInterceptor() {
            @Override public boolean preHandle(HttpServletRequest request, HttpServletResponse response,
                                               Object target) {
                if ("GET".equals(request.getMethod()) || "HEAD".equals(request.getMethod())) return true;
                response.setStatus(405);
                response.setHeader("Allow", "GET, HEAD");
                return false;
            }
        });
        return mapping;
    }
}
