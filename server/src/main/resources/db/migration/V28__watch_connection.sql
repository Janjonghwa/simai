-- simAI V28: 워치 I4 연결 기록 (S15P31A101-336, 확장 부록 C.6, FR-44)
-- 워치가 /ws/watch 로 붙을 때 한 행, 끊기면 ended_at·end_reason 을 채운다. 지금 연결 상태는 EC2 메모리(WatchHub)가 정본이고,
-- 이 표는 끊김을 나중에 세는 근거다(-341 연결 비율, LOST 를 관리자에게 알린 근거). 하트비트마다 쓰지 않고 연결 경계에서만 쓴다.
CREATE TABLE watch_connection (
    connection_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    device_id text NOT NULL REFERENCES device (device_id),
    connected_at timestamptz NOT NULL,
    first_heartbeat_at timestamptz,
    last_heartbeat_at timestamptz,
    transport text CHECK (transport IN ('WIFI', 'PHONE_BLUETOOTH', 'CELLULAR', 'UNKNOWN')),
    app_version text,
    ended_at timestamptz,
    end_reason text CHECK (end_reason IN ('CLOSED', 'HEARTBEAT_TIMEOUT', 'TOKEN_INVALID', 'REPLACED', 'SEND_FAILED', 'EC2_RESTART')),
    PRIMARY KEY (connection_id),
    CONSTRAINT watch_connection_end_check CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);

-- 워치 하나에 열린 연결은 하나다(새 연결이 옛 연결을 REPLACED 로 닫는다)
CREATE UNIQUE INDEX watch_connection_one_open_idx ON watch_connection (device_id) WHERE ended_at IS NULL;

CREATE INDEX watch_connection_device_connected_idx ON watch_connection (device_id, connected_at DESC);

COMMENT ON TABLE watch_connection IS '워치 I4 연결 기록(확장 C.6). 연결·끊김 경계만 남긴다';
COMMENT ON COLUMN watch_connection.first_heartbeat_at IS '이 연결에서 처음 받은 watch_heartbeat 의 EC2 시각. 없으면 붙기만 하고 하트비트를 못 받음';
COMMENT ON COLUMN watch_connection.last_heartbeat_at IS '끊길 때 적는 마지막 watch_heartbeat 의 EC2 시각(연결 중에는 device.last_heartbeat_at 을 본다)';
COMMENT ON COLUMN watch_connection.end_reason IS 'CLOSED 워치·네트워크가 닫음, HEARTBEAT_TIMEOUT interval_ms×3 누락(LOST), TOKEN_INVALID 연결 중 토큰 폐기·회전, REPLACED 같은 워치의 새 연결, SEND_FAILED EC2 송신이 밀려 닫음, EC2_RESTART EC2 종료·재시작(끝 시각은 마지막으로 살아 있던 때)';
