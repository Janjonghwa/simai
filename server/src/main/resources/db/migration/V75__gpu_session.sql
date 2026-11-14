-- GPU 세션 등록(S15P31A101-699, docs/design/S15P31A101-699-gpu-session-register.md, 계약 messages/gpu_session_register·_result·report).
-- GPU 인스턴스가 공급자 비밀 저장소로 받은 부팅 비밀값 하나로 EC2 에 세션을 등록하면 EC2 가 이번 세션의 워커·Tier 0 토큰을 발급한다.
-- 같은 시점 GPU 세션은 하나다(명세 3.12). 새 등록은 이전 세션을 끝낸다(SUPERSEDED).

-- 부팅 비밀값. 기기·워커 토큰과 같이 원문은 관리 명령이 한 번만 보여 주고 DB 에는 SHA-256 16진수만 둔다(명세 14).
-- 이 비밀값으로는 GPU 세션 등록·진행·종료 보고만 할 수 있다(SecurityConfig gpuBootFilterChain). 워커 ID 는 비밀값마다 하나로 묶는다:
-- 비밀값이 새도 그 워커 하나의 토큰만 바꿀 수 있다.
CREATE TABLE gpu_boot_secret (
    boot_id text NOT NULL,
    token_hash text NOT NULL,
    worker_id text NOT NULL REFERENCES worker (worker_id),
    issued_at timestamptz NOT NULL,
    rotated_at timestamptz,
    revoked_at timestamptz,
    PRIMARY KEY (boot_id)
);
CREATE UNIQUE INDEX gpu_boot_secret_token_hash_key ON gpu_boot_secret (token_hash);

COMMENT ON TABLE gpu_boot_secret IS 'GPU 부팅 비밀값(해시만). 관리 명령 admin gpu-boot 로만 발급·교체·폐기';
COMMENT ON COLUMN gpu_boot_secret.worker_id IS '이 비밀값으로 연 세션에 새 토큰을 발급할 워커 ID(하나)';

-- GPU 세션 한 번(등록 한 번). 등록 때 아는 사실만 한 번 쓰고 고치지 않는다. 진행·종료는 gpu_session_event 에 덧붙인다(AGENTS 6).
-- tier0_token_hash: 이번 세션 Tier 0 토큰의 SHA-256 16진수. 세션이 끝나지 않은 동안만 I1·업로드 서명에서 받는다.
CREATE TABLE gpu_session (
    session_id uuid NOT NULL,
    boot_id text NOT NULL REFERENCES gpu_boot_secret (boot_id),
    provider text NOT NULL CHECK (provider IN ('RUNPOD', 'AWS', 'MANUAL')),
    instance_id text NOT NULL,
    gpu_type text NOT NULL,
    gpu_count integer,
    region text,
    public_host text NOT NULL,
    ports jsonb NOT NULL,
    image_digest text,
    price_per_hour_usd numeric(10, 4),
    tls_public_key_sha256 text,
    worker_id text NOT NULL REFERENCES worker (worker_id),
    gateway_id text NOT NULL,
    tier0_token_hash text NOT NULL,
    registered_at timestamptz NOT NULL,
    register_msg_id uuid NOT NULL,
    PRIMARY KEY (session_id)
);
CREATE UNIQUE INDEX gpu_session_tier0_token_hash_key ON gpu_session (tier0_token_hash);
CREATE INDEX gpu_session_registered_idx ON gpu_session (registered_at DESC);

COMMENT ON TABLE gpu_session IS 'GPU 세션 등록(추가만). 상태는 gpu_session_event 의 마지막 행, 끝났는지는 ENDED 행 유무';
COMMENT ON COLUMN gpu_session.tier0_token_hash IS '이번 세션 Tier 0 토큰 SHA-256 16진수(원문은 등록 응답에서 한 번만)';
COMMENT ON COLUMN gpu_session.gateway_id IS '이번 세션 Tier 0 에 준 gateway_id';

-- GPU 세션 사건(추가만). REGISTERED 1행, 진행 보고(PROGRESS: stage·reason_code·detail·progress), ENDED 1행(end_reason).
-- received_at 은 EC2 수신 시각이고 무응답 판정은 이것으로 한다(인스턴스 시계 reported_at 은 기록만).
CREATE TABLE gpu_session_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY,
    session_id uuid NOT NULL REFERENCES gpu_session (session_id),
    kind text NOT NULL CHECK (kind IN ('REGISTERED', 'PROGRESS', 'ENDED')),
    stage text CHECK (stage IN ('WEIGHTS', 'ENGINE', 'TIER0', 'VLLM', 'READY', 'FAILED')),
    reason_code text,
    detail text,
    progress real,
    end_reason text CHECK (end_reason IN ('END_REPORTED', 'SUPERSEDED', 'STALE', 'BOOT_SECRET_REVOKED', 'ADMIN_STOPPED')),
    reported_at timestamptz,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (event_id),
    CHECK ((kind = 'PROGRESS') = (stage IS NOT NULL)),
    CHECK ((kind = 'ENDED') = (end_reason IS NOT NULL))
);
CREATE INDEX gpu_session_event_session_idx ON gpu_session_event (session_id, event_id DESC);
-- 끝은 한 번만. 동시에 끝내도(종료 보고·무응답·새 등록) 한 행만 들어가고 토큰 폐기도 그 한 번만 한다
CREATE UNIQUE INDEX gpu_session_event_ended_key ON gpu_session_event (session_id) WHERE kind = 'ENDED';

COMMENT ON TABLE gpu_session_event IS 'GPU 세션 등록·진행·종료 사건(추가만)';
COMMENT ON COLUMN gpu_session_event.end_reason IS
    'END_REPORTED 종료 보고, SUPERSEDED 새 등록에 밀림, STALE 무응답, BOOT_SECRET_REVOKED 부팅 비밀값 폐기, ADMIN_STOPPED 관리자 정지';
