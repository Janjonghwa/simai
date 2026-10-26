package com.ssafy.simai.domain.tier0.i1;

import java.time.Instant;
import java.util.Set;

/** I1 메시지 종류 하나를 받아 저장한다. 빈으로 등록하면 I1MessageRouter 가 msgType 으로 찾아 부른다. */
public interface I1MessageIngestor {

    String msgType();

    /** 읽을 줄 아는 메이저 버전. 그 밖의 메이저는 의미가 달라졌을 수 있어 저장하지 않는다(명세 12.1). */
    Set<Integer> supportedMajors();

    /**
     * receivedAt 은 EC2 수신 시각이다. 같은 msg_id 를 다시 받아도 행이 늘지 않아야 한다(Tier 0 재전송, 12.1).
     * 계약에 맞지 않는 payload 는 InvalidI1Message 를 던진다. connection 은 보낸 Tier 0 연결이다(연결별 상태, 회신).
     */
    Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection);

    /**
     * 이 종류의 메시지를 계약 위반(형식·타입·읽을 수 없는 메이저)으로 버렸다. 다시 받아도 같으니 필요하면 건너뛴 것으로 친다
     * (저널 전이는 빈 구간이 영원히 남지 않게). DB 장애처럼 다시 받으면 될 수 있는 실패에는 부르지 않는다.
     */
    default void onRejected(I1Envelope envelope, I1Connection connection, String reason) {
    }

    /** STORED: 행을 쓰거나 바꿨다. DUPLICATE: 이미 같은 메시지가 저장돼 있어 아무것도 바꾸지 않았다. */
    enum Outcome {
        STORED,
        DUPLICATE
    }
}
