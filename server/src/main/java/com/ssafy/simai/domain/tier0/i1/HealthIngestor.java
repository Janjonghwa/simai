package com.ssafy.simai.domain.tier0.i1;

import java.time.Instant;
import java.util.Set;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;

/**
 * health(1 Hz, 명세 12.3·11.2) → system_health. 시각 축은 Tier 0 의 sent_at 이다(db-tables.md).
 * 저장했으면 화면 상태(viewer_health)가 원문을 쓰도록 Tier0HealthStored 를 낸다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class HealthIngestor implements I1MessageIngestor {

    private final Tier0TelemetryRepository repository;
    private final ApplicationEventPublisher events;

    HealthIngestor(Tier0TelemetryRepository repository, ApplicationEventPublisher events) {
        this.repository = repository;
        this.events = events;
    }

    @Override
    public String msgType() {
        return "health";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1, 2);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        if (!repository.insertHealth(envelope, receivedAt)) {
            return Outcome.DUPLICATE;
        }
        events.publishEvent(new Tier0HealthStored(envelope, receivedAt));
        return Outcome.STORED;
    }
}
