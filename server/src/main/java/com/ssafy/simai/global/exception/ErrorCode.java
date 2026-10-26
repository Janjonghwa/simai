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
        INVALID_EVENT_SEQUENCE(HttpStatus.BAD_REQUEST, "event_sequence 는 1 이상이어야 합니다");

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
}
