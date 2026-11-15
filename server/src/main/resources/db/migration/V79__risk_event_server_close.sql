-- EC2 마감(S15P31A101-722, docs/design/S15P31A101-722-auto-close-open-events.md, 계약 risk_event 2.5.0 server_close_resolution).
-- 그 이벤트를 들고 있던 Tier 0 가 사라졌거나(작업 종료·GPU 세션 종료) 살아 있는 Tier 0 의 재동기화 스냅샷에 없어 해소 전이가
-- 다시 오지 않을 LIVE 이벤트를 EC2 가 닫은 기록이다. 관측 해소도 운영자 종료도 아니다(검증 상태를 바꾸지 않는다).
-- 추가만 한다(AGENTS 6). risk_event(현재 상태)에는 lifecycle RESOLVED·resolved_at·resolution(kind 와 이 행의 close_id)만 투영하고,
-- Tier 0 전이 기록(risk_event_update)에는 아무것도 넣지 않는다(EC2 는 Tier 0 전이를 만들지 않는다).
-- 같은 Tier 0 가 돌아와 그 이벤트의 전이를 다시 보내면 Tier 0 쪽을 따라 현재 상태를 되돌리고(이 행은 그대로), 다시 닫히면 새 행이 붙는다.
CREATE TABLE risk_event_server_close (
    close_id uuid NOT NULL,
    -- 운영은 risk_event 를 지우지 않는다. 지우는 것은 시험 정리·첫 운영 초기화뿐이라 이벤트와 함께 지운다(마감 기록은 이벤트 없이 뜻이 없다)
    event_id uuid NOT NULL REFERENCES risk_event (event_id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('WORK_ENDED', 'SESSION_ENDED', 'NOT_IN_SNAPSHOT')),
    gateway_id text NOT NULL,
    trigger_ref text,
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    lifecycle_before text NOT NULL CHECK (lifecycle_before IN ('RAISED', 'UPDATED')),
    last_event_sequence integer,
    closed_at timestamptz NOT NULL,
    PRIMARY KEY (close_id)
);
CREATE INDEX risk_event_server_close_event_idx ON risk_event_server_close (event_id, closed_at DESC);

COMMENT ON TABLE risk_event_server_close IS 'EC2 마감 기록(추가만). Tier 0 가 들고 있지 않아 해소 전이가 다시 오지 않을 LIVE 이벤트를 닫음. 관측 해소 아님';
COMMENT ON COLUMN risk_event_server_close.kind IS
    'WORK_ENDED 작업 종료(Tier 0 연결·열린 GPU 세션 없음), SESSION_ENDED GPU 세션 종료, NOT_IN_SNAPSHOT 살아 있는 Tier 0 스냅샷에 없음';
COMMENT ON COLUMN risk_event_server_close.trigger_ref IS 'WORK_ENDED 는 work_unit ID, SESSION_ENDED 는 GPU 세션 ID, NOT_IN_SNAPSHOT 은 gateway_id';
COMMENT ON COLUMN risk_event_server_close.last_event_sequence IS '닫을 때 반영돼 있던 마지막 Tier 0 event_sequence(모르면 NULL)';
