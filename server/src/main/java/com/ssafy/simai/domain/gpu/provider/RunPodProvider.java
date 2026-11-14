package com.ssafy.simai.domain.gpu.provider;

import com.ssafy.simai.global.properties.GpuProperties;
import java.io.IOException;
import java.math.BigDecimal;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * RunPod 공급자(S15P31A101-702). 공식 문서(2026-11 확인)의 두 API 를 쓴다.
 * <ul>
 * <li>재고·가격: {@code GET https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD&count=1&cloud=SECURE|COMMUNITY}
 * → {@code gpus[]: {id, name, memory, price: {secure, community}, availability, dataCenters: [{id, name, availability}]}}.
 * 가격은 GPU 1대 시간당 USD 온디맨드다(스팟 가격은 이 목록에 없다).</li>
 * <li>Pod: {@code POST https://rest.runpod.io/v1/pods}(templateId·gpuTypeIds·gpuCount·dataCenterIds·cloudType·interruptible·env,
 * 우선순위 custom), {@code DELETE /pods/{id}}(지우기, 과금 멈춤), {@code GET /pods/{id}}(desiredStatus RUNNING·EXITED·TERMINATED).</li>
 * </ul>
 * 인증은 {@code Authorization: Bearer <SIMAI_RUNPOD_API_KEY>}. 키·요청 환경 변수·응답 본문은 로그와 예외 문구에 싣지 않는다(상태 코드만).
 * 비밀 환경 변수는 RunPod Secrets 참조 {@code {{ RUNPOD_SECRET_이름 }}} 글자로만 넣는다(서버는 원문을 모른다).
 */
@Component
public class RunPodProvider implements GpuProvider {

    public static final String ID = "RUNPOD";
    private static final Logger log = LoggerFactory.getLogger(RunPodProvider.class);
    private static final Duration CONNECT_TIMEOUT = Duration.ofSeconds(5);

    private final ObjectMapper mapper;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(CONNECT_TIMEOUT).build();
    private volatile String apiKey;
    private volatile String restBaseUrl;
    private volatile String catalogBaseUrl;
    private final Duration timeout;

    public RunPodProvider(ObjectMapper mapper, GpuProperties properties) {
        this.mapper = mapper;
        GpuProperties.RunPod runpod = properties.runpod();
        this.apiKey = runpod.apiKey();
        this.restBaseUrl = runpod.restBaseUrl();
        this.catalogBaseUrl = runpod.catalogBaseUrl();
        this.timeout = runpod.timeout();
    }

    /** 시험에서 가짜 RunPod 서버를 가리킬 때만 쓴다(운영은 설정값). */
    public void useEndpoints(String restBaseUrl, String catalogBaseUrl, String apiKey) {
        this.restBaseUrl = restBaseUrl.replaceAll("/+$", "");
        this.catalogBaseUrl = catalogBaseUrl.replaceAll("/+$", "");
        this.apiKey = apiKey == null ? "" : apiKey;
    }

    @Override
    public String id() {
        return ID;
    }

    @Override
    public boolean configured() {
        return !apiKey.isEmpty();
    }

    @Override
    public List<GpuOffer> offers(OfferQuery query) throws GpuProviderException {
        String cloud = "COMMUNITY".equals(query.cloudType()) ? "COMMUNITY" : "SECURE";
        String url = catalogBaseUrl + "/v2/catalog/gpus?include=AVAILABILITY&product=POD&count=" + Math.max(1, query.gpuCount())
                + "&cloud=" + URLEncoder.encode(cloud, StandardCharsets.UTF_8);
        JsonNode body = send(HttpRequest.newBuilder(URI.create(url)).GET(), "재고 조회");
        JsonNode gpus = body.get("gpus");
        if (gpus == null || !gpus.isArray()) {
            throw new GpuProviderException("BAD_RESPONSE", "RunPod 재고 응답에 gpus 가 없다");
        }
        String priceKey = cloud.toLowerCase(Locale.ROOT);
        List<GpuOffer> offers = new ArrayList<>();
        for (JsonNode gpu : gpus) {
            String id = text(gpu, "id");
            if (id == null) {
                continue;
            }
            String name = text(gpu, "name");
            Integer memory = gpu.hasNonNull("memory") && gpu.get("memory").isNumber() ? gpu.get("memory").intValue() : null;
            BigDecimal price = null;
            JsonNode priceNode = gpu.path("price").get(priceKey);
            if (priceNode != null && priceNode.isNumber() && priceNode.decimalValue().signum() > 0) {
                price = priceNode.decimalValue().multiply(BigDecimal.valueOf(Math.max(1, query.gpuCount())));
            }
            JsonNode dataCenters = gpu.get("dataCenters");
            if (dataCenters == null || !dataCenters.isArray()) {
                continue; // 어디에도 없다(문서: 없으면 빠진다). 서비스가 선호 칸을 NONE 으로 채운다
            }
            for (JsonNode dc : dataCenters) {
                String region = text(dc, "id");
                if (region == null) {
                    continue;
                }
                offers.add(new GpuOffer(id, name, memory, region, text(dc, "name"), stock(text(dc, "availability")), price));
            }
        }
        return offers;
    }

