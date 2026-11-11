package com.ssafy.simai.domain.tier0.event;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;

/**
 * 지금 Tier 0 가 들고 있는 이벤트(S15P31A101-595). 스냅샷으로 통째로 바꾸고 실시간 전이로 고치며, I1 수신이 유예 시간 넘게 없으면 비운다.
 * 시계는 가짜 단조 시계(ns)다.
 */
class Tier0HeldEventsTest {

    private static final String GW = "tier0-01";
    private static final long GRACE_MS = 60_000;
    private static final long MS = 1_000_000L;

    private final AtomicLong now = new AtomicLong(1_000_000 * MS);
    private final I1ConnectionRegistry connections = new I1ConnectionRegistry();
    private final Tier0HeldEvents held = new Tier0HeldEvents(connections, GRACE_MS, now::get, "boot");

    private final UUID a = UUID.randomUUID();
    private final UUID b = UUID.randomUUID();
    private final UUID old = UUID.randomUUID();

    private void receive() {
        connections.received(GW, now.get());
    }

    private void advanceMs(long ms) {
        now.addAndGet(ms * MS);
    }

    @Test
    void 스냅샷의_활성_이벤트만_들고_있고_스냅샷에_없는_EC2_활성_이벤트는_들고_있지_않다() {
        receive();
        held.replace(GW, Set.of(a, b));

        assertThat(held.isHeld(GW, a)).isTrue();
        assertThat(held.isHeld(GW, b)).isTrue();
        assertThat(held.isHeld(GW, old)).isFalse();
        // 다른 Tier 0 의 같은 ID 나 gateway_id 없는 이벤트는 들고 있지 않다
        assertThat(held.isHeld("tier0-02", a)).isFalse();
        assertThat(held.isHeld(null, a)).isFalse();
    }

    @Test
    void 새_Tier_0_가_빈_스냅샷을_보내면_이전_이벤트는_모두_내려가고_판이_바뀐다() {
        receive();
        held.replace(GW, Set.of(a, old));
        String before = held.revision();

        held.replace(GW, Set.of());

        assertThat(held.isHeld(GW, a)).isFalse();
        assertThat(held.isHeld(GW, old)).isFalse();
        assertThat(held.revision()).isNotEqualTo(before).startsWith("boot-");
    }

    @Test
    void 같은_집합의_스냅샷은_판을_바꾸지_않는다_재연결마다_목록을_다시_받지_않게() {
        receive();
        held.replace(GW, Set.of(a));
        String before = held.revision();

        held.replace(GW, Set.of(a));

        assertThat(held.revision()).isEqualTo(before);
    }

    @Test
    void 스냅샷_뒤_실시간_전이는_발생_갱신이면_넣고_해소면_빼며_확인은_그대로_두고_판은_바꾸지_않는다() {
        receive();
        held.replace(GW, Set.of(a));
        String before = held.revision();

        held.apply(GW, b, "RAISED");
        held.apply(GW, a, "RESOLVED");
        held.apply(GW, old, "ACKNOWLEDGED");

        assertThat(held.isHeld(GW, b)).isTrue();
        assertThat(held.isHeld(GW, a)).isFalse();
        assertThat(held.isHeld(GW, old)).isFalse();
        // 전이 하나는 그 /topic/events 가 알린다
        assertThat(held.revision()).isEqualTo(before);

        held.apply(GW, a, "UPDATED"); // 재개
        assertThat(held.isHeld(GW, a)).isTrue();
    }

    @Test
    void 짧은_I1_끊김에는_유예_시간_안이라_계속_들고_있고_다시_받으면_이어진다() {
        receive();
        held.replace(GW, Set.of(a));
        String before = held.revision();

        advanceMs(GRACE_MS); // 경계: 유예 시간과 같으면 아직 살아 있다
        held.expire();
        assertThat(held.isHeld(GW, a)).isTrue();
        assertThat(held.revision()).isEqualTo(before);

        receive(); // 같은 Tier 0 가 다시 붙었다
        advanceMs(GRACE_MS - 1);
        held.expire();
        assertThat(held.isHeld(GW, a)).isTrue();
    }

    @Test
    void GPU_를_끄면_유예_시간_뒤_진행_중이_하나도_남지_않고_판이_바뀐다() {
        receive();
        held.replace(GW, Set.of(a, b));
        String before = held.revision();

        advanceMs(GRACE_MS + 1);
        // 주기 작업 전에도 조회는 이미 내려간 값을 준다
        assertThat(held.isHeld(GW, a)).isFalse();

        held.expire();
        assertThat(held.revision()).isNotEqualTo(before);
        String afterExpire = held.revision();

        // 같은 Tier 0 가 늦게 다시 붙어도 스냅샷 전에는 비어 있다(이전 집합을 되살리지 않는다)
        receive();
        assertThat(held.isHeld(GW, a)).isFalse();
        held.expire();
        assertThat(held.revision()).isEqualTo(afterExpire);
    }

    @Test
    void EC2_가_다시_뜨면_스냅샷을_받기_전까지_비어_있고_판의_앞머리가_다르다() {
        Tier0HeldEvents restarted = new Tier0HeldEvents(connections, GRACE_MS, now::get, "boot2");
        receive();

        assertThat(restarted.isHeld(GW, a)).isFalse();
        assertThat(restarted.revision()).isEqualTo("boot2-0").isNotEqualTo(held.revision());
    }

    @Test
    void 유예_시간은_0_보다_커야_한다() {
        assertThatThrownBy(() -> new Tier0HeldEvents(connections, 0, now::get, "boot"))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
