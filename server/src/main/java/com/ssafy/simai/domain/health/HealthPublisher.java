package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.realtime.RealtimePublisher;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import java.util.Objects;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * /topic/health(viewer_health)를 1초마다 보낸다(명세 12.4). 받은 값이 바뀌어 화면이 다르게 보일 상태(System Health·원격 단절·연결·
 * 구성·Scene)가 바뀌면 기다리지 않고 바로 보낸다. 끊김은 시간이 지나야 드러나므로 1초 주기가 잡는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class HealthPublisher {

    private final ViewerHealthService health;
    private final RealtimePublisher publisher;
    private final AtomicReference<String> lastShape = new AtomicReference<>();

    HealthPublisher(ViewerHealthService health, RealtimePublisher publisher, HealthState state) {
        this.health = health;
        this.publisher = publisher;
        state.onChange(this::publishIfChanged);
    }

    @Scheduled(fixedRate = 1_000)
    void publish() {
        send(health.message());
    }

    void publishIfChanged() {
        ObjectNode message = health.message();
        if (!Objects.equals(lastShape.get(), shape(message.get("payload")))) {
            send(message);
        }
    }

    private void send(ObjectNode message) {
        lastShape.set(shape(message.get("payload")));
        publisher.publish(RealtimeTopics.HEALTH, message.get("msg_type").asString(), message.get("schema_version").asString(),
                message.get("payload"));
    }

    /** 화면이 다르게 보일 값만(시각·데이터 연령·원문 제외) */
    private static String shape(JsonNode p) {
        return String.join("|", p.path("system_health").asString(""), p.path("remote_state").asString(""),
                p.path("tier0_link").asString(""), p.path("laptop").path("link").asString(""),
                p.path("calibration_state").asString(""), p.path("config").toString(), p.path("scene").toString());
    }
}