    @Override
    public CreatedInstance create(LaunchSpec spec) throws GpuProviderException {
        ObjectNode body = mapper.createObjectNode();
        body.put("name", spec.name());
        if (spec.templateId() != null) {
            body.put("templateId", spec.templateId());
        }
        body.put("computeType", "GPU");
        body.put("cloudType", spec.cloudType());
        body.putArray("gpuTypeIds").add(spec.gpuType());
        body.put("gpuTypePriority", "custom");
        body.put("gpuCount", spec.gpuCount());
        body.putArray("dataCenterIds").add(spec.region());
        body.put("dataCenterPriority", "custom");
        body.put("interruptible", spec.interruptible());
        ObjectNode env = body.putObject("env");
        spec.plainEnv().forEach(env::put);
        for (Map.Entry<String, String> secret : spec.secretEnv().entrySet()) {
            env.put(secret.getKey(), "{{ RUNPOD_SECRET_" + secret.getValue() + " }}");
        }
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(restBaseUrl + "/pods"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)));
        JsonNode pod = send(request, "Pod 만들기");
        String id = text(pod, "id");
        if (id == null || id.isBlank()) {
            throw new GpuProviderException("BAD_RESPONSE", "RunPod Pod 만들기 응답에 id 가 없다");
        }
        BigDecimal cost = pod.hasNonNull("costPerHr") && pod.get("costPerHr").isNumber() ? pod.get("costPerHr").decimalValue() : null;
        return new CreatedInstance(id, cost);
    }

    @Override
    public void terminate(String instanceId) throws GpuProviderException {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(restBaseUrl + "/pods/" + path(instanceId))).DELETE();
        HttpResponse<String> res = exchange(request, "Pod 지우기");
        if (res.statusCode() == 404 || (res.statusCode() >= 200 && res.statusCode() < 300)) {
            return; // 이미 없음도 같은 결과
        }
        throw failure(res.statusCode(), "Pod 지우기");
    }

    @Override
    public InstanceState state(String instanceId) throws GpuProviderException {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(restBaseUrl + "/pods/" + path(instanceId))).GET();
        HttpResponse<String> res = exchange(request, "Pod 조회");
        if (res.statusCode() == 404) {
            return InstanceState.GONE;
        }
        if (res.statusCode() < 200 || res.statusCode() >= 300) {
            throw failure(res.statusCode(), "Pod 조회");
        }
        JsonNode pod = parse(res.body(), "Pod 조회");
        String status = text(pod, "desiredStatus");
        if (status == null) {
            return InstanceState.UNKNOWN;
        }
        return switch (status) {
            case "RUNNING" -> InstanceState.RUNNING;
            case "EXITED" -> InstanceState.STOPPED;
            case "TERMINATED" -> InstanceState.GONE;
            default -> InstanceState.UNKNOWN;
        };
    }

    private JsonNode send(HttpRequest.Builder request, String what) throws GpuProviderException {
        HttpResponse<String> res = exchange(request, what);
        if (res.statusCode() < 200 || res.statusCode() >= 300) {
            if (res.statusCode() >= 400 && res.statusCode() < 500 && outOfStock(res.body())) {
                log.warn("RunPod {} 거절: 재고 없음(HTTP {})", what, res.statusCode());
                throw new GpuProviderException("OUT_OF_STOCK", "RunPod 에 그 GPU·지역 재고가 없다");
            }
            throw failure(res.statusCode(), what);
        }
        return parse(res.body(), what);
    }

    private HttpResponse<String> exchange(HttpRequest.Builder request, String what) throws GpuProviderException {
        if (!configured()) {
            throw new GpuProviderException("NOT_CONFIGURED", "RunPod API 키(SIMAI_RUNPOD_API_KEY)가 설정되지 않았다");
        }
        HttpRequest built = request.timeout(timeout).header("Authorization", "Bearer " + apiKey).header("Accept", "application/json")
                .build();
        try {
            return http.send(built, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (HttpTimeoutException e) {
            log.warn("RunPod {} 시간 초과", what);
            throw new GpuProviderException("UNAVAILABLE", "RunPod " + what + " 시간 초과");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new GpuProviderException("UNAVAILABLE", "RunPod " + what + " 중단");
        } catch (IOException e) {
            log.warn("RunPod {} 연결 실패: {}", what, e.getClass().getSimpleName());
            throw new GpuProviderException("UNAVAILABLE", "RunPod " + what + " 연결 실패");
        }
    }

    private GpuProviderException failure(int status, String what) {
        log.warn("RunPod {} 실패: HTTP {}", what, status);
        String code = status >= 500 || status == 429 ? "UNAVAILABLE" : "REJECTED";
        return new GpuProviderException(code, "RunPod " + what + " 실패(HTTP " + status + ")");
    }

    private JsonNode parse(String body, String what) throws GpuProviderException {
        try {
            JsonNode node = mapper.readTree(body);
            if (node == null || !node.isObject()) {
                throw new GpuProviderException("BAD_RESPONSE", "RunPod " + what + " 응답이 JSON 객체가 아니다");
            }
            return node;
        } catch (RuntimeException e) {
            throw new GpuProviderException("BAD_RESPONSE", "RunPod " + what + " 응답이 JSON 이 아니다");
        }
    }

    /** 재고 없음 거절 문구(RunPod: "There are no longer any instances available ...", "no instances available") */
    private static boolean outOfStock(String body) {
        if (body == null) {
            return false;
        }
        String lower = body.toLowerCase(Locale.ROOT);
        return lower.contains("no longer any instances") || lower.contains("no instances available")
                || lower.contains("not enough") && lower.contains("available");
    }

    private static String stock(String availability) {
        if (availability == null) {
            return "NONE";
        }
        String upper = availability.toUpperCase(Locale.ROOT);
        return switch (upper) {
            case "HIGH", "MEDIUM", "LOW" -> upper;
            default -> "NONE";
        };
    }

    private static String path(String instanceId) {
        return URLEncoder.encode(instanceId, StandardCharsets.UTF_8);
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value == null || value.isNull() || !value.isString() ? null : value.stringValue();
    }
}
