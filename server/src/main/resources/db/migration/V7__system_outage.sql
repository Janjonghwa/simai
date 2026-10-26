-- simAI V7: 장애 기록 (S15P31A101-122, 기획서 6.5, FR-11, G3)
-- 관제 정지·현장 장비 응답 없음·원격 전달 단절이 시작·바뀜·끝날 때 한 행. 화면 알림(viewer_health)과 별도로
-- "단절 10초 안에 관리자 화면 알림"(G3)을 재는 근거다: last_seen_at(실패한 쪽의 마지막 정상 근거) → detected_at(EC2 판정).
CREATE TABLE system_outage (
    outage_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    kind text NOT NULL CHECK (kind IN ('CONTROL_HALTED', 'DEVICE_DOWN', 'REMOTE_LINK_LOST')),
    last_seen_at timestamptz,
    detected_at timestamptz NOT NULL,
    ended_at timestamptz,
    end_reason text CHECK (end_reason IN ('RECOVERED', 'CHANGED', 'EC2_RESTART')),
    details jsonb,
    PRIMARY KEY (outage_id),
    CONSTRAINT system_outage_end_check CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);

-- 한 시점에 열린 장애는 하나다(EC2 한 대가 기록한다)
CREATE UNIQUE INDEX system_outage_one_open_idx ON system_outage ((true)) WHERE ended_at IS NULL;

CREATE INDEX system_outage_detected_at_idx ON system_outage (detected_at DESC);

COMMENT ON TABLE system_outage IS '장애 기록(관제 정지·현장 장비 응답 없음·원격 전달 단절). G3 단절 감지 시간 근거';
COMMENT ON COLUMN system_outage.last_seen_at IS '실패한 쪽의 마지막 정상 근거(하트비트·health 수신, 처리 진행). 모르면 null';
COMMENT ON COLUMN system_outage.detected_at IS 'EC2 가 장애로 판정한 시각(받는 쪽 단조 시계로 3회 누락)';
