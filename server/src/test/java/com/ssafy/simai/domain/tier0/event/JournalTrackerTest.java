package com.ssafy.simai.domain.tier0.event;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.UUID;
import org.junit.jupiter.api.Test;

class JournalTrackerTest {

    @Test
    void 저장된_커서가_없으면_처음_받은_offset_앞을_기준으로_잇는다() {
        JournalTracker tracker = new JournalTracker(null);

        assertThat(tracker.onPersisted(40)).isNull();
        assertThat(tracker.onPersisted(41)).isNull();

        assertThat(tracker.acked()).isEqualTo(41);
        assertThat(tracker.takePendingAck()).isEqualTo(41);
        assertThat(tracker.takePendingAck()).isNull();
    }

    @Test
    void 빈_구간은_한_번만_다시_요청하고_채워지면_이어서_나아간다() {
        JournalTracker tracker = new JournalTracker(9L);

        assertThat(tracker.onPersisted(10)).isNull();
        assertThat(tracker.onPersisted(13)).isEqualTo(11);
        assertThat(tracker.onPersisted(14)).isNull();
        assertThat(tracker.acked()).isEqualTo(10);

        assertThat(tracker.onPersisted(11)).isNull();
        assertThat(tracker.onPersisted(12)).isNull();
        assertThat(tracker.acked()).isEqualTo(14);

        // 빈 구간이 다 찼으니 새 빈 구간은 다시 요청한다
        assertThat(tracker.onPersisted(16)).isEqualTo(15);
    }

    @Test
    void 이미_받은_offset_은_아무것도_바꾸지_않는다() {
        JournalTracker tracker = new JournalTracker(20L);

        assertThat(tracker.onPersisted(18)).isNull();
        assertThat(tracker.acked()).isEqualTo(20);
        assertThat(tracker.hasPendingAck()).isFalse();
    }

    @Test
    void 스냅샷_offset_까지_받지_못했으면_다시_요청한다() {
        JournalTracker tracker = new JournalTracker(5L);

        assertThat(tracker.missingBefore(7L)).isEqualTo(6);
        assertThat(tracker.missingBefore(5L)).isNull();
        assertThat(tracker.missingBefore(null)).isNull();
    }

    @Test
    void 기준이_전혀_없으면_스냅샷_offset_을_기준으로_받아들인다() {
        JournalTracker tracker = new JournalTracker(null);

        assertThat(tracker.missingBefore(30L)).isNull();
        assertThat(tracker.acked()).isEqualTo(30);
    }

    @Test
    void 동기화_중에_바뀐_이벤트만_모으고_끝나면_실시간이다() {
        JournalTracker tracker = new JournalTracker(null);
        UUID during = UUID.randomUUID();
        UUID after = UUID.randomUUID();

        tracker.touched(during);
        assertThat(tracker.finishSync()).containsExactly(during);
        tracker.touched(after);

        assertThat(tracker.syncMode()).isFalse();
        assertThat(tracker.finishSync()).isEmpty();
    }

    @Test
    void 음수_acked_는_보내지_않는다() {
        JournalTracker tracker = new JournalTracker(null);

        tracker.onPersisted(0);
        assertThat(tracker.acked()).isZero();
        assertThat(tracker.takePendingAck()).isZero();
    }
}
