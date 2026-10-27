package com.ssafy.simai.domain.metric;

import com.ssafy.simai.domain.metric.SystemMetricRepository.Row;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.Tier0HealthStored;
import jakarta.annotation.PreDestroy;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * Tier 0 health(1 Hz, 계약 health) 의 수치를 system_metric 으로 푼다(명세 3.11, 6.6, S15P31A101-70). 원문은 system_health 에 그대로 있고,
 * 여기서는 구간별 지연·FPS·큐·손실·GPU 를 지표 하나씩 꺼내 G0·G6 측정과 상태 패널이 시간 축으로 읽게 한다.
 * <ul>
 * <li>시각은 health 를 저장한 것과 같은 봉투 sent_at(Tier 0 시계), 출처는 봉투 source_id(gateway_id)다.</li>
 * <li>백분위 지표(촬영 → 판정·프레임 경과·대기열 대기)는 값에 p95(지연 목표 기준, 6.6)를 두고 p50·p95·p99 를 labels 에 둔다.</li>
 * <li>값이 null 이거나 없는 칸은 쓰지 않는다(잴 수 없음과 0 을 섞지 않게).</li>
 * </ul>
 * 지표 저장은 전용 스레드에서 하고, 실패해도 health 수신은 그대로 둔다(경고만). 지표는 관측 보조이고 health 원문이 정본이다.
 */
@Component
class HealthMetrics {

    private static final Logger log = LoggerFactory.getLogger(HealthMetrics.class);
    /** 카메라와 무관한 지표의 camera_id(키에 NULL 을 쓰지 않는다, db-tables) */
    static final String NO_CAMERA = "";
    /** 카메라별 frames 집계에서 시간(ms)인 칸. 나머지는 프레임 수다 */
    private static final String GAP_MS = "camera_to_gateway_gap_max_ms";

    /** 1 Hz health 10분어치. 넘치면(DB 가 오래 막힘) 그 health 의 지표는 버린다 */
    private static final int QUEUE_CAPACITY = 600;

    private final SystemMetricRepository metrics;
    private final ObjectMapper mapper;
    /**
     * 지표 저장은 I1 수신 스레드 밖에서 한다. health 처리 안에서 동기로 쓰면 같은 연결의 world_state(LIVE)가 지표 배치 INSERT 를
     * 기다린다. 스레드 하나라 쓰는 순서는 받은 순서다.
     */
    private final ThreadPoolExecutor writer;

    HealthMetrics(SystemMetricRepository metrics, ObjectMapper mapper) {
        this.metrics = metrics;
        this.mapper = mapper;
        this.writer = new ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS, new ArrayBlockingQueue<>(QUEUE_CAPACITY), task -> {
            Thread thread = new Thread(task, "health-metrics");
            thread.setDaemon(true);
            return thread;
        }, (task, executor) -> log.warn("health 지표 대기열이 차서 이번 지표를 버린다"));
    }

    @EventListener
    void onHealth(Tier0HealthStored stored) {
        I1Envelope envelope = stored.envelope();
        writer.execute(() -> {
            try {
                metrics.insert(rows(envelope));
            } catch (RuntimeException e) {
                log.warn("health 지표를 system_metric 에 쓰지 못했다({}): {}", envelope.sourceId(), e.getMessage());
            }
        });
    }

    @PreDestroy
    void close() {
        writer.shutdown();
    }

    List<Row> rows(I1Envelope envelope) {
        Instant at = envelope.sentAt();
        String source = envelope.sourceId();
        JsonNode p = envelope.payload();
        List<Row> rows = new ArrayList<>();
        number(rows, at, source, NO_CAMERA, "pipeline_lag_ms", p.get("pipeline_lag_ms"), "ms", null);
        number(rows, at, source, NO_CAMERA, "pipeline_lag_lower_bound_ms", p.get("pipeline_lag_lower_bound_ms"), "ms", null);
        percentiles(rows, at, source, NO_CAMERA, "capture_to_risk_ms", p.get("capture_to_risk_ms"));
        number(rows, at, source, NO_CAMERA, "gpu_util", p.get("gpu_util"), "ratio", null);
        number(rows, at, source, NO_CAMERA, "gpu_memory_used_ratio", p.get("gpu_memory_used_ratio"), "ratio", null);
        JsonNode drops = p.get("ec2_world_state_drops");
        if (drops != null && drops.isObject()) {
            // 누적 수다. since_ms 가 바뀌면 Tier 0 가 다시 센 것이다
            ObjectNode since = mapper.createObjectNode();
            if (drops.hasNonNull("since_ms")) {
                since.set("since_ms", drops.get("since_ms"));
            }
            number(rows, at, source, NO_CAMERA, "ec2_world_state_drops_superseded", drops.get("superseded"), "count", since);
            number(rows, at, source, NO_CAMERA, "ec2_world_state_drops_disconnected", drops.get("disconnected"), "count", since);
        }
        JsonNode offset = p.get("laptop_clock_offset");
        if (offset != null && offset.isObject()) {
            ObjectNode labels = mapper.createObjectNode();
            for (String field : new String[] {"uncertainty_ms", "round_trip_ms", "sample_count"}) {
                if (offset.hasNonNull(field)) {
                    labels.set(field, offset.get(field));
                }
            }
            number(rows, at, source, NO_CAMERA, "laptop_clock_offset_ms", offset.get("offset_ms"), "ms", labels);
        }
        JsonNode cameras = p.get("cameras");
        if (cameras != null && cameras.isArray()) {
            for (JsonNode camera : cameras) {
                String id = camera.path("id").stringValue("");
                if (id.isEmpty()) {
                    continue;
                }
                number(rows, at, source, id, "fps", camera.get("fps"), "fps", null);
                number(rows, at, source, id, "sync_offset_ms", camera.get("sync_offset_ms"), "ms", null);
                number(rows, at, source, id, "timestamp_uncertainty_ms", camera.get("timestamp_uncertainty_ms"), "ms", null);
                percentiles(rows, at, source, id, "frame_age_ms", camera.get("frame_age_ms"));
                percentiles(rows, at, source, id, "queue_wait_ms", camera.get("queue_wait_ms"));
                number(rows, at, source, id, "max_result_gap_ms", camera.get("max_result_gap_ms"), "ms", null);
                JsonNode frames = camera.get("frames");
                if (frames != null && frames.isObject()) {
                    // 보고 구간(직전 health 이후)의 집계다: received·gateway_to_runpod_loss·skipped_by_policy 등
                    for (Map.Entry<String, JsonNode> entry : frames.properties()) {
                        boolean ms = GAP_MS.equals(entry.getKey());
                        number(rows, at, source, id, ms ? entry.getKey() : "frames_" + entry.getKey(), entry.getValue(),
                                ms ? "ms" : "count", null);
                    }
                }
            }
        }
        return rows;
    }

    private static void number(List<Row> rows, Instant at, String source, String camera, String metric, JsonNode value, String unit,
            JsonNode labels) {
        if (value != null && value.isNumber()) {
            rows.add(new Row(at, source, metric, camera, value.doubleValue(), unit, labels == null || labels.isEmpty() ? null : labels));
        }
    }

    private static void percentiles(List<Row> rows, Instant at, String source, String camera, String metric, JsonNode value) {
        if (value != null && value.isObject() && value.path("p95").isNumber()) {
            rows.add(new Row(at, source, metric, camera, value.get("p95").doubleValue(), "ms", value));
        }
    }
}
