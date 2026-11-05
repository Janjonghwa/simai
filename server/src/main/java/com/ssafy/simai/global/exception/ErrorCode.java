package com.ssafy.simai.global.exception;

import org.springframework.http.HttpStatus;

/**
 * 서버 오류 목록. 도메인마다 enum 하나를 두고 HTTP 상태와 문구를 함께 적는다.
 * 응답의 code 는 enum 상수 이름이다(GlobalExceptionHandler).
 */
public sealed interface ErrorCode {

    HttpStatus status();

    String message();

    String name();

    enum Auth implements ErrorCode {
        // 아이디 없음·비밀번호 틀림·막힌 계정을 나누지 않는다. 나누면 아이디가 있는지 드러난다
        LOGIN_FAILED(HttpStatus.UNAUTHORIZED, "아이디 또는 비밀번호가 맞지 않습니다"),
        // 없음·만료·폐기·재사용·막힌 계정을 나누지 않는다. 관제 앱은 다시 로그인시킨다
        INVALID_REFRESH_TOKEN(HttpStatus.UNAUTHORIZED, "다시 로그인하세요"),
        // 같은 쿠키로 동시에 갱신해 다른 요청이 방금 교체했다(탭 두 개·재시도). 새 쿠키가 이미 있으니 한 번 더 갱신한다
        REFRESH_TOKEN_ROTATED(HttpStatus.CONFLICT, "다른 요청이 먼저 갱신했습니다. 다시 시도하세요"),
        // 보안 필터가 거부했다(access 토큰 없음·만료·위조, 역할 부족)
        UNAUTHORIZED(HttpStatus.UNAUTHORIZED, "로그인이 필요합니다"),
        FORBIDDEN(HttpStatus.FORBIDDEN, "권한이 없습니다");

        private final HttpStatus status;
        private final String message;

