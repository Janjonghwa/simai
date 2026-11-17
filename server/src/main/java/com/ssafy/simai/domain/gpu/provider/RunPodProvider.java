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
 * <li>Pod(REST v2, S15P31A101-781): {@code POST https://api.runpod.io/v2/pods}(name·templateId·gpu{id, count}·cloud·dataCenterIds·env
 * → 201 Pod {id, status, cost}), {@code DELETE /pods/{id}}(지우기, 과금 멈춤), {@code GET /pods/{id}}(status PROVISIONING·STARTING·RUNNING·
 * EXITED·ERROR·TERMINATED). REST v1(rest.runpod.io/v1)은 2026-11-15 에 닫혔다. v2 에는 스팟(interruptible)이 없고 GPU 종류는 하나만 준다.
 * 오류 본문은 RFC 9457({title, status, detail})이다.</li>
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
            if (dataCenters == null || !dataCenters.isArray() || dataCenters.isEmpty()) {
                // 어디에도 없다(문서: 없으면 빠진다). 카탈로그 행으로만 남긴다(지역 null·NONE, 켜기 패널의 모든 GPU 행, S15P31A101-705)
                offers.add(new GpuOffer(id, name, memory, null, null, "NONE", price));
                continue;
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
        if (spec.interruptible()) {
            // v2 는 스팟 Pod 를 없앴다. 온디맨드로 바꿔 켜면 사용자가 고른 가격 조건과 달라지므로 켜지 않는다
            throw new GpuProviderException("REJECTED", "RunPod 는 스팟(interruptible) Pod 를 더는 만들지 않는다. GPU 설정에서 스팟을 끈다");
        }
        if (spec.templateId() == null) {
            // v2 는 템플릿이 없으면 image 가 필수다. 이미지·포트·디스크는 템플릿에만 있다
            throw new GpuProviderException("NOT_CONFIGURED", "RunPod 템플릿 ID 가 설정되지 않았다");
        }
        ObjectNode body = mapper.createObjectNode();
        body.put("name", spec.name());
        body.put("templateId", spec.templateId());
        ObjectNode gpu = body.putObject("gpu");
        gpu.put("id", spec.gpuType());
        gpu.put("count", spec.gpuCount());
        body.put("cloud", "COMMUNITY".equals(spec.cloudType()) ? "COMMUNITY" : "SECURE");
        body.putArray("dataCenterIds").add(spec.region());
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
        BigDecimal cost = pod.hasNonNull("cost") && pod.get("cost").isNumber() ? pod.get("cost").decimalValue() : null;
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
        String status = text(pod, "status");
        if (status == null) {
            return InstanceState.UNKNOWN;
        }
        // PROVISIONING·STARTING 은 아직 켜는 중, ERROR 는 RunPod 쪽 실패(과금 여부를 모른다)라 모름으로 둔다. 지운 것으로 보지 않는다
        if ("ERROR".equals(status)) {
            log.warn("RunPod Pod {} 이 ERROR 상태다. RunPod 화면에서 확인한다", instanceId);
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
            // 재고 없음은 배치 불가(400)·대기열 가득(409)에서만 본다. 422(본문 형식)·402(잔액) 등은 다른 실패다
            if ((res.statusCode() == 400 || res.statusCode() == 409) && outOfStock(res.body())) {
                log.warn("RunPod {} 거절: 재고 없음(HTTP {})", what, res.statusCode());
                throw new GpuProviderException("OUT_OF_STOCK", "RunPod 에 그 GPU·지역 재고가 없다");
            }
            // v2 오류(RFC 9457)의 title 만 남긴다(짧은 분류 문구, 요청 값·비밀 없음). detail·본문은 남기지 않는다
            String title = errorTitle(res.body());
            if (title != null) {
                log.warn("RunPod {} 거절 사유: {}", what, title);
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

    /**
     * 재고 없음 거절 문구. v1: "There are no longer any instances available ...", "no instances available".
     * v2(RFC 9457 detail): 400 "this GPU/data center combination could not be placed", 용량 부족(capacity) 문구.
     */
    private static boolean outOfStock(String body) {
        if (body == null) {
            return false;
        }
        String lower = body.toLowerCase(Locale.ROOT);
        return lower.contains("no longer any instances") || lower.contains("no instances available")
                || lower.contains("not enough") && lower.contains("available")
                || lower.contains("could not be placed") || lower.contains("insufficient capacity")
                || lower.contains("no capacity");
    }

    private String errorTitle(String body) {
        try {
            JsonNode node = body == null ? null : mapper.readTree(body);
            String title = node == null || !node.isObject() ? null : text(node, "title");
            return title == null ? null : title.replaceAll("[\\r\\n]", " ").substring(0, Math.min(80, title.length()));
        } catch (RuntimeException e) {
            return null;
        }
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
