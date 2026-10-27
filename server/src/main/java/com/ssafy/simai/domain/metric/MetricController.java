package com.ssafy.simai.domain.metric;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 시스템 지표 조회(관리자, 명세 12.6 GET /api/metrics, 3.11, S15P31A101-70). 상태 패널·G0·G6 측정이 system_metric 을 계열(출처·지표·카메라)
 * 별 시간 순서로 읽는다. latest=true 면 계열마다 가장 늦은 한 점만 준다(상태 패널).
 * <pre>
 * GET /api/metrics?metric=pipeline_lag_ms,fps&amp;source_id=tier0-01&amp;camera_id=cam_A&amp;from=ms&amp;to=ms&amp;latest=false
 * </pre>
 * <ul>
 * <li>기간 기본은 최근 10분(latest 는 1시간: 잠깐 끊긴 출처도 마지막 값과 그 시각이 남아 화면이 '오래된 값'으로 보이게), 최대 24시간.</li>
 * <li>행이 상한(50,000)을 넘으면 늦은 점부터 상한까지만 주고 truncated=true 로 알린다. 계열은 빠지지 않고 오래된 점이 빠진다.</li>
 * <li>빈 문자열 필터는 필터 없음이다. 카메라와 무관한 지표는 응답에서 camera_id null 이다.</li>
 * </ul>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class MetricController {

    private static final Duration DEFAULT_WINDOW = Duration.ofMinutes(10);
    private static final Duration DEFAULT_LATEST_WINDOW = Duration.ofHours(1);
    private static final Duration MAX_WINDOW = Duration.ofHours(24);
    /** 카메라 4대 1 Hz 면 health 하나에 지표 59개, 10분이 약 35,400행이다 */
    private static final int MAX_ROWS = 50_000;
    /** PostgreSQL timestamptz 가 받는 범위 안(9999-12-31) */
    private static final long MAX_EPOCH_MS = 253_402_300_799_999L;

    private final SystemMetricRepository metrics;
    private final ObjectMapper mapper;
    private final Clock clock;

    MetricController(SystemMetricRepository metrics, ObjectMapper mapper, Clock clock) {
        this.metrics = metrics;
        this.mapper = mapper;
        this.clock = clock;
    }

    @GetMapping("/api/metrics")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode metrics(@RequestParam(name = "metric", required = false) String metric,
            @RequestParam(name = "source_id", required = false) String sourceId,
            @RequestParam(name = "camera_id", required = false) String cameraId,
            @RequestParam(name = "from", required = false) String fromMs,
            @RequestParam(name = "to", required = false) String toMs,
            @RequestParam(name = "latest", defaultValue = "false") boolean latest) {
        Instant to = blankToNull(toMs) == null ? clock.instant() : epochMs(toMs);
        Instant from = blankToNull(fromMs) == null ? to.minus(latest ? DEFAULT_LATEST_WINDOW : DEFAULT_WINDOW) : epochMs(fromMs);
        if (from.isAfter(to) || Duration.between(from, to).compareTo(MAX_WINDOW) > 0) {
            throw new BusinessException(ErrorCode.Metric.INVALID_METRIC_QUERY);
        }
        List<String> names = metric == null ? List.of()
                : Arrays.stream(metric.split(",")).map(String::strip).filter(s -> !s.isEmpty()).distinct().toList();
        List<Map<String, Object>> rows = metrics.query(from, to, names, blankToNull(sourceId), blankToNull(cameraId), latest,
                MAX_ROWS + 1);
        boolean truncated = rows.size() > MAX_ROWS;
        // 행은 늦은 시각부터 온다. 계열(출처·지표·카메라 순)로 묶고 계열 안은 시간 순으로 되돌린다
        Map<List<String>, List<Map<String, Object>>> grouped = new TreeMap<>((a, b) -> {
            for (int i = 0; i < a.size(); i++) {
                int c = a.get(i).compareTo(b.get(i));
                if (c != 0) {
                    return c;
                }
            }
            return 0;
        });
        for (Map<String, Object> row : truncated ? rows.subList(0, MAX_ROWS) : rows) {
            grouped.computeIfAbsent(List.of((String) row.get("source_id"), (String) row.get("metric"), (String) row.get("camera_id")),
                    k -> new ArrayList<>()).add(row);
        }
        ObjectNode body = mapper.createObjectNode();
        body.put("from", from.toEpochMilli());
        body.put("to", to.toEpochMilli());
        body.put("truncated", truncated);
        ArrayNode series = body.putArray("series");
        grouped.forEach((key, points) -> {
            ObjectNode current = series.addObject();
            current.put("source_id", key.get(0));
            current.put("metric", key.get(1));
            // 카메라와 무관한 지표는 빈 문자열로 저장한다. 응답에서는 null 로 준다
            current.put("camera_id", key.get(2).isEmpty() ? null : key.get(2));
            current.put("unit", (String) points.getFirst().get("unit"));
            ArrayNode array = current.putArray("points");
            for (Map<String, Object> row : points.reversed()) {
                ObjectNode point = array.addObject();
                point.put("t", ((Timestamp) row.get("measured_at")).toInstant().toEpochMilli());
                point.put("value", ((Number) row.get("value")).doubleValue());
                String labels = (String) row.get("labels");
                if (labels != null) {
                    point.set("labels", mapper.readTree(labels));
                }
            }
        });
        return body;
    }

    /** 숫자가 아니거나 DB 가 받지 못하는 시각이면 400(코드 INVALID_METRIC_QUERY, 500·코드 없는 400 이 아니게) */
    private static Instant epochMs(String value) {
        try {
            long ms = Long.parseLong(value.strip());
            if (ms < 0 || ms > MAX_EPOCH_MS) {
                throw new BusinessException(ErrorCode.Metric.INVALID_METRIC_QUERY);
            }
            return Instant.ofEpochMilli(ms);
        } catch (NumberFormatException e) {
            throw new BusinessException(ErrorCode.Metric.INVALID_METRIC_QUERY);
        }
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value.strip();
    }
}
