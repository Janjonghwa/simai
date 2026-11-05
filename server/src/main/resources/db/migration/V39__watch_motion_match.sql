-- simAI V39: 워치 걸음 특징 저장과 움직임 대조 배정 (S15P31A101-340, 확장 부록 C.6, FR-43·44, docs/design/server-watch-match.md)
-- V35(-338) 가 watch_binding 을 만들었고 V36(-33) 은 일일 일지다.

-- 워치가 보낸 gait_features 한 창(4초, 2초마다). measured_at 은 창 시작을 EC2 시각으로 바꾼 값이다
CREATE TABLE watch_feature (
    measured_at timestamptz NOT NULL,
    device_id text NOT NULL,
    features jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (measured_at, device_id)
);
SELECT create_hypertable('watch_feature', by_range('measured_at', INTERVAL '1 day'));
-- 워치 한 대가 하루 4만여 행이다. 대조·지연 측정(-341)에 쓰고 나면 필요 없어 system_metric 처럼 30일 둔다
SELECT add_retention_policy('watch_feature', INTERVAL '30 days');
COMMENT ON TABLE watch_feature IS '워치 가속도 걸음 특징(I4 gait_features payload 그대로, 확장 C.6). 시각은 payload 안은 워치 시계, measured_at 은 clock_offset_ms 로 바꾼 EC2 시각';

-- World ID 별 개인 채널 상태가 바뀐 때만 한 행(바뀌지 않으면 쓰지 않는다)
CREATE TABLE channel_state (
    observed_at timestamptz NOT NULL,
    world_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('BOUND', 'UNBOUND', 'LOST')),
    device_id text,
    confidence real,
    PRIMARY KEY (observed_at, world_id)
);
SELECT create_hypertable('channel_state', by_range('observed_at', INTERVAL '1 day'));
SELECT add_retention_policy('channel_state', INTERVAL '30 days');
COMMENT ON TABLE channel_state IS 'World ID 별 개인 채널 상태 전이(확장 C.6, FR-44). BOUND 연결·신뢰도 충분, UNBOUND 워치 없음·미확정·신뢰도 부족·ID 병합/분열 의심, LOST 배정된 워치가 끊김';

-- 대조 배정이 풀리는 사유를 더한다: 점수가 떨어지거나 트랙이 사라짐(MATCH_LOST), ID 병합·분할(IDENTITY_CHANGED)
ALTER TABLE watch_binding DROP CONSTRAINT watch_binding_unbind_reason_check;
ALTER TABLE watch_binding ADD CONSTRAINT watch_binding_unbind_reason_check
    CHECK (unbind_reason IN ('RELEASED', 'REPLACED', 'MATCH_LOST', 'IDENTITY_CHANGED'));
COMMENT ON COLUMN watch_binding.unbind_reason IS 'RELEASED 관리자가 풂, REPLACED 같은 워치·World ID 의 새 배정, MATCH_LOST 대조 점수가 떨어지거나 트랙이 사라짐, IDENTITY_CHANGED World ID 병합·분할';
