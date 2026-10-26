package com.ssafy.simai.domain.tier0.event;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.UUID;
import org.junit.jupiter.api.Test;

class JournalTrackerTest {

    @Test
    void 저장된_커서가_없으면_처음_받은_offset_앞을_기준으로_잇는다() {
        JournalTracker tracker = new JournalTracker(null, 0);

        assertThat(tracker.onPersisted(40)).isNull();
        assertThat(tracker.onPersisted(41)).isNull();

        assertThat(tracker.acked()).isEqualTo(41);
        assertThat(tracker.hasPendingAck()).isTrue();
        assertThat(tracker.takeAck()).isEqualTo(41);
        assertThat(tracker.hasPendingAck()).isFalse();
    }

    @Test
    void 빈_구간은_한_번만_다시_요청하고_채워지면_이어서_나아간다() {
        JournalTracker tracker = new JournalTracker(9L, 0);

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
        JournalTracker tracker = new JournalTracker(20L, 0);

        assertThat(tracker.onPersisted(18)).isNull();
        assertThat(tracker.acked()).isEqualTo(20);
        assertThat(tracker.hasPendingAck()).isFalse();
    }

    @Test
    void 스냅샷_offset_까지_받지_못했으면_다시_요청한다() {
        JournalTracker tracker = new JournalTracker(5L, 0);

        assertThat(tracker.missingBefore(7L)).isEqualTo(6);
        assertThat(tracker.missingBefore(5L)).isNull();
        assertThat(tracker.missingBefore(null)).isNull();
    }

    @Test
    void 기준이_전혀_없으면_스냅샷_offset_을_기준으로_받아들인다() {
        JournalTracker tracker = new JournalTracker(null, 0);

        assertThat(tracker.missingBefore(30L)).isNull();
        assertThat(tracker.acked()).isEqualTo(30);
    }

    @Test
    void 동기화_중에_바뀐_이벤트만_모으고_끝나면_실시간이다() {
        JournalTracker tracker = new JournalTracker(null, 0);
        UUID during = UUID.randomUUID();
        UUID after = UUID.randomUUID();

        tracker.touched(during);
        assertThat(tracker.touchedDuringSync()).containsExactly(during);
        // 분류가 커밋되기 전에는 그대로 남는다
        assertThat(tracker.touchedDuringSync()).containsExactly(during);
        tracker.finishSync();
        tracker.touched(after);

        assertThat(tracker.syncMode()).isFalse();
        assertThat(tracker.touchedDuringSync()).isEmpty();
    }

    @Test
    void 음수_acked_는_보내지_않는다() {
        JournalTracker tracker = new JournalTracker(null, 0);

        tracker.onPersisted(0);
        assertThat(tracker.acked()).isZero();
        assertThat(tracker.takeAck()).isZero();
    }

    @Test
    void 첫_offset_저장이_실패해도_다음_성공이_그_offset_을_건너뛰지_않는다() {
        JournalTracker tracker = new JournalTracker(null, 0);

        tracker.noteReceived(0);
        // offset 0 저장 실패(onPersisted 를 부르지 않음), offset 1 은 저장됨
        assertThat(tracker.onPersisted(1)).isEqualTo(0);
        assertThat(tracker.acked()).isEqualTo(-1);
        assertThat(tracker.hasPendingAck()).isFalse();
    }

    @Test
    void 스냅샷_offset_이_acked_보다_작거나_저널이_비었으면_새_저널이다() {
        JournalTracker tracker = new JournalTracker(500L, 3);

        assertThat(tracker.isNewJournal(600L)).isFalse();
        assertThat(tracker.isNewJournal(2L)).isTrue();
        assertThat(tracker.isNewJournal(null)).isTrue();

        tracker.startEpoch(4);
        assertThat(tracker.epoch()).isEqualTo(4);
        assertThat(tracker.acked()).isNull();
        assertThat(tracker.onPersisted(0)).isNull();
        assertThat(tracker.acked()).isZero();
    }
}
