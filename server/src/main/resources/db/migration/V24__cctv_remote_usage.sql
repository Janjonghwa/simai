-- CCTV 원격 시청 송출 사용 기록(S15P31A101-400, 설계 docs/design/cctv-remote-view.md).
-- EC2 가 관리자 브라우저에 보낸 바이트를 1분마다 한 줄씩 추가만 한다(덮어쓰지 않음, AGENTS 규칙 6). 영상 자체는 어디에도 저장하지 않는다.
-- 월(UTC 달력 월) 누적 = period_start 가 그 달인 줄의 합. 상한(초기 80 GB)을 넘으면 새 시청을 막고 이미 보던 연결도 닫는다.
CREATE TABLE cctv_remote_usage (
    usage_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    sent_bytes bigint NOT NULL CHECK (sent_bytes > 0),
    peak_viewers integer NOT NULL CHECK (peak_viewers >= 0),
    recorded_at timestamptz NOT NULL,
    CHECK (period_end >= period_start)
);

COMMENT ON TABLE cctv_remote_usage IS 'CCTV 원격 시청 송출 사용 기록(추가만). 영상은 저장하지 않는다';
COMMENT ON COLUMN cctv_remote_usage.period_start IS '이 줄이 센 구간의 시작(UTC). 월 누적은 이 값의 달로 묶는다';
COMMENT ON COLUMN cctv_remote_usage.sent_bytes IS '구간 동안 EC2 가 원격 시청자에게 보낸 WebSocket 본문 바이트(텍스트·이진 합)';
COMMENT ON COLUMN cctv_remote_usage.peak_viewers IS '구간 안 최대 동시 시청자 수';

CREATE INDEX cctv_remote_usage_period_start_idx ON cctv_remote_usage (period_start);
