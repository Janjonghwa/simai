-- simAI V35: 워치 배정과 진동 알림 (S15P31A101-338, 확장 부록 C.6, FR-45·46)
-- watch_binding: 익명 World ID ↔ 워치 배정 이력. 지금은 관리자 임시 수동 배정(MANUAL)만, 움직임 대조(MOTION_MATCH)는 -340.
-- watch_alert: EC2 가 BOUND 워치에 보낸 watch_alert 와 ACK·격상. LIVE 이벤트만(복합 FK, notification 과 같은 두 겹 차단의 DB 쪽).
CREATE TABLE watch_binding (
    binding_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    device_id text NOT NULL REFERENCES device (device_id),
    world_id text NOT NULL,
    method text NOT NULL CHECK (method IN ('MANUAL', 'MOTION_MATCH')),
    score real NOT NULL,
    bound_by_user_id bigint REFERENCES app_user (user_id),
    bound_at timestamptz NOT NULL,
    unbound_at timestamptz,
    unbind_reason text CHECK (unbind_reason IN ('RELEASED', 'REPLACED')),
    PRIMARY KEY (binding_id),
    CONSTRAINT watch_binding_end_check CHECK ((unbound_at IS NULL) = (unbind_reason IS NULL)),
    CONSTRAINT watch_binding_manual_actor_check CHECK (method <> 'MANUAL' OR bound_by_user_id IS NOT NULL)
);

-- 1:1: 지금 배정은 워치마다 하나, World ID 마다 하나
CREATE UNIQUE INDEX watch_binding_active_device_idx ON watch_binding (device_id) WHERE unbound_at IS NULL;
CREATE UNIQUE INDEX watch_binding_active_world_idx ON watch_binding (world_id) WHERE unbound_at IS NULL;

COMMENT ON TABLE watch_binding IS '익명 World ID ↔ 워치 배정 이력(확장 C.6). 사람 이름을 두지 않는다';
COMMENT ON COLUMN watch_binding.score IS '움직임 대조 점수(0~1). MANUAL 은 1';
COMMENT ON COLUMN watch_binding.unbind_reason IS 'RELEASED 관리자가 풂, REPLACED 같은 워치·World ID 의 새 배정';

CREATE TABLE watch_alert (
    alert_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    alert_msg_id uuid NOT NULL UNIQUE,
    event_id uuid NOT NULL,
    source text NOT NULL CHECK (source IN ('LIVE')),
    event_sequence integer NOT NULL CHECK (event_sequence >= 1),
    resume_count integer NOT NULL,
    device_id text NOT NULL REFERENCES device (device_id),
    world_id text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('CRITICAL', 'HIGH', 'CAUTION')),
    vibration_pattern text NOT NULL CHECK (vibration_pattern IN ('DANGER_LONG', 'CAUTION_DOUBLE', 'CAUTION_WEAK')),
    direction text CHECK (direction IN ('FRONT', 'BACK', 'LEFT', 'RIGHT')),
    word text NOT NULL,
    needs_check boolean NOT NULL,
    ack_within_ms integer NOT NULL CHECK (ack_within_ms BETWEEN 1 AND 600000),
    sent_at timestamptz NOT NULL,
    acked_at timestamptz,
    ack_received_at timestamptz,
    confirmed_at timestamptz,
    confirm_received_at timestamptz,
    escalated_at timestamptz,
    escalation_reason text CHECK (escalation_reason IN ('NO_ACK', 'SEND_FAILED')),
    PRIMARY KEY (alert_id),
    CONSTRAINT watch_alert_event_fkey FOREIGN KEY (event_id, source) REFERENCES risk_event (event_id, source),
    CONSTRAINT watch_alert_escalation_check CHECK ((escalated_at IS NULL) = (escalation_reason IS NULL))
);

-- 같은 이벤트(재개마다 따로)·워치·등급은 한 번만 알린다(재전송·ACK·요약 갱신으로 이벤트가 다시 발행돼도). 등급이 오르면 다시 알린다.
-- 보내지 못한 알림(SEND_FAILED)은 세지 않아 워치가 다시 붙은 뒤 다시 보낼 수 있다
CREATE UNIQUE INDEX watch_alert_event_device_severity_idx ON watch_alert (event_id, resume_count, device_id, severity)
    WHERE escalation_reason IS DISTINCT FROM 'SEND_FAILED';
-- 격상 판정이 훑는 대기 알림
CREATE INDEX watch_alert_pending_idx ON watch_alert (sent_at) WHERE ack_received_at IS NULL AND escalated_at IS NULL;

COMMENT ON TABLE watch_alert IS '워치 진동 알림과 ACK·격상(확장 C.6, FR-45·46). LIVE 이벤트만';
COMMENT ON COLUMN watch_alert.resume_count IS '알린 때의 risk_event.resume_count. 해소 뒤 다시 활성(resumed, 6.4)이면 새로 알린다';
COMMENT ON COLUMN watch_alert.acked_at IS 'VIBRATED watch_ack 의 acked_at(워치 시계, 기록용)';
COMMENT ON COLUMN watch_alert.ack_received_at IS 'EC2 가 VIBRATED watch_ack 를 받은 EC2 시각. 격상 판단 기준(sent_at + ack_within_ms, CONFIRMED 를 받아도 받은 것으로 본다)';
COMMENT ON COLUMN watch_alert.confirmed_at IS 'CONFIRMED watch_ack 의 acked_at(작업자가 화면에서 확인, 워치 시계)';
COMMENT ON COLUMN watch_alert.escalation_reason IS 'NO_ACK 기한 안에 VIBRATED ACK 없음, SEND_FAILED 워치로 보내지 못함';
