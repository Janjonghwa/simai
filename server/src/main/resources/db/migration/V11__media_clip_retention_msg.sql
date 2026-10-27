-- S15P31A101-261: 이벤트 클립 기록(I1 media_clip)·보존 표시(I1 media_clip_retention, 관리자 PUT /api/clips/{id}/retention)를 받는다.
-- media_clip_retention 은 이력(추가만)이라 Tier 0 가 같은 메시지를 다시 보내면 행이 늘어난다. msg_id 로 재전송을 거른다(명세 12.1).
-- 관리자 기록은 메시지가 아니므로 비운다(NULL 은 유일 제약에 걸리지 않는다).
ALTER TABLE media_clip_retention ADD COLUMN msg_id uuid;
CREATE UNIQUE INDEX media_clip_retention_msg_id_key ON media_clip_retention (msg_id);
CREATE INDEX media_clip_retention_clip_id_idx ON media_clip_retention (clip_id, retention_id);

COMMENT ON COLUMN media_clip_retention.msg_id IS 'Tier 0 기록의 봉투 msg_id(재전송 제거). 관리자 기록은 비움';

-- 앞선 메시지를 아직 받지 못해 지금 처리할 수 없는 I1 메시지(이벤트 전이보다 먼저 온 클립, 클립보다 먼저 온 보존 기록).
-- 클립 기록은 저널 메시지가 아니라 Tier 0 가 다시 보내 주지 않으므로 버리지 않고 여기 두었다가 다시 처리한다(ClipDeferredRetrier).
CREATE TABLE i1_deferred_message (
    msg_id uuid NOT NULL,
    msg_type text NOT NULL,
    envelope jsonb NOT NULL,
    reason text NOT NULL,
    received_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL,
    PRIMARY KEY (msg_id)
);
CREATE INDEX i1_deferred_message_due_idx ON i1_deferred_message (next_attempt_at);

COMMENT ON TABLE i1_deferred_message IS '앞선 메시지를 기다리는 I1 메시지(받은 봉투 그대로). 처리되면 지운다';
COMMENT ON COLUMN i1_deferred_message.received_at IS 'EC2 가 처음 받은 시각. 저장할 때도 이 시각을 받은 시각으로 쓴다';
