package com.ssafy.simai.domain.report;

import com.ssafy.simai.global.properties.RiskEngineProperties;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.time.Duration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * EC2 Python Risk Engine 컨테이너(compose 내부망, 12.2) JSON 호출. TBM 보고서(S15P31A101-418)와 일일 일지(S15P31A101-33)가
 * 같은 주소·타임아웃을 쓴다. 응답이 2xx JSON 객체가 아니면 {@link EngineCallException}, 읽기 타임아웃은 {@link HttpTimeoutException}.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RiskEngineReportClient {

    private static final Duration CONNECT_TIMEOUT = Duration.ofSeconds(5);

    private final ObjectMapper mapper;
    private final HttpClient httpClient = HttpClient.newBuilder().connectTimeout(CONNECT_TIMEOUT).build();
    private volatile String baseUrl;
    private volatile Duration timeout;

    RiskEngineReportClient(ObjectMapper mapper, RiskEngineProperties properties) {
        this.mapper = mapper;
        this.baseUrl = properties.baseUrl();
        this.timeout = properties.timeout();
    }

    /** 시험에서 가짜 Risk Engine 주소·타임아웃을 바꿀 때 쓴다. */
    void configure(String baseUrl, Duration timeout) {
        this.baseUrl = baseUrl.strip().replaceAll("/+$", "");
        this.timeout = timeout;
    }

    JsonNode postJson(String path, ObjectNode body) throws HttpTimeoutException, EngineCallException {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + path))
                .timeout(timeout)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
                .build();
        try {
            HttpResponse<String> res = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
            if (res.statusCode() < 200 || res.statusCode() >= 300) {
                throw new EngineCallException("HTTP " + res.statusCode());
            }
            JsonNode parsed = mapper.readTree(res.body());
            if (parsed == null || !parsed.isObject()) {
                throw new EngineCallException("Non-object JSON response");
            }
            return parsed;
        } catch (HttpTimeoutException e) {
            throw e;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new EngineCallException("Interrupted", e);
        } catch (IOException | RuntimeException e) {
            throw new EngineCallException("Engine request failed", e);
        }
    }

    static final class EngineCallException extends Exception {
        EngineCallException(String message) {
            super(message);
        }

        EngineCallException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
