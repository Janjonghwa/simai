-- S15P31A101-263: Tier 0 의 보정 상태 알림(I1 calibration_update)을 받아 GET /api/calibrations/current 로 돌려준다.
-- DRIFTED 표시는 새 보정 버전을 만들지 않고 같은 버전의 상태만 바꾼다(ai calibration/watch.py). 버전 행은 덮어쓰지 않으므로(AGENTS 6)
-- 받은 메시지를 추가만 하는 기록으로 남기고(msg_id 로 재전송 제거, 명세 12.1), 현재 상태는 LEAST(sent_at, received_at) 가 가장 늦은 것이다.
-- 보낸 시각으로 늦게 온 옛 재전송을 거르고, 받은 시각으로 상한을 둬 Tier 0 시계가 한 번 미래로 앞선 알림이 계속 현재로 남지 않게 한다.
-- V9 는 S15P31A101-69(upload_grant_content)가 쓴다.
CREATE TABLE calibration_update_message (
    msg_id uuid NOT NULL,
    gateway_id text NOT NULL,
    calibration_version text NOT NULL,
    calibration_state text NOT NULL CHECK (calibration_state IN ('UNCALIBRATED', 'PROVISIONAL', 'CALIBRATED', 'DRIFTED')),
    reason text NOT NULL,
    sent_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (msg_id)
);

ALTER TABLE calibration_update_message ADD CONSTRAINT calibration_update_message_calibration_version_fkey
    FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);

-- 현재 상태: LEAST(sent_at, received_at) 가 가장 늦은 것, 같으면 늦게 받은 것(CalibrationViews)
CREATE INDEX calibration_update_message_current_idx
    ON calibration_update_message ((LEAST(sent_at, received_at)) DESC, received_at DESC);
CREATE INDEX calibration_update_message_version_idx ON calibration_update_message (calibration_version);
-- 설치 안내는 현재 보정 버전의 것만 읽는다
CREATE INDEX coverage_guide_calibration_version_idx ON coverage_guide (calibration_version, created_at DESC);

COMMENT ON TABLE calibration_update_message IS 'I1 calibration_update 수신 기록(추가만). 현재 보정 상태의 근거';
COMMENT ON COLUMN calibration_update_message.sent_at IS '봉투 sent_at(Tier 0 시계). 받은 시각과 함께 알림 순서';
COMMENT ON COLUMN calibration_update_message.payload IS '받은 payload 원문(카메라별 운영 중 재투영 오차 포함)';
