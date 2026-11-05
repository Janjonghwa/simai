-- simAI V43: PPE 상태 수신 (S15P31A101-516, 명세 5.5·11장·12.3, docs/design/ppe-state-ingest.md)
-- I1 ppe_state(계약 1.0.0, 1~2 Hz)를 사람 한 명당 한 행으로 추가만 하고(ppe_state), 화면용 현재 상태는 따로 둔다(ppe_current_state).

-- 이력: 메시지 하나의 people 항목 하나가 한 행. observation_at 은 그 사람 상태의 촬영 시각(capture_time_ms)이다
CREATE TABLE ppe_state (
    observation_at timestamptz NOT NULL,
    source_id text NOT NULL,
    stream_epoch bigint NOT NULL,
    state_sequence bigint NOT NULL,
    world_id text NOT NULL,
    msg_id uuid NOT NULL,
    identity_revision integer NOT NULL,
    helmet_state text NOT NULL CHECK (helmet_state IN ('WORN', 'NOT_WORN', 'UNKNOWN')),
    helmet_reason text NOT NULL,
    helmet_valid_cells integer NOT NULL,
    helmet_support_ratio real NOT NULL,
    helmet_last_evidence_at timestamptz,
    helmet_expires_at timestamptz,
    helmet_evidence jsonb NOT NULL,
    vest_state text NOT NULL CHECK (vest_state IN ('WORN', 'NOT_WORN', 'UNKNOWN')),
    vest_reason text NOT NULL,
    vest_valid_cells integer NOT NULL,
    vest_support_ratio real NOT NULL,
    vest_last_evidence_at timestamptz,
    vest_expires_at timestamptz,
    vest_evidence jsonb NOT NULL,
    model_version text NOT NULL,
    checkpoint_sha256 text NOT NULL,
    policy_version text NOT NULL,
    input_mode text NOT NULL,
    input_provenance jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (observation_at, source_id, stream_epoch, state_sequence, world_id)
);
SELECT create_hypertable('ppe_state', by_range('observation_at', INTERVAL '1 day'));
-- 압축·보관은 world_track_state 와 같다(11.5): 7일 지난 청크 압축, 상시 삭제 없이 발표 후 일괄 파기.
-- 압축 청크에서도 PK 를 지키도록 PK 열을 모두 segmentby·orderby 에 넣는다(system_metric 과 같은 방식)
ALTER TABLE ppe_state SET (timescaledb.compress, timescaledb.compress_segmentby = 'world_id',
    timescaledb.compress_orderby = 'observation_at DESC, source_id, stream_epoch, state_sequence');
SELECT add_compression_policy('ppe_state', INTERVAL '7 days');
CREATE INDEX ppe_state_world_id_observation_at_desc_idx ON ppe_state (world_id, observation_at DESC);
-- 재전송 거르기: 멱등 키(source_id, stream_epoch, state_sequence)로 이미 받은 메시지인지 본다(12.3)
CREATE INDEX ppe_state_message_key_idx ON ppe_state (source_id, stream_epoch, state_sequence);

COMMENT ON TABLE ppe_state IS 'I1 ppe_state 이력(추가만, 5.5·11장). 메시지의 people 항목 하나가 한 행. 이미지·crop·얼굴은 싣지 않는다';
COMMENT ON COLUMN ppe_state.observation_at IS '그 사람 상태를 계산할 때 본 가장 새 원본 프레임의 촬영 시각(person_state.capture_time_ms, Tier 0 시계)';
COMMENT ON COLUMN ppe_state.stream_epoch IS 'PPE 프로세스 시작 시각(ms). 재시작하면 커진다. state_sequence 는 이 값 안에서만 비교한다';
COMMENT ON COLUMN ppe_state.helmet_evidence IS '안전모 근거(item_state.evidence 그대로: 프레임 키·local ID·후보 상태·confidence·영역 품질)';
COMMENT ON COLUMN ppe_state.vest_evidence IS '조끼 근거(item_state.evidence 그대로)';
COMMENT ON COLUMN ppe_state.received_at IS 'EC2 수신 시각';

-- 화면용 현재 상태: 사람마다 가장 새 상태 하나. 과거·만료된 메시지는 덮지 않는다(12.3)
CREATE TABLE ppe_current_state (
    source_id text NOT NULL,
    world_id text NOT NULL,
    identity_revision integer NOT NULL,
    observation_at timestamptz NOT NULL,
    stream_epoch bigint NOT NULL,
    state_sequence bigint NOT NULL,
    helmet_state text NOT NULL CHECK (helmet_state IN ('WORN', 'NOT_WORN', 'UNKNOWN')),
    helmet_reason text NOT NULL,
    helmet_last_evidence_at timestamptz,
    helmet_expires_at timestamptz,
    vest_state text NOT NULL CHECK (vest_state IN ('WORN', 'NOT_WORN', 'UNKNOWN')),
    vest_reason text NOT NULL,
    vest_last_evidence_at timestamptz,
    vest_expires_at timestamptz,
    model_version text NOT NULL,
    policy_version text NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (source_id, world_id)
);
COMMENT ON TABLE ppe_current_state IS '사람별 현재 PPE 상태(화면용, 5.5·12.3). (identity_revision, observation_at, stream_epoch, state_sequence) 가 더 큰 메시지만 덮는다. 읽는 쪽은 expires_at 이 지났으면 UNKNOWN 으로 보고, 현재 world_state 의 identity_revision 과 다르면 그 사람에게 붙이지 않는다';
COMMENT ON COLUMN ppe_current_state.updated_at IS '이 상태를 쓴 메시지의 EC2 수신 시각';
