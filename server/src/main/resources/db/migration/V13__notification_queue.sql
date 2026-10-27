-- S15P31A101-123: LIVE 이벤트만 알림을 보낸다(명세 7.4 출처 분리, 11.4-3). DB 기록 차단(source='LIVE' CHECK + risk_event(event_id, source)
-- 복합 FK)은 V1 에 있다. 이 마이그레이션은 발송 쪽(NotificationQueue)이 쓰는 칸과 색인만 더한다.
-- 같은 전이(event_id, event_sequence)를 같은 채널·대상으로 두 번 넣지 않는다(Tier 0 재전송·EC2 재처리에도 알림은 한 번).
-- 대상이 없는 칸(NULL)은 유일 비교에서 서로 다르게 보이므로 NULLS NOT DISTINCT 로 묶는다(PostgreSQL 15+, 운영 17).
CREATE UNIQUE INDEX notification_transition_target_key
    ON notification (event_id, event_sequence, channel, subscription_id, target_user_id) NULLS NOT DISTINCT;

-- 발송 워커가 꺼낸 알림의 임대 기한. 꺼내기는 한 문장으로 이 칸을 쓰고 바로 커밋하므로, 행 잠금이나 열린 트랜잭션을 쥔 채
-- 외부 푸시를 부르지 않는다. 기한 안에 SENT·FAILED 로 끝내지 않으면(워커가 죽음) 다시 꺼낼 수 있다.
ALTER TABLE notification ADD COLUMN claimed_until timestamptz;
COMMENT ON COLUMN notification.claimed_until IS '발송 워커 임대 기한(QUEUED 일 때만 뜻이 있다). 비어 있거나 지났으면 꺼낼 수 있다';

-- 발송 워커가 꺼낼 대기 알림
CREATE INDEX notification_queued_idx ON notification (notification_id) WHERE status = 'QUEUED';
