package com.ssafy.simai.domain.setup;

import java.util.Set;
import java.util.UUID;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 보정 품질 게이트와 「카메라 확인」(S15P31A101-646, docs/design/S15P31A101-646-setup-camera-check.md).
 * <p>I5 site_setup 1.14.0 스냅샷의 {@code quality_gate}·{@code camera_check} 를 세션에 남길 모양으로 고르고, 구역 단계·세팅 완료를
 * 받을 수 있는지 판단한다. 서버는 하트비트를 스키마로 검사하지 않으므로 여기서 모양(객체·상태 값·크기)만 본다. 표시용 값이 어긋나면
 * 저장하지 않고 이전 값을 둔다(스냅샷 자체를 거부하지 않는다). 받는 쪽은 status 와 calibration_version 만 판단에 쓴다.
 * <p>맵 앞 확인·시도(S15P31A101-687, site_setup 1.19.0): 워커는 보정 등록 뒤 맵을 굽기 전에 AWAITING_START + {@code user_wait} 로
 * 관리자의 「걷기 시작」을 기다리고, 시도마다 {@code attempt}(번호·ID·시작/만료·보정)를 싣는다. 시도가 FAILED·INCOMPLETE 면
 * {@code user_wait} 를 다시 실어 「확인만 다시」를 기다린다. 서버는 그 요청을 {@code camera_check_request} 에 두고 하트비트 응답으로
 * 보낸다. 세션 조회의 {@code camera_check_phase} 는 화면 걷기 창이 쓰는 한 값이다.
 * <p>재생 시험 건너뜀(S15P31A101-736, site_setup 1.20.0 SKIPPED_REPLAY): EC2 재생 시험 스위치가 켜져 있고 노트북 입력이 모두 녹화 파일일 때
 * 워커가 걷지 않고 남기는 상태다. {@link #passed} 는 그대로 PASSED 만 통과로 보고(재사용 후보는 걷기로 확인한 세션만), 관문은
 * {@link #satisfied} 로 지금도 건너뛰기가 허용될 때만 SKIPPED_REPLAY 를 통과로 본다.
 */
public final class SetupCameraCheck {

    /** 세션 행 jsonb 한 칸의 상한(쌍 256·카메라 64 를 넉넉히 담는다) */
    private static final int MAX_JSON_BYTES = 256 * 1024;
    /** 끝난 시도 기록 상한(대여 하나에 대기 상한 600 s ÷ 시도 최소 45 s ≈ 14번, 재대여까지 넉넉히) */
    static final int MAX_ATTEMPT_RECORDS = 64;
    private static final Set<String> GATE_VERDICTS = Set.of("PASSED", "NEEDS_CHECK", "FAILED");
    private static final Set<String> CHECK_STATUSES = Set.of("AWAITING_START", "WAITING", "MEASURING", "PASSED", "FAILED",
            "INCOMPLETE", "SKIPPED_REPLAY");
    private static final Set<String> FINAL_STATUSES = Set.of("PASSED", "FAILED", "INCOMPLETE");
    /** 관리자 입력을 받을 수 있는 상태(user_wait 가 함께 있어야 한다) */
    private static final Set<String> USER_WAIT_STATUSES = Set.of("AWAITING_START", "FAILED", "INCOMPLETE");
    public static final String PASSED = "PASSED";
    /** 녹화 재생 시험이라 걷기 확인을 건너뛰었다(site_setup 1.20.0, S15P31A101-736). 통과가 아니다 */
    public static final String SKIPPED_REPLAY = "SKIPPED_REPLAY";
    /** 세션 조회 paused_reason: 워커가 관리자의 걷기 시작·확인만 다시를 기다린다 */
    public static final String PAUSED_REASON = "CAMERA_CHECK";

    /** 화면 걷기 창이 쓰는 카메라 확인 단계(세션 조회 camera_check_phase) */
    enum Phase {
        /** 보정이 끝나 「걷기 시작」을 기다린다 */
        AWAITING_START,
        /** 걷기 시작·확인만 다시를 받았고 워커가 아직 시도를 열지 않았다 */
        STARTING,
        /** 시도 중(사람 기다림·재는 중) */
        WALKING,
        /** 시도가 실패·표본 부족으로 끝났고 「확인만 다시」를 받는다 */
        RETRY_WAIT,
        PASSED,
        /** 녹화 재생 시험이라 걷기를 건너뛰었다(S15P31A101-736). 화면은 「재생 시험: 걷기 확인 생략」을 보인다 */
        SKIPPED_REPLAY,
        /** 실패로 끝났고 더는 다시 확인을 받지 않는다(세팅 실패·마감 또는 옛 워커의 실패) */
        FAILED
    }

    private SetupCameraCheck() {
    }

    /** @return 저장할 quality_gate JSON 문자열, 이 스냅샷에 없거나 어긋나면 null */
    static String qualityGate(ObjectMapper mapper, JsonNode snapshot) {
        JsonNode gate = snapshot.get("quality_gate");
        if (gate == null || !gate.isObject() || !GATE_VERDICTS.contains(gate.path("verdict").asString(""))
                || !gate.path("pairs").isObject() || !gate.path("cameras").isObject() || !gate.path("problems").isArray()) {
            return null;
        }
        return bounded(mapper, gate);
    }

    /** @return 저장할 camera_check JSON 문자열, 이 스냅샷에 없거나 어긋나면 null */
    static String cameraCheck(ObjectMapper mapper, JsonNode snapshot) {
        JsonNode check = snapshot.get("camera_check");
        if (check == null || !check.isObject() || !CHECK_STATUSES.contains(check.path("status").asString(""))
                || !check.path("calibration_version").isString() || check.path("calibration_version").asString().isBlank()
                || !check.path("pairs").isObject() || !check.path("cameras").isObject() || !check.path("problems").isArray()) {
            return null;
        }
        JsonNode attempt = check.get("attempt");
        if (attempt != null && (!attempt.isObject() || attemptNo(check) < 1 || !attempt.path("id").isString())) {
            return null;
        }
        return bounded(mapper, check);
    }

    private static String bounded(ObjectMapper mapper, JsonNode node) {
        String json = mapper.writeValueAsString(node);
        return json.length() > MAX_JSON_BYTES ? null : json;
    }

    /** camera_check 의 시도 번호. 시도가 없으면(걷기 시작 전, 1.18.0 이전 워커) 0 */
    static int attemptNo(JsonNode check) {
        if (check == null || !check.isObject()) {
            return 0;
        }
        JsonNode no = check.path("attempt").path("no");
        return no.isIntegralNumber() ? no.intValue() : 0;
    }

    /** 워커가 관리자의 걷기 시작·확인만 다시를 기다리는가(user_wait 가 있고 그 상태) */
    static boolean awaitingUser(JsonNode check) {
        return check != null && check.isObject() && check.path("user_wait").isObject()
                && USER_WAIT_STATUSES.contains(check.path("status").asString(""));
    }

    /** 이 대여에 한 요청의 시도 번호. 요청이 없거나 다른 대여 것이면 0 */
    static int requestFor(JsonNode request, UUID progressLeaseId) {
        if (request == null || !request.isObject() || progressLeaseId == null
                || !progressLeaseId.toString().equals(request.path("lease_id").asString(null))) {
            return 0;
        }
        JsonNode attempt = request.path("attempt");
        return attempt.isIntegralNumber() ? attempt.intValue() : 0;
    }

    /** 이 대여에 아직 워커가 열지 않은 요청이 있는가(요청 번호 > 워커가 알린 시도 번호) */
    static boolean pending(JsonNode request, JsonNode check, UUID progressLeaseId) {
        return requestFor(request, progressLeaseId) > attemptNo(check);
    }

    /** 세션 조회 camera_check_phase. 카메라 확인을 받은 적 없으면 null */
    static Phase phase(boolean open, JsonNode check, JsonNode request, UUID progressLeaseId) {
        if (check == null || !check.isObject()) {
            return null;
        }
        String status = check.path("status").asString("");
        if (PASSED.equals(status)) {
            return Phase.PASSED;
        }
        if (SKIPPED_REPLAY.equals(status)) {
            return Phase.SKIPPED_REPLAY;
        }
        if (!open) {
            return Phase.FAILED;
        }
        if (pending(request, check, progressLeaseId)) {
            return Phase.STARTING;
        }
        if (awaitingUser(check)) {
            return "AWAITING_START".equals(status) ? Phase.AWAITING_START : Phase.RETRY_WAIT;
        }
        return FINAL_STATUSES.contains(status) ? Phase.FAILED : Phase.WALKING;
    }

    /**
     * 끝난 시도 하나의 기록(camera_check_attempts 에 덧붙일 줄). 시도가 없거나 아직 안 끝났으면 null
     * (id·no·lease_id·status·calibration_version·started_at_ms·expires_at_ms·reasons)
     */
    static String attemptRecord(ObjectMapper mapper, JsonNode check, UUID leaseId) {
        if (attemptNo(check) < 1 || !FINAL_STATUSES.contains(check.path("status").asString(""))) {
            return null;
        }
        JsonNode attempt = check.get("attempt");
        ObjectNode out = mapper.createObjectNode();
        out.put("id", attempt.path("id").asString());
        out.put("no", attempt.path("no").intValue());
        if (leaseId != null) {
            out.put("lease_id", leaseId.toString());
        }
        out.put("status", check.path("status").asString());
        out.set("calibration_version", attempt.path("calibration_version"));
        out.set("started_at_ms", attempt.path("started_at_ms"));
        out.set("expires_at_ms", attempt.path("expires_at_ms"));
        ArrayNode reasons = out.putArray("reasons");
        check.path("problems").forEach(p -> reasons.add(p.path("reason").asString("")));
        return mapper.writeValueAsString(out);
    }

    /**
     * 카메라 확인을 통과했는지: 상태 PASSED 이고 그 확인이 잰 보정이 지금 세션 보정과 같다. 재대여로 보정이 바뀌면 이전 대여의 통과는
     * 새 보정의 통과가 아니다. 모르면(확인 결과 없음, 보정 없음) 통과가 아니다.
     */
    public static boolean passed(String status, String checkedCalibrationVersion, String sessionCalibrationVersion) {
        return PASSED.equals(status) && sessionCalibrationVersion != null
                && sessionCalibrationVersion.equals(checkedCalibrationVersion);
    }

    /**
     * 구역 단계·세팅 완료 관문의 카메라 확인 조건: {@link #passed} 이거나, 녹화 재생 시험 건너뜀(SKIPPED_REPLAY)이고 지금도 건너뛰기가 허용되며
     * (재생 시험 스위치 켜짐 + 노트북 입력이 모두 녹화 파일, {@code ReplayTestSwitch.cameraCheckSkipAllowed}) 그 보정이 세션 보정과 같다
     * (S15P31A101-736). 실제 카메라로 돌아왔거나 스위치를 끄면 건너뛴 세션은 다시 막힌다.
     */
    public static boolean satisfied(String status, String checkedCalibrationVersion, String sessionCalibrationVersion,
            boolean replaySkipAllowed) {
        if (passed(status, checkedCalibrationVersion, sessionCalibrationVersion)) {
            return true;
        }
        return replaySkipAllowed && SKIPPED_REPLAY.equals(status) && sessionCalibrationVersion != null
                && sessionCalibrationVersion.equals(checkedCalibrationVersion);
    }
}
