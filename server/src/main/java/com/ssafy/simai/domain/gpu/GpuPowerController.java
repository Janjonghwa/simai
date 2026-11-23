package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.domain.gpu.provider.GpuProviders;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 켜기·끄기·GPU 설정 API(S15P31A101-702, 관리자만, 계약 밖 관리 REST). 상태는 G1 GET /api/gpu/sessions/current 에 더한 필드
 * (launch·power·stop_block)로 본다.
 * <pre>
 * GET  /api/gpu/settings   → {settings, providers: [{id, configured, default}]}  지금 GPU 설정(없으면 서버 기본값, version_id null).
 *                           default = 켜기 기본 공급자(AWS 준비됐으면 AWS, 아니면 RunPod, S15P31A101-841)
 * POST /api/gpu/settings   {provider, preferred_gpu_types, preferred_regions, cloud_type, interruptible, max_price_per_hour_usd,
 *                           auto_stop_idle_minutes, runpod_template_id} → 201 settings  새 버전 추가(덮어쓰지 않음)
 * GET  /api/gpu/offers?provider=RUNPOD|AWS → {provider, gpu_types, regions, cells: [{gpu_type, region, stock, price_per_hour_usd, within_cap}], default}
 *                           provider 가 없으면 기본 공급자. 설정 공급자가 아니면 그 공급자의 서버 기본 선호로 기본 칸을 고른다(S15P31A101-841)
 * POST /api/gpu/launches   {provider?, gpu_type?, region?} → 201 launch     provider 가 없으면 기본 공급자, gpu_type·region 이 없으면
 *                           선호 순서 × 재고 × 가격 상한으로 고른다. 이미 켜져 있으면 공급자와 무관하게 409 GPU_ALREADY_ON
 * POST /api/gpu/stop       → 204                                관제(LIVE)·세팅 중이면 409 GPU_STOP_BLOCKED_LIVE·_SETUP
 * POST /api/gpu/presence   → 204                                관리자 화면 조작 신호(S15P31A101-708, 마지막 시각만 메모리에 둔다)
 * </pre>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
@PreAuthorize("hasRole('ADMIN')")
class GpuPowerController {

    private final GpuPowerService power;
    private final GpuSettingsService settings;
    private final GpuProviders providers;
    private final ObjectMapper mapper;

    GpuPowerController(GpuPowerService power, GpuSettingsService settings, GpuProviders providers, ObjectMapper mapper) {
        this.power = power;
        this.settings = settings;
        this.providers = providers;
        this.mapper = mapper;
    }

    @GetMapping("/api/gpu/settings")
    ObjectNode settings() {
        ObjectNode body = mapper.createObjectNode();
        body.set("settings", settings.view(settings.current()));
        ArrayNode list = body.putArray("providers");
        String defaultProvider = power.defaultProvider();
        for (String id : GpuSettingsService.PROVIDERS.stream().sorted((a, b) -> "RUNPOD".equals(a) ? -1 : "RUNPOD".equals(b) ? 1 : 0)
                .toList()) {
            ObjectNode row = list.addObject();
            row.put("id", id);
            row.put("configured", providers.find(id).map(p -> p.configured()).orElse(false));
            row.put("default", id.equals(defaultProvider));
        }
        return body;
    }

    @PostMapping("/api/gpu/settings")
    @ResponseStatus(HttpStatus.CREATED)
    ObjectNode saveSettings(@RequestBody(required = false) JsonNode body, @AuthenticationPrincipal Jwt jwt) {
        return settings.view(settings.save(body, userId(jwt)));
    }

    @GetMapping("/api/gpu/offers")
    ObjectNode offers(@RequestParam(name = "provider", required = false) String provider) {
        return power.offers(provider == null || provider.isBlank() ? null : provider.strip());
    }

    @PostMapping("/api/gpu/launches")
    @ResponseStatus(HttpStatus.CREATED)
    ObjectNode launch(@RequestBody(required = false) JsonNode body, @AuthenticationPrincipal Jwt jwt) {
        String provider = text(body, "provider");
        String gpuType = text(body, "gpu_type");
        String region = text(body, "region");
        return power.launch(userId(jwt), provider, gpuType, region);
    }

    @PostMapping("/api/gpu/stop")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void stop(@AuthenticationPrincipal Jwt jwt) {
        power.stop(userId(jwt));
    }

    @PostMapping("/api/gpu/presence")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void presence() {
        power.presence();
    }

    private static String text(JsonNode body, String field) {
        if (body == null || body.isNull()) {
            return null;
        }
        if (!body.isObject()) {
            throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_SETTINGS);
        }
        JsonNode node = body.get(field);
        if (node == null || node.isNull()) {
            return null;
        }
        if (!node.isString()) {
            throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_SETTINGS);
        }
        String value = node.stringValue().strip();
        return value.isEmpty() ? null : value;
    }

    private static Long userId(Jwt jwt) {
        try {
            return jwt == null ? null : Long.parseLong(jwt.getSubject());
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
