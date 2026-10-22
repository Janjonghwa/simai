package com.ssafy.simai.global.command;

/** 관리 명령 옵션이 틀렸다. 사용법을 보여 주고 종료 코드 2 로 끝낸다. */
class CommandUsageException extends RuntimeException {

    CommandUsageException(String message) {
        super(message);
    }
}
