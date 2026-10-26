package com.ssafy.simai.domain.tier0.i1;

/** 봉투·payload 가 계약과 맞지 않는 메시지. 연결은 끊지 않고 그 메시지만 버린다. 문구에 payload 값을 넣지 않는다. */
public class InvalidI1Message extends RuntimeException {

    public InvalidI1Message(String message) {
        super(message);
    }
}
