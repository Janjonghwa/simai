package com.ssafy.simai.domain.metric;

import com.ssafy.simai.domain.metric.SystemMetricRepository.Row;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * EC2 자체 지표를 Micrometer(Actuator) 에서 읽어 system_metric 에 source_id ec2 로 남긴다(명세 3.11 "EC2 에 Actuator + Micrometer",
 * S15P31A101-70). Actuator 엔드포인트는 외부에 열지 않으므로(14장, Nginx 는 /api 만 넘김) 관제 화면은 /api/metrics 로 이 기록을 읽는다.
 * 없는 계측(예: 시험에서 DB 연결 풀을 아직 만들지 않음)은 건너뛴다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class Ec2MetricSampler {

    static final String SOURCE = "ec2";
    private static final Logger log = LoggerFactory.getLogger(Ec2MetricSampler.class);

    private final MeterRegistry meters;
    private final SystemMetricRepository metrics;
    private final Clock clock;

    Ec2MetricSampler(MeterRegistry meters, SystemMetricRepository metrics, Clock clock) {
        this.meters = meters;
        this.metrics = metrics;
        this.clock = clock;
    }

    @Scheduled(fixedDelayString = "${simai.metrics.ec2-interval:10s}", initialDelayString = "${simai.metrics.ec2-interval:10s}")
    void sample() {
        try {
            metrics.insert(rows(clock.instant().truncatedTo(ChronoUnit.MILLIS)));
        } catch (RuntimeException e) {
            log.warn("EC2 지표를 system_metric 에 쓰지 못했다: {}", e.getMessage());
        }
    }

    List<Row> rows(Instant at) {
        List<Row> rows = new ArrayList<>();
        gauge(rows, at, "ec2_process_cpu_usage", "process.cpu.usage", "ratio");
        double heapUsed = sumGauges("jvm.memory.used", "heap");
        double heapMax = sumGauges("jvm.memory.max", "heap");
        if (heapUsed >= 0) {
            rows.add(new Row(at, SOURCE, "ec2_jvm_heap_used_bytes", HealthMetrics.NO_CAMERA, heapUsed, "bytes", null));
            if (heapMax > 0) {
                rows.add(new Row(at, SOURCE, "ec2_jvm_heap_used_ratio", HealthMetrics.NO_CAMERA, heapUsed / heapMax, "ratio", null));
            }
        }
        gauge(rows, at, "ec2_db_connections_active", "hikaricp.connections.active", "count");
        // I1 수신 메시지 누적 수(결과 무관). 서버가 다시 뜨면 0 부터 센다
        Collection<Counter> received = meters.find("simai.i1.messages").counters();
        if (!received.isEmpty()) {
            double total = received.stream().mapToDouble(Counter::count).sum();
            rows.add(new Row(at, SOURCE, "ec2_i1_messages_total", HealthMetrics.NO_CAMERA, total, "count", null));
        }
        return rows;
    }

    private void gauge(List<Row> rows, Instant at, String metric, String meter, String unit) {
        Gauge gauge = meters.find(meter).gauge();
        // 모르면 음수를 주는 계측이 있다(예: 기동 직후 process.cpu.usage). 잴 수 없는 값은 쓰지 않는다
        if (gauge != null && Double.isFinite(gauge.value()) && gauge.value() >= 0) {
            rows.add(new Row(at, SOURCE, metric, HealthMetrics.NO_CAMERA, gauge.value(), unit, null));
        }
    }

    /** 같은 이름의 게이지(메모리 영역별)를 area 태그로 골라 더한다. 하나도 없으면 -1 */
    private double sumGauges(String meter, String area) {
        Collection<Gauge> gauges = meters.find(meter).tag("area", area).gauges();
        if (gauges.isEmpty()) {
            return -1;
        }
        // max 가 정해지지 않은 영역은 -1 이다. 그런 영역은 빼고 더한다
        return gauges.stream().mapToDouble(Gauge::value).filter(v -> v >= 0 && Double.isFinite(v)).sum();
    }
}
