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
        INVALID_ACK_CHANNEL(HttpStatus.BAD_REQUEST, "channel 은 CONTROL_ROOM 또는 FOREMAN_PWA 여야 합니다");

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
}
