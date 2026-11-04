package com.ssafy.simai.domain.coverage;

import com.ssafy.simai.domain.calibration.CalibrationViews;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 1단계 카메라 각도 헬퍼 중계 서비스(S15P31A101-447, 명세 4.3·4.5·12.2·12.6·13.4·18.1).
 *
 * <ul>
 *   <li>게이트웨이 별도 CPU 프로세스가 보내는 {@code POST /api/devices/coverage-guides} 요청을
 *       {@code contracts/messages/coverage_guide.schema.json} 1.0.0 및 의미 불변식으로 검증하고,
 *       현장·기기 귀속({@code device.kind = 'LAPTOP'}, {@code source_id == deviceId})을 확인한 뒤
 *       메모리에 최신 결과 1건만 보관한다(DB 에 쌓지 않는다).</li>
 *   <li>관리자가 {@code GET /api/cameras/coverage-guides} 를 부르면 최신 결과를 돌려주되,
 *       수신 후 {@code staleAfter}(초기 10 s)가 지났으면 {@code status = "STALE"}({@code stale = true})로 표시하고
 *       마지막 관리자 조회 시각을 기록한다.</li>
 *   <li>기기 POST 응답에는 최근 {@code demandTtl}(초기 15 s) 안에 관리자 조회가 있었는지 나타내는
 *       {@code active_demand} 를 실어 게이트웨이 헬퍼(-446)가 계산을 켜고 끄게 한다.</li>
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CoverageGuideService {

    private static final String SCHEMA_NAME = "messages/coverage_guide";

    private final ContractSchemas schemas;
    private final NamedParameterJdbcTemplate jdbc;
    private final SiteLookup sites;
    private final CalibrationViews calibrationViews;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final CoverageGuideProperties properties;

    private final AtomicReference<StoredGuide> latest = new AtomicReference<>();
    private final AtomicReference<Instant> lastAdminQueryAt = new AtomicReference<>();

    public CoverageGuideService(
            ContractSchemas schemas,
            NamedParameterJdbcTemplate jdbc,
            SiteLookup sites,
            CalibrationViews calibrationViews,
            ObjectMapper mapper,
            Clock clock,
            CoverageGuideProperties properties) {
        this.schemas = schemas;
        this.jdbc = jdbc;
        this.sites = sites;
        this.calibrationViews = calibrationViews;
        this.mapper = mapper;
        this.clock = clock;
        this.properties = properties;
    }

    /** 게이트웨이 기기가 올린 최신 카메라 각도 안내를 검증·보관하고 수요 신호를 돌려준다. */
    public ObjectNode ingest(JsonNode body, String deviceId) {
        if (!"LAPTOP".equals(deviceKindOf(deviceId))) {
            throw new BusinessException(ErrorCode.CoverageGuide.NOT_A_LAPTOP);
        }
        String siteId = sites.siteId();
        validateContractAndSemantics(body);

        String sourceId = body.path("source_id").asString(null);
        if (!deviceId.equals(sourceId)) {
            throw new BusinessException(ErrorCode.CoverageGuide.COVERAGE_GUIDE_SOURCE_MISMATCH);
        }

        Instant now = clock.instant();
        // 영상 없음·쌍 결과 없음(UNKNOWN)뿐인 빈 보고는 이미 실제 쌍 결과가 있으면 최신본을 덮지 않는다(화면이 실제 결과와 "영상 없음" 사이를
        // 깜박이는 것을 막는다). 실제 결과는 staleAfter 가 지나면 STALE 로 표시된다.
        StoredGuide previous = latest.get();
        if (!(previous != null && !hasRealPairResult(body) && hasRealPairResult(previous.payload()))) {
            latest.set(new StoredGuide(((ObjectNode) body).deepCopy(), deviceId, siteId, now));
        }
        return buildPostResponse(siteId, deviceId, now);
    }

    /** 기기가 수요만 묻는다(GET /api/devices/coverage-guides). 최신 결과를 건드리거나 보관하지 않는다. */
    public ObjectNode demandFor(String deviceId) {
        if (!"LAPTOP".equals(deviceKindOf(deviceId))) {
            throw new BusinessException(ErrorCode.CoverageGuide.NOT_A_LAPTOP);
        }
        ObjectNode res = buildPostResponse(sites.siteId(), deviceId, clock.instant());
        res.remove("received_at_ms");
        return res;
    }

    private static boolean hasRealPairResult(JsonNode payload) {
        for (JsonNode pair : payload.path("pairs")) {
            if (!"UNKNOWN".equals(pair.path("grade").asString(""))) {
                return true;
            }
        }
        return false;
    }

    /** 관리자가 최신 카메라 각도 안내를 조회한다. 마지막 관리자 조회 시각을 갱신해 수요 신호를 켠다. */
    public ObjectNode queryForAdmin() {
        String siteId = sites.siteId();
        Instant now = clock.instant();
        lastAdminQueryAt.set(now);

        ObjectNode node = calibrationViews.coverageGuides();
        node.put("site_id", siteId);
        node.put("active_demand", true);
        node.put("demand_ttl_ms", properties.demandTtl().toMillis());
        node.put("last_admin_query_at_ms", now.toEpochMilli());
        node.put("demand_until_ms", now.plus(properties.demandTtl()).toEpochMilli());
        node.put("stale_after_ms", properties.staleAfter().toMillis());

        StoredGuide stored = latest.get();
        if (stored == null) {
            node.put("status", "EMPTY");
            node.put("stale", false);
            node.putNull("received_at_ms");
            node.putNull("age_ms");
            node.putNull("latest");
            return node;
        }

        Duration age = Duration.between(stored.receivedAt(), now);
        long ageMs = Math.max(0L, age.toMillis());
        boolean stale = age.compareTo(properties.staleAfter()) > 0;

        node.put("status", stale ? "STALE" : "OK");
        node.put("stale", stale);
        node.put("received_at_ms", stored.receivedAt().toEpochMilli());
        node.put("age_ms", ageMs);
        node.set("latest", stored.payload().deepCopy());
        return node;
    }

    /** 시험에서 메모리 상태를 초기화할 때 쓴다. */
    public void clearForTest() {
        latest.set(null);
        lastAdminQueryAt.set(null);
    }

    private ObjectNode buildPostResponse(String siteId, String deviceId, Instant now) {
        Instant lastQuery = lastAdminQueryAt.get();
        boolean activeDemand = isDemandActive(lastQuery, now);

        ObjectNode res = mapper.createObjectNode();
        res.put("site_id", siteId);
        res.put("source_id", deviceId);
        res.put("received_at_ms", now.toEpochMilli());
        res.put("active_demand", activeDemand);
        res.put("demand_ttl_ms", properties.demandTtl().toMillis());
        if (lastQuery == null) {
            res.putNull("last_admin_query_at_ms");
            res.putNull("demand_until_ms");
        } else {
            res.put("last_admin_query_at_ms", lastQuery.toEpochMilli());
            res.put("demand_until_ms", lastQuery.plus(properties.demandTtl()).toEpochMilli());
        }
        return res;
    }

    private boolean isDemandActive(Instant lastQuery, Instant now) {
        if (lastQuery == null) {
            return false;
        }
        Duration elapsed = Duration.between(lastQuery, now);
        return elapsed.compareTo(properties.demandTtl()) <= 0;
    }

    private String deviceKindOf(String deviceId) {
        if (deviceId == null || deviceId.isBlank()) {
            return null;
        }
        return jdbc.queryForList(
                "SELECT kind FROM device WHERE device_id = :id AND revoked_at IS NULL",
                Map.of("id", deviceId),
                String.class).stream().findFirst().orElse(null);
    }

    private void validateContractAndSemantics(JsonNode body) {
        if (body == null || !body.isObject()) {
            throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
        }
        String rawJson = mapper.writeValueAsString(body);
        if (!schemas.violations(SCHEMA_NAME, rawJson).isEmpty()) {
            throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
        }

        String schemaVersion = body.path("schema_version").asString("");
        if (!schemaVersion.startsWith("1.")) {
            throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
        }

        long computedAtMs = body.path("computed_at_ms").asLong(-1L);
        long validUntilMs = body.path("valid_until_ms").asLong(-1L);
        if (computedAtMs < 0 || validUntilMs < computedAtMs) {
            throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
        }

        Set<String> cameraIds = new HashSet<>();
        for (JsonNode cam : body.path("cameras")) {
            String camId = cam.path("camera_id").asString("");
            if (camId.isEmpty() || !cameraIds.add(camId)) {
                throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
            }
        }
        if (cameraIds.size() != 4) {
            throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
        }

        Set<String> pairKeys = new HashSet<>();
        for (JsonNode pair : body.path("pairs")) {
            String a = pair.path("a").asString("");
            String b = pair.path("b").asString("");
            if (!cameraIds.contains(a) || !cameraIds.contains(b) || a.equals(b)) {
                throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
            }
            String canonical = a.compareTo(b) < 0 ? a + "|" + b : b + "|" + a;
            if (!pairKeys.add(canonical)) {
                throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
            }
        }
        if (pairKeys.size() != 6) {
            throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
        }

        for (JsonNode comp : body.path("components")) {
            for (JsonNode member : comp) {
                if (!cameraIds.contains(member.asString(""))) {
                    throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
                }
            }
        }

        for (JsonNode guide : body.path("guides")) {
            String camId = guide.path("camera_id").asString("");
            if (!cameraIds.contains(camId)) {
                throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
            }
            JsonNode targetPair = guide.get("target_pair");
            if (targetPair != null && !targetPair.isNull()) {
                String p0 = targetPair.path(0).asString("");
                String p1 = targetPair.path(1).asString("");
                if (!cameraIds.contains(p0) || !cameraIds.contains(p1) || p0.equals(p1)) {
                    throw new BusinessException(ErrorCode.CoverageGuide.INVALID_COVERAGE_GUIDE);
                }
            }
        }
    }

    private record StoredGuide(ObjectNode payload, String sourceId, String siteId, Instant receivedAt) {
    }
}
