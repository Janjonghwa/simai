package com.ssafy.simai.domain.watch;

import java.util.concurrent.atomic.AtomicLong;

/**
 * 워치 대조 시험(WatchMotionMatch·WatchWaveCheckin)이 함께 쓰는 World 시각 배정(S15P31A101-674).
 *
 * <p>두 클래스는 같은 Spring 문맥(대조기 하나)을 쓰고 대조기는 시험이 끝나도 앞 시험의 트랙을 들고 있다. 클래스마다 따로 60초씩
 * 띄우면 한 클래스의 트랙이 다른 클래스 시험과 같은 World 시각에 겹친다(예: 걷는 사람 1.9 Hz 와 손 흔들기 2.0 Hz). 그 트랙이
 * 경쟁 짝이 되어 점수 차이 문턱에 걸리면 잇지 않는다(CI #921·#946). 그래서 두 클래스가 한 계수기로 늘 앞으로만 간다.
 */
final class WatchMatchTestTimes {

    /** 시험 하나가 쓰는 World 시각 폭. 가장 긴 시험이 world_state 36초를 흘리므로 그보다 넉넉히 띄운다 */
    static final long SLOT_MS = 45_000;
    /**
     * 첫 시험은 벽시계보다 이만큼 앞에서 시작한다. 대조는 마지막 창(끝이 마지막 World 시각보다 뒤)을 벽시계로 창 끝 + 5초가
     * 지나야 내므로, 시험이 도는 동안 그 창이 내지지 않게 앞에 둔다. 두 클래스 시험 10개를 이어 돌아도 마지막 World 시각이
     * {@link WatchMotionMatcher#MAX_FUTURE_MS}(600초)보다 한참 안쪽이다(30 + 9 × 45 + 36 = 471초)
     */
    static final long LEAD_MS = 30_000;
    private static final AtomicLong NEXT = new AtomicLong();

    private WatchMatchTestTimes() {
    }

    /** 다음 시험의 World 시작 시각(ms, 100 ms 단위). 앞 시험의 모든 트랙보다 뒤다 */
    static long nextT0() {
        return NEXT.updateAndGet(prev -> Math.max(System.currentTimeMillis() / 100 * 100 + LEAD_MS, prev + SLOT_MS));
    }
}
