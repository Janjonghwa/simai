package com.ssafy.simai.domain.tier0.i1;

import java.time.Instant;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;

/** health(1 Hz, 명세 12.3·11.2) → system_health. 시각 축은 Tier 0 의 sent_at 이다(db-tables.md). */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class HealthIngestor implements I1MessageIngestor {

    private final Tier0TelemetryRepository repository;

    HealthIngestor(Tier0TelemetryRepository repository) {
        this.repository = repository;
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
    public Outcome ingest(I1Envelope envelope, Instant receivedAt) {
        return repository.insertHealth(envelope, receivedAt) ? Outcome.STORED : Outcome.DUPLICATE;
    }
}
