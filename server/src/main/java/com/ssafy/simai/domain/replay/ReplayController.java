package com.ssafy.simai.domain.replay;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Instant;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 최소 리플레이 조회(명세 12.6·8.5·11.4, FR-28, S15P31A101-136). 열람 권한(로그인한 모든 역할).
 * 응답의 메시지는 관제 앱이 실시간으로 받는 봉투와 같은 모양이라 재생 엔진(viewer3d replay)에 그대로 넣는다. 설계는 docs/design/server-replay.md.
 * <pre>
 * GET /api/replay?from=ms&amp;to=ms&amp;step_ms=100
 *   → {from, to, step_ms, world_states[], initial_events[], events[], gaps[{from, to}]}
 * GET /api/world?at=ms
 *   → {at, world_state | null, events[]}
 * </pre>
 * 기록 없는 시각을 보간하거나 채우지 않는다. 구간이 너무 크면 잘라 주지 않고 400 으로 알린다(잘린 걸 모르고 "기록 없음"으로 보지 않게).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ReplayController {

    /** world_state 주기(10 Hz, 명세 12.3). 이 값이면 칸으로 솎지 않고 모든 tick 을 준다 */
    static final long MIN_STEP_MS = 100;
    static final long MAX_STEP_MS = 60_000;
    /**
     * 한 번에 볼 구간. step_ms 를 키워도 칸을 고르려면 구간의 트랙 행을 모두 읽으므로 구간 자체를 묶는다.
     * 10 Hz·사람 10명이면 36만 행이다. 더 긴 작업은 화면이 나눠 부른다
     */
    static final long MAX_RANGE_MS = 3_600_000;
    /** 한 번에 줄 world_state 수. 10 Hz 로 5분, 사람 10명이면 트랙 행 3만 개 정도다 */
    static final int MAX_FRAMES = 3_000;
    /** 한 번에 줄 전이 수. 넘으면 구간을 줄이라고 알린다 */
    static final int MAX_TRANSITIONS = 5_000;
    /** Tier 0 health(1 Hz)가 이만큼 없으면 기록 없는 구간이다. 노트북 하트비트 3회 누락과 같은 기준(FR-134) */
    static final long GAP_MS = 3_000;
    /** PostgreSQL timestamptz 가 받는 범위 안(9999-12-31) */
    private static final long MAX_EPOCH_MS = 253_402_300_799_999L;

    private final ReplayRepository replay;
    private final ObjectMapper mapper;

    ReplayController(ReplayRepository replay, ObjectMapper mapper) {
        this.replay = replay;
        this.mapper = mapper;
    }

    /** 여러 질의를 한 스냅숏에서 읽는다. 지금에 가까운 구간이면 읽는 사이 들어온 기록 때문에 world·이벤트·빈 구간이 서로 다른 시점이 될 수 있다 */
    @GetMapping("/api/replay")
    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    ObjectNode replay(@RequestParam(name = "from", required = false) String fromMs,
            @RequestParam(name = "to", required = false) String toMs,
            @RequestParam(name = "step_ms", required = false) String stepMs) {
        Instant from = epochMs(fromMs);
        Instant to = epochMs(toMs);
        long step = stepMs == null || stepMs.isBlank() ? MIN_STEP_MS : number(stepMs);
        if (!from.isBefore(to) || step < MIN_STEP_MS || step > MAX_STEP_MS) {
            throw new BusinessException(ErrorCode.Replay.INVALID_REPLAY_QUERY);
        }
        // 칸 수는 우선 구간으로 센다. 기록이 드문 구간이라도 같은 요청은 늘 같은 결과(거부 또는 응답)가 되게 한다
        long span = to.toEpochMilli() - from.toEpochMilli();
        if (span > MAX_RANGE_MS || span / step + 1 > MAX_FRAMES) {
            throw new BusinessException(ErrorCode.Replay.REPLAY_RANGE_TOO_LARGE);
        }
        List<ObjectNode> transitions = replay.transitions(from, to, MAX_TRANSITIONS + 1);
        if (transitions.size() > MAX_TRANSITIONS) {
            throw new BusinessException(ErrorCode.Replay.REPLAY_RANGE_TOO_LARGE);
        }
        // 솎지 않으면 촬영 시각이 흔들려 구간으로 센 칸 수보다 tick 이 많을 수 있다. 자르지 않고 거부한다
        List<ObjectNode> worldStates = replay.worldStates(from, to, step, step == MIN_STEP_MS, MAX_FRAMES + 1);
        if (worldStates.size() > MAX_FRAMES) {
            throw new BusinessException(ErrorCode.Replay.REPLAY_RANGE_TOO_LARGE);
        }
        ObjectNode body = mapper.createObjectNode();
        body.put("from", from.toEpochMilli());
        body.put("to", to.toEpochMilli());
        body.put("step_ms", step);
        array(body, "world_states", worldStates);
        array(body, "initial_events", replay.activeEventsAt(from));
        array(body, "events", transitions);
        body.set("gaps", replay.gaps(from, to, GAP_MS));
        return body;
    }

    @GetMapping("/api/world")
    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    ObjectNode world(@RequestParam(name = "at", required = false) String atMs) {
        Instant at = epochMs(atMs);
        ObjectNode body = mapper.createObjectNode();
        body.put("at", at.toEpochMilli());
        body.set("world_state", replay.worldStateAt(at));
        array(body, "events", replay.activeEventsAt(at));
        return body;
    }

    private static void array(ObjectNode body, String field, List<ObjectNode> items) {
        ArrayNode out = body.putArray(field);
        items.forEach(out::add);
    }

    /** 없거나 숫자가 아니거나 DB 가 받지 못하는 시각이면 400(코드 INVALID_REPLAY_QUERY) */
    private static Instant epochMs(String value) {
        if (value == null || value.isBlank()) {
            throw new BusinessException(ErrorCode.Replay.INVALID_REPLAY_QUERY);
        }
        long ms = number(value);
        if (ms < 0 || ms > MAX_EPOCH_MS) {
            throw new BusinessException(ErrorCode.Replay.INVALID_REPLAY_QUERY);
        }
        return Instant.ofEpochMilli(ms);
    }

    private static long number(String value) {
        try {
            return Long.parseLong(value.strip());
        } catch (NumberFormatException e) {
            throw new BusinessException(ErrorCode.Replay.INVALID_REPLAY_QUERY);
        }
    }
}