        Auth(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    enum User implements ErrorCode {
        DUPLICATE_LOGIN_ID(HttpStatus.CONFLICT, "이미 있는 아이디입니다"),
        USER_NOT_FOUND(HttpStatus.NOT_FOUND, "없는 아이디입니다"),
        WEAK_PASSWORD(HttpStatus.BAD_REQUEST, "비밀번호는 8자 이상이어야 합니다"),
        // BCrypt 는 72바이트까지만 본다
        PASSWORD_TOO_LONG(HttpStatus.BAD_REQUEST, "비밀번호는 72바이트(영문 72자, 한글 24자) 이하여야 합니다");

        private final HttpStatus status;
        private final String message;

        User(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    enum Event implements ErrorCode {
        EVENT_NOT_FOUND(HttpStatus.NOT_FOUND, "이벤트를 찾을 수 없습니다"),
        // Tier 0 는 해소된 이벤트의 확인을 응답 없이 버린다. 미리 막아 화면에 알린다
        EVENT_RESOLVED(HttpStatus.CONFLICT, "이미 해소된 이벤트입니다"),
        INVALID_EVENT_SEQUENCE(HttpStatus.BAD_REQUEST, "event_sequence 는 1 이상이어야 합니다"),
        INVALID_ACK_CHANNEL(HttpStatus.BAD_REQUEST, "channel 은 CONTROL_ROOM 또는 FOREMAN_PWA 여야 합니다"),
        ASSESSMENT_NOT_FOUND(HttpStatus.NOT_FOUND, "VLM 평가 기록을 찾을 수 없습니다"),
        ASSESSMENT_ALREADY_DECIDED(HttpStatus.CONFLICT, "이미 승인 또는 거절된 상향 의견입니다"),
        ESCALATION_NOT_UPWARD(HttpStatus.CONFLICT, "현재 등급보다 높은 상향 의견만 승인할 수 있습니다"),
        INVALID_ASSESSMENT_ID(HttpStatus.BAD_REQUEST, "assessmentId 가 필요합니다"),
        INVALID_ESCALATION_DECISION(HttpStatus.BAD_REQUEST, "decision 은 APPROVED 또는 REJECTED 여야 합니다"),
        INVALID_ESCALATION_NOTE(HttpStatus.BAD_REQUEST, "note 는 1~500자여야 합니다"),
        EVENT_NOT_LIVE(HttpStatus.CONFLICT, "LIVE 이벤트만 사고 구간 요약을 요청할 수 있습니다"),
        CLIP_NOT_READY(HttpStatus.CONFLICT, "이벤트 클립이 아직 준비되지 않았습니다"),
        INVALID_CLOSE_REASON(HttpStatus.BAD_REQUEST, "reason 은 1~500자여야 합니다"),
        INVALID_IDEMPOTENCY_KEY(HttpStatus.BAD_REQUEST, "idempotency_key 는 1~128자여야 합니다"),
        // 같은 멱등 키를 다른 이벤트·다른 관리자가 쓰면 처음 결과를 돌려주지 않는다
        IDEMPOTENCY_KEY_REUSED(HttpStatus.CONFLICT, "이 멱등 키는 다른 종료 요청에 이미 쓰였습니다"),
        CLOSE_NO_GATEWAY(HttpStatus.CONFLICT, "이 이벤트를 보낸 Tier 0 를 알 수 없어 종료를 요청할 수 없습니다");

        private final HttpStatus status;
        private final String message;

        Event(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    enum Machine implements ErrorCode {
        MACHINE_EXISTS(HttpStatus.CONFLICT, "이미 등록된 ID 입니다. 토큰을 바꾸려면 rotate 를 쓴다"),
        MACHINE_NOT_FOUND(HttpStatus.NOT_FOUND, "등록되지 않은 ID 입니다"),
        MACHINE_REVOKED(HttpStatus.CONFLICT, "폐기된 ID 입니다. 다시 쓰려면 reactivate 를 쓴다"),
        MACHINE_NOT_REVOKED(HttpStatus.CONFLICT, "폐기되지 않은 ID 입니다. 토큰을 바꾸려면 rotate 를 쓴다"),
        TOKEN_CHECK_UNAVAILABLE(HttpStatus.SERVICE_UNAVAILABLE, "토큰을 확인할 수 없습니다. 잠시 뒤 다시 시도하세요"),
        INVALID_MACHINE_ID(HttpStatus.BAD_REQUEST, "ID 는 소문자·숫자·- 로 1~63자여야 합니다"),
        INVALID_DEVICE_KIND(HttpStatus.BAD_REQUEST, "기기 종류는 LAPTOP 또는 WATCH 여야 합니다"),
        INVALID_JOB_TYPES(HttpStatus.BAD_REQUEST, "작업 유형은 계약 job_type 값이어야 합니다(쉼표로 구분)");

        private final HttpStatus status;
        private final String message;

        Machine(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 구역·작업면·보정 문서·구성 묶음(S15P31A101-120·-58) */
    enum Config implements ErrorCode {
        ZONE_NOT_FOUND(HttpStatus.NOT_FOUND, "구역을 찾을 수 없습니다"),
        INVALID_ZONE(HttpStatus.BAD_REQUEST, "구역 값이 올바르지 않습니다(name·zone_kind 필수, eval_type 은 GROUND_FOOTPRINT·VOLUME_3D, severity 는 CRITICAL·HIGH·CAUTION·INFO)"),
        INVALID_POLYGON(HttpStatus.BAD_REQUEST, "다각형은 서로 다른 꼭짓점 3개 이상이고 자기 교차·넓이 0 이 없어야 합니다"),
        INVALID_Z_RANGE(HttpStatus.BAD_REQUEST, "높이 범위는 z_min_m < z_max_m 이어야 하고 VOLUME_3D 구역은 둘 다 있어야 합니다"),
        STATIC_OBJECT_NOT_FOUND(HttpStatus.NOT_FOUND, "작업면·정적 객체를 찾을 수 없습니다"),
        // 같은 항목을 동시에 고쳐 다른 요청이 먼저 새 버전을 만들었다. 다시 읽고 고친다
        CONCURRENT_EDIT(HttpStatus.CONFLICT, "다른 요청이 먼저 새 버전을 만들었습니다. 다시 불러와 고치세요"),
        INVALID_STATIC_OBJECT(HttpStatus.BAD_REQUEST, "작업면·정적 객체 값이 올바르지 않습니다(kind, OBB center·size·yaw_rad, 작업면은 top_height_m > 0)"),
        INVALID_SURFACE_ZONE(HttpStatus.BAD_REQUEST, "작업면만 구역에 이을 수 있고, zone_id 는 이 현장의 지금 구역이어야 합니다"),
        // 한 구역에 지금 작업면은 하나다. 옛 작업면을 먼저 보관하거나 연결을 푼다
        SURFACE_ZONE_TAKEN(HttpStatus.CONFLICT, "이 구역에는 이미 이어진 작업면이 있습니다"),
        INVALID_ZONE_COMMIT(HttpStatus.BAD_REQUEST, "구역 저장 요청이 올바르지 않습니다(zone.op 는 CREATE·UPDATE·ARCHIVE·KEEP, surface.op 는 CREATE·UPDATE·REPLACE·ARCHIVE, 필요한 ID·본문)"),
        INVALID_CALIBRATION(HttpStatus.BAD_REQUEST, "보정 문서가 계약(contracts/providers/calibration)에 맞지 않습니다"),
        CALIBRATION_SITE_MISMATCH(HttpStatus.BAD_REQUEST, "보정 문서의 현장(coordinate_frame.site_id)이 이 서버의 현장과 다릅니다"),
        // 덮어쓰지 않는다. 다른 문서면 새 calibration_version 으로 올린다
        CALIBRATION_VERSION_CONFLICT(HttpStatus.CONFLICT, "같은 보정 버전에 다른 문서가 이미 있습니다"),
        CALIBRATION_MISSING(HttpStatus.CONFLICT, "올린 보정 문서가 없어 구성 묶음을 만들 수 없습니다. 먼저 POST /api/calibrations 로 올리세요"),
        // 화면의 현재 보정(Tier 0 알림)과 묶음의 보정이 갈리지 않게 한다(S15P31A101-263)
        CURRENT_CALIBRATION_MISSING(HttpStatus.CONFLICT, "Tier 0 가 알린 현재 보정 버전의 문서가 없어 구성 묶음을 만들 수 없습니다. 그 버전 문서를 POST /api/calibrations 로 올리세요"),
        RULES_MISSING(HttpStatus.CONFLICT, "유효한 규칙 버전이 없어 구성 묶음을 만들 수 없습니다"),
        INVALID_RULE_PROFILE(HttpStatus.BAD_REQUEST, "rule_profile 은 OPERATION 또는 CLASSROOM_TEST 여야 합니다"),
        PUBLIC_URL_NOT_SET(HttpStatus.SERVICE_UNAVAILABLE, "Tier 0 가 묶음을 내려받을 https 공개 주소(SIMAI_PUBLIC_BASE_URL)가 설정되지 않았습니다"),
        CONFIG_BUNDLE_NOT_FOUND(HttpStatus.NOT_FOUND, "구성 묶음을 찾을 수 없습니다"),
        SITE_NOT_SINGLE(HttpStatus.CONFLICT, "현장이 하나가 아닙니다(MVP 는 현장 하나)");

        private final HttpStatus status;
        private final String message;

        Config(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 노트북 하트비트·System Health(S15P31A101-262) */
    enum Health implements ErrorCode {
        INVALID_HEARTBEAT(HttpStatus.BAD_REQUEST, "하트비트가 계약(laptop_heartbeat 1.x)에 맞지 않습니다"),
        HEARTBEAT_SOURCE_MISMATCH(HttpStatus.BAD_REQUEST, "하트비트 source_id 가 기기 토큰의 기기 ID 와 다릅니다"),
        NOT_A_LAPTOP(HttpStatus.FORBIDDEN, "노트북으로 등록된 기기만 하트비트를 보냅니다");

        private final HttpStatus status;
        private final String message;

        Health(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 워치 임시 수동 배정(확장 C.6, S15P31A101-338) */
    enum Watch implements ErrorCode {
        WATCH_NOT_FOUND(HttpStatus.NOT_FOUND, "등록된(폐기되지 않은) 워치가 아닙니다"),
        INVALID_WORLD_ID(HttpStatus.BAD_REQUEST, "world_id 는 1~64자여야 합니다"),
        BINDING_NOT_FOUND(HttpStatus.NOT_FOUND, "이 워치의 배정이 없습니다"),
        BINDING_CONFLICT(HttpStatus.CONFLICT, "같은 워치나 World ID 를 동시에 배정하고 있습니다. 다시 시도하세요"),
        WATCH_NOT_CONNECTED(HttpStatus.CONFLICT, "워치가 EC2 에 연결돼 있지 않아 보낼 수 없습니다"),
        WATCH_SEND_FAILED(HttpStatus.BAD_GATEWAY, "보내는 사이 워치 연결이 끊겨 보내지 못했습니다. 알림은 SEND_FAILED 로 남아 관리자 화면에 격상됩니다"),
        INVALID_ALERT(HttpStatus.BAD_REQUEST,
                "severity 는 CRITICAL·HIGH·CAUTION, word 는 1~8자(앞뒤 공백은 지움, 가운데 공백·줄바꿈 불가), direction 은 FRONT·BACK·LEFT·RIGHT, "
                        + "ack_within_ms 는 1000~600000 이어야 합니다");

        private final HttpStatus status;
        private final String message;

        Watch(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 시스템 지표 조회(S15P31A101-70) */
    enum Metric implements ErrorCode {
        INVALID_METRIC_QUERY(HttpStatus.BAD_REQUEST, "지표 조회 기간이 맞지 않습니다(from·to 는 UTC 밀리초, from ≤ to, 24시간 이내)");

        private final HttpStatus status;
        private final String message;

        Metric(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** Tier 0 엔드포인트 조회(S15P31A101-290) */
    enum Endpoint implements ErrorCode {
        // 노트북은 404 를 '아직 등록 없음'으로 보고 다시 조회한다(gateway/site_alert endpoint.py)
        TIER0_ENDPOINT_NOT_REGISTERED(HttpStatus.NOT_FOUND, "등록된 Tier 0 엔드포인트가 없습니다. Tier 0 가 등록할 때까지 다시 조회하세요");

        private final HttpStatus status;
        private final String message;

        Endpoint(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** I5 분석 작업 등록·대여(S15P31A101-229) */
    enum Job implements ErrorCode {
        INVALID_JOB(HttpStatus.BAD_REQUEST, "작업 유형·입력·시도 수·시간 제한이 계약(job_request)에 맞지 않습니다"),
        JOB_NOT_FOUND(HttpStatus.NOT_FOUND, "작업을 찾을 수 없습니다"),
        INVALID_WORKER_MESSAGE(HttpStatus.BAD_REQUEST, "워커 메시지가 계약(I5 2.x)에 맞지 않거나 경로의 job_id·작업 유형과 다릅니다"),
        // 계약 job_lease_request: 토큰의 워커 ID 와 봉투 source_id 가 다르면 401
        WORKER_SOURCE_MISMATCH(HttpStatus.UNAUTHORIZED, "봉투 source_id 가 워커 토큰의 워커 ID 와 다릅니다"),
        // 객체 서명(S15P31A101-248). 대여가 없음·남의 것·만료·끝남·작업 취소를 나누지 않는다. 워커는 그 작업을 멈춘다
        LEASE_NOT_ACTIVE(HttpStatus.CONFLICT, "이 작업의 유효한 대여가 아닙니다"),
        INVALID_OBJECT_REQUEST(HttpStatus.BAD_REQUEST, "객체 서명 요청(lease_id·op·이름·형식·크기·해시)이 맞지 않습니다"),
        OBJECT_NOT_IN_INPUT(HttpStatus.FORBIDDEN, "작업 입력에 없는 객체는 받을 수 없습니다");

        private final HttpStatus status;
        private final String message;

        Job(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** BEV 평면도(S15P31A101-240) */
    enum FloorPlan implements ErrorCode {
        // 화면은 404 를 '아직 평면도 없음'으로 보고 보조 화면을 비운다
        FLOOR_PLAN_NOT_READY(HttpStatus.NOT_FOUND, "아직 만들어진 BEV 평면도가 없습니다"),
        INVALID_FRAMES(HttpStatus.BAD_REQUEST, "프레임 출처·카메라 ID·객체 저장소 주소(버킷 안 s3://)가 맞지 않거나 같은 카메라가 두 번 있습니다");

        private final HttpStatus status;
        private final String message;

        FloorPlan(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 객체 저장소 임시 서명 주소(S15P31A101-69) */
    enum Storage implements ErrorCode {
        INVALID_UPLOAD(HttpStatus.BAD_REQUEST, "업로드 목적·형식·크기·해시·식별자가 맞지 않습니다"),
        UPLOAD_NOT_ALLOWED(HttpStatus.FORBIDDEN, "이 목적의 업로드 주소를 받을 수 없는 주체입니다"),
        // 같은 클립에 다른 내용(형식·크기·해시·요청 주체)으로 다시 받거나, 이미 기록된 클립을 덮어쓰려 했다
        UPLOAD_KEY_CONFLICT(HttpStatus.CONFLICT, "이미 다른 내용으로 발급했거나 기록된 객체입니다"),
        STORAGE_NOT_CONFIGURED(HttpStatus.SERVICE_UNAVAILABLE, "객체 저장소(SIMAI_STORAGE_BUCKET)가 설정되지 않았습니다"),
        STORAGE_UNAVAILABLE(HttpStatus.SERVICE_UNAVAILABLE, "객체 저장소 서명 주소를 만들 수 없습니다. 잠시 뒤 다시 시도하세요");

        private final HttpStatus status;
        private final String message;

        Storage(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 이벤트 클립 조회·보존 표시(S15P31A101-261) */
    enum Clip implements ErrorCode {
        CLIP_NOT_FOUND(HttpStatus.NOT_FOUND, "클립을 찾을 수 없습니다"),
        // 보관 기간(14일)이 지나 객체를 지웠다. 기록은 남아 목록에는 보인다
        CLIP_DELETED(HttpStatus.GONE, "보관 기간이 지나 삭제된 클립입니다"),
        // CRITICAL 이벤트 연결분은 자동 보존이라 떼지 못한다(명세 11.5)
        CLIP_AUTO_RETAINED(HttpStatus.CONFLICT, "CRITICAL 이벤트의 클립은 보존 표시를 뗄 수 없습니다"),
        INVALID_RETENTION(HttpStatus.BAD_REQUEST, "retention_hold(true·false)가 필요합니다");

        private final HttpStatus status;
        private final String message;

        Clip(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 현재 Scene 조회(S15P31A101-264) */
    enum Scene implements ErrorCode {
        // COMMITTED Scene 이 없고 시연용 기본 Scene(simai.scene.default-gltf-key)도 등록하지 않았다
        SCENE_NOT_FOUND(HttpStatus.NOT_FOUND, "확정된 Scene 이 없고 기본 Scene 도 등록되지 않았습니다"),
        // 이하 Scene 등록·확정(S15P31A101-322)
        INVALID_SCENE(HttpStatus.BAD_REQUEST, "Scene 값이 올바르지 않습니다(scene_version 은 소문자·숫자·. _ - 1~63자이고 current 가 아님, calibration_version·gltf_key 필수, quality 는 객체, 거절은 reason 500자 이하)"),
        SCENE_VERSION_NOT_FOUND(HttpStatus.NOT_FOUND, "Scene 을 찾을 수 없습니다"),
        SCENE_EXISTS(HttpStatus.CONFLICT, "같은 scene_version 이 이미 있습니다"),
        // 4.5: Scene 은 보정 위에서만 만든다. 등록은 PROVISIONAL·CALIBRATED 보정만 받는다
        SCENE_CALIBRATION_INVALID(HttpStatus.CONFLICT, "이 현장의 PROVISIONAL·CALIBRATED 보정 버전이 아닙니다"),
        SCENE_GLTF_NOT_UPLOADED(HttpStatus.BAD_REQUEST, "gltf_key 가 SCENE_GLTF 로 발급받아 아직 쓰이지 않은 업로드가 아닙니다(POST /api/uploads/sign)"),
        SCENE_STATE_CONFLICT(HttpStatus.CONFLICT, "확정·거절할 수 있는 상태(CANDIDATE)가 아닙니다"),
        // 서명 주소만 받고 올리지 않았거나 다른 크기다. 확정하면 화면 배경이 깨지므로 막는다
        SCENE_GLTF_MISSING(HttpStatus.CONFLICT, "glb 가 저장소에 없거나 발급한 크기와 다릅니다. 서명 주소로 올린 뒤 확정하세요"),
        // 3D 배경 재생성(S15P31A101-428)
        REGENERATE_IN_PROGRESS(HttpStatus.CONFLICT, "이미 진행 중인 3D 배경 재생성 요청이 있습니다"),
        // LIVE 관제(작업 IN_PROGRESS) 중에는 GPU 를 Tier 0 가 쓰므로 워커가 재생성을 대여하지 않아 10분 뒤 TIMEOUT 이 된다
        REGENERATE_WORK_IN_PROGRESS(HttpStatus.CONFLICT, "작업이 진행 중(LIVE 관제)일 때는 3D 배경을 다시 만들 수 없습니다. 작업을 끝낸 뒤 요청하세요"),
        REGENERATE_NOT_FOUND(HttpStatus.NOT_FOUND, "3D 배경 재생성 요청을 찾을 수 없습니다"),
        INVALID_REGENERATE_REQUEST(HttpStatus.BAD_REQUEST, "재생성 요청 값이 올바르지 않습니다(reason 은 문자열 120자 이하)");

        private final HttpStatus status;
        private final String message;

        Scene(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 리플레이 조회(S15P31A101-136) */
    enum Replay implements ErrorCode {
        INVALID_REPLAY_QUERY(HttpStatus.BAD_REQUEST, "조회 시각이 맞지 않습니다(from·to·at 은 UTC 밀리초, from < to, step_ms 는 100~60000)"),
        // 잘라서 주면 화면이 빠진 부분을 "기록 없음"으로 볼 수 있어 거부한다
        REPLAY_RANGE_TOO_LARGE(HttpStatus.BAD_REQUEST, "구간이 너무 큽니다. 구간을 나눠 부르거나 step_ms 를 늘리세요(구간 1시간, World 3000개, 이벤트 전이 5000개 이하)");

        private final HttpStatus status;
        private final String message;

        Replay(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 아바타 자산 조회(S15P31A101-325) */
    enum Avatar implements ErrorCode {
        AVATAR_ASSET_NOT_FOUND(HttpStatus.NOT_FOUND, "아바타 자산을 찾을 수 없습니다");

        private final HttpStatus status;
        private final String message;

        Avatar(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 상시 영상 조각 조회(S15P31A101-330) */
    enum Video implements ErrorCode {
        INVALID_VIDEO_QUERY(HttpStatus.BAD_REQUEST, "조회 시각이 맞지 않습니다(from·to 는 UTC 밀리초, from ≤ to, 24시간 이내)"),
        SEGMENT_NOT_FOUND(HttpStatus.NOT_FOUND, "영상 조각을 찾을 수 없습니다"),
        // 보관 기간이 지나 삭제 작업이 객체를 지웠다(기록은 남는다)
        SEGMENT_DELETED(HttpStatus.GONE, "보관 기간이 지나 삭제된 영상 조각입니다");

        private final HttpStatus status;
        private final String message;

        Video(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 현장 등록·수정(S15P31A101-322) */
    enum Site implements ErrorCode {
        INVALID_SITE(HttpStatus.BAD_REQUEST, "현장 값이 올바르지 않습니다(site_id 는 소문자·숫자·- 1~63자, name 1~100자, world_origin 은 객체)"),
        // MVP 는 현장 하나다(16장). 이름·ID 는 PUT /api/sites/current 로 바꾼다
        SITE_EXISTS(HttpStatus.CONFLICT, "현장이 이미 있습니다. PUT /api/sites/current 로 고치세요"),
        // 구역·보정·Scene·구성 묶음이 이 ID 에 붙은 뒤에는 ID 를 바꾸지 않는다
        SITE_IN_USE(HttpStatus.CONFLICT, "이 현장에 구역·보정·Scene 등이 이미 있어 site_id 를 바꿀 수 없습니다");

        private final HttpStatus status;
        private final String message;

        Site(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** TBM 작업(GET·POST·PUT·DELETE /api/work-units) */
    enum Work implements ErrorCode {
        WORK_ALREADY_IN_PROGRESS(HttpStatus.CONFLICT, "현장에 이미 진행 중인 작업이 있습니다"),
        WORK_INVALID_STATE(HttpStatus.CONFLICT, "현재 작업 상태에서는 요청한 전이를 수행할 수 없습니다"),
        WORK_UNIT_NOT_FOUND(HttpStatus.NOT_FOUND, "작업을 찾을 수 없습니다"),
        INVALID_WORK_UNIT(HttpStatus.BAD_REQUEST, "작업 값이 올바르지 않습니다(date YYYY-MM-DD, name 1~100자, from_ms < to_ms 이고 24시간 이내, planned_people 0~100, place 100자·lead 50자 이내)"),
        // 구역 밖 작업은 zone_id 를 비우고 place 에 장소를 적는다
        UNKNOWN_WORK_ZONE(HttpStatus.BAD_REQUEST, "없는 구역입니다"),
        INVALID_WORK_DAYS(HttpStatus.BAD_REQUEST, "days 는 1~90 이어야 합니다");

        private final HttpStatus status;
        private final String message;

        Work(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 가상 레이아웃(GET·POST·PUT /api/layouts, S15P31A101-30) */
    enum Layout implements ErrorCode {
        LAYOUT_NOT_FOUND(HttpStatus.NOT_FOUND, "레이아웃을 찾을 수 없습니다"),
        INVALID_LAYOUT(HttpStatus.BAD_REQUEST, "레이아웃 값이 올바르지 않습니다(name 1~100자, items 200개 이내, item_id 1~64자 겹치지 않게, kind 는 OPENING·SCAFFOLD·EQUIPMENT, center·size 는 유한한 수 3개(size > 0), yaw_rad 는 [-π, π])"),
        // 다른 사람이 먼저 고쳤다. 다시 불러와 고친다
        LAYOUT_REVISION_CONFLICT(HttpStatus.CONFLICT, "다른 요청이 먼저 레이아웃을 고쳤습니다. 다시 불러와 고치세요");

        private final HttpStatus status;
        private final String message;

        Layout(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** TBM 보고서(POST·GET /api/reports/tbm, S15P31A101-418) */
    enum Report implements ErrorCode {
        REPORT_NOT_FOUND(HttpStatus.NOT_FOUND, "보고서를 찾을 수 없습니다"),
        INVALID_REPORT_REQUEST(HttpStatus.BAD_REQUEST, "보고서 요청 값이 올바르지 않습니다(work_id 는 양의 정수)"),
        WORK_HAS_NO_LAYOUT(HttpStatus.CONFLICT, "작업에 연결된 레이아웃이 없습니다. 먼저 작업에 layout_id 를 연결하세요"),
        REPORT_IN_PROGRESS(HttpStatus.CONFLICT, "이 작업의 보고서를 이미 생성 중입니다"),
        REPORT_VERSION_STALE(HttpStatus.CONFLICT, "새 보고서 버전이 있습니다"),
        REPORT_APPROVED(HttpStatus.CONFLICT, "관리자 결재가 완료된 보고서입니다"),
        REPORT_NOT_READY(HttpStatus.CONFLICT, "보고서가 준비되지 않았습니다"),
        SIGNATURE_LIMIT_REACHED(HttpStatus.CONFLICT, "이 보고서 버전의 서명 인원 상한(50명)에 도달했습니다"),
        REPORT_LOCKED(HttpStatus.CONFLICT, "서명된 보고서 버전입니다"),
        INVALID_SIGNATURE(HttpStatus.BAD_REQUEST, "서명 요청이 올바르지 않습니다"),
        INVALID_DAILY_DATE(HttpStatus.BAD_REQUEST, "report_date 는 YYYY-MM-DD 이고 오늘(Asia/Seoul)보다 뒤일 수 없습니다"),
        DAILY_REPORT_IN_PROGRESS(HttpStatus.CONFLICT, "이 날짜의 일일 일지를 이미 생성 중입니다");

        private final HttpStatus status;
        private final String message;

        Report(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 현장 세팅 세션(GET·POST /api/setup-sessions, S15P31A101-452) */
    enum Setup implements ErrorCode {
        SETUP_SESSION_NOT_FOUND(HttpStatus.NOT_FOUND, "세팅 세션을 찾을 수 없습니다"),
        INVALID_SETUP_REQUEST(HttpStatus.BAD_REQUEST, "세팅 요청 값이 올바르지 않습니다"),
        CAMERA_HEIGHT_REQUIRED(HttpStatus.BAD_REQUEST, "모든 활성 카메라의 높이를 미터 단위로 입력해야 합니다"),
        SETUP_CAMERAS_INSUFFICIENT(HttpStatus.CONFLICT, "현장에 활성 카메라가 2대 이상 있어야 세팅을 시작할 수 있습니다"),
        SETUP_ALREADY_IN_PROGRESS(HttpStatus.CONFLICT, "이미 진행 중인 세팅 세션이 있습니다"),
        SETUP_WORKER_UNAVAILABLE(HttpStatus.CONFLICT, "현장 세팅(site_setup)을 처리할 활성 워커가 없습니다"),
        SETUP_CANCEL_NOT_ALLOWED(HttpStatus.CONFLICT, "이미 끝난 세팅 세션은 취소할 수 없습니다"),
        SETUP_RETRY_NOT_ALLOWED(HttpStatus.CONFLICT, "실패한 세팅 세션만 재시도할 수 있습니다"),
        SETUP_BLOCKED_BY_LIVE_WORK(HttpStatus.CONFLICT, "같은 현장에 진행 중(IN_PROGRESS)인 작업이 있어 현장 세팅을 시작할 수 없습니다"),
        INVALID_SETUP_PROGRESS(HttpStatus.BAD_REQUEST, "세팅 진행률·결과 보고가 계약이나 등록된 버전 계보에 맞지 않습니다");

        private final HttpStatus status;
        private final String message;

        Setup(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }

    /** 세팅 1단계 카메라 각도 헬퍼 중계(POST /api/devices/coverage-guides, GET /api/cameras/coverage-guides, S15P31A101-447) */
    enum CoverageGuide implements ErrorCode {
        INVALID_COVERAGE_GUIDE(HttpStatus.BAD_REQUEST, "카메라 각도 안내가 계약(messages/coverage_guide 1.x)에 맞지 않습니다"),
        COVERAGE_GUIDE_SOURCE_MISMATCH(HttpStatus.BAD_REQUEST, "coverage_guide source_id 가 기기 토큰의 기기 ID 와 다릅니다"),
        NOT_A_LAPTOP(HttpStatus.FORBIDDEN, "현장 노트북(LAPTOP)으로 등록된 기기만 카메라 각도 안내를 올릴 수 있습니다");

        private final HttpStatus status;
        private final String message;

        CoverageGuide(HttpStatus status, String message) {
            this.status = status;
            this.message = message;
        }

        @Override
        public HttpStatus status() {
            return status;
        }

        @Override
        public String message() {
            return message;
        }
    }
}
