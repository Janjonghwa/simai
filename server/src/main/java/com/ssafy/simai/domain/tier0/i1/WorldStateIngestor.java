package com.ssafy.simai.domain.tier0.i1;

import java.time.Instant;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * world_state(10 Hz, 명세 12.3·11.2) → world_track_state·skeleton_3d. 트랙이 없는 tick 은 행을 남기지 않는다.
 * 값은 받은 그대로 둔다. 1.x 와 2.x 는 head_z UNCERTAIN 규칙만 다르므로(계약 설명) 읽는 쪽이 head_z_validity 를 함께 본다.
 * 트랙·골격은 한 트랜잭션이다. 중간에 계약 위반이 나오면 그 메시지는 아무것도 남기지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class WorldStateIngestor implements I1MessageIngestor {

    private final Tier0TelemetryRepository repository;

    WorldStateIngestor(Tier0TelemetryRepository repository) {
        this.repository = repository;
    }

    @Override
    public String msgType() {
        return "world_state";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1, 2);
    }

    @Override
    @Transactional
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        return repository.upsertWorldState(envelope, receivedAt) ? Outcome.STORED : Outcome.DUPLICATE;
    }
}
