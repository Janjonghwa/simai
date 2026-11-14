-- GPU 켜기·끄기·자동 정지와 GPU 설정(S15P31A101-702, docs/design/S15P31A101-702-gpu-provider-runpod.md).
-- 팀원(관리자)이 화면에서 공급자(RunPod 먼저, 나중 AWS -703) GPU 를 켜고 끈다. 한 시점 GPU 는 하나다(명세 3.12).
-- 모두 추가만 한다(AGENTS 6): 설정은 새 버전, 켠 요청은 한 번, 그 뒤 일은 사건 행으로 덧붙인다.

-- GPU 설정. 저장할 때마다 새 버전(가장 큰 version_id 가 지금 설정). 비밀 값(API 키·부팅 비밀값)은 두지 않는다.
CREATE TABLE gpu_settings_version (
    version_id bigint GENERATED ALWAYS AS IDENTITY,
    provider text NOT NULL CHECK (provider IN ('RUNPOD', 'AWS')),
    -- 공급자의 GPU 종류 ID(RunPod gpuTypeIds 값, 예: NVIDIA H100 80GB HBM3). 앞이 먼저다
    preferred_gpu_types jsonb NOT NULL CHECK (jsonb_typeof(preferred_gpu_types) = 'array'),
    -- 공급자의 지역 ID(RunPod dataCenterIds 값, 예: AP-JP-1). 앞이 먼저다
    preferred_regions jsonb NOT NULL CHECK (jsonb_typeof(preferred_regions) = 'array'),
    cloud_type text NOT NULL CHECK (cloud_type IN ('SECURE', 'COMMUNITY')),
    -- true 면 스팟(중단될 수 있음, RunPod interruptible)
    interruptible boolean NOT NULL,
    -- 시간당 가격 상한(USD, GPU 1대). NULL 이면 상한 없음
    max_price_per_hour_usd numeric(10, 4) CHECK (max_price_per_hour_usd IS NULL OR max_price_per_hour_usd > 0),
    -- 유휴 이만큼이면 자동 정지. 0 이면 유휴 자동 정지를 하지 않는다(시연 당일 하루 유지)
    auto_stop_idle_minutes integer NOT NULL CHECK (auto_stop_idle_minutes BETWEEN 0 AND 1440),
    -- RunPod 템플릿 ID(이미지·포트·디스크·비밀 참조 환경 변수). 비밀 아님
    runpod_template_id text,
    created_at timestamptz NOT NULL,
    created_by bigint REFERENCES app_user (user_id),
    PRIMARY KEY (version_id)
);

COMMENT ON TABLE gpu_settings_version IS 'GPU 설정 버전(추가만). 가장 큰 version_id 가 지금 설정, 없으면 서버 기본값';

-- 켠 요청 한 번(누가·언제·무엇을). 공급자에 만들기를 부르기 전에 쓰고 고치지 않는다.
-- 이 행이 있고 끝 사건(CREATE_FAILED·STOPPED·GONE)이 없으면 "켜져 있거나 켜는 중"이다(한 번에 하나).
CREATE TABLE gpu_launch (
    launch_id uuid NOT NULL,
    provider text NOT NULL CHECK (provider IN ('RUNPOD', 'AWS')),
    settings_version_id bigint REFERENCES gpu_settings_version (version_id),
    gpu_type text NOT NULL,
    region text NOT NULL,
    cloud_type text NOT NULL CHECK (cloud_type IN ('SECURE', 'COMMUNITY')),
    interruptible boolean NOT NULL,
    -- 켤 때 공급자 목록의 시간당 가격(USD). 모르면 NULL
    price_per_hour_usd numeric(10, 4),
    -- 자동으로 고른 것인지(사용자가 표에서 바꾸면 false)
    auto_selected boolean NOT NULL,
    requested_by bigint REFERENCES app_user (user_id),
    requested_at timestamptz NOT NULL,
    PRIMARY KEY (launch_id)
);
CREATE INDEX gpu_launch_requested_idx ON gpu_launch (requested_at DESC);

-- 켠 요청의 뒤 일(추가만).
-- CREATED(공급자가 인스턴스를 만듦, instance_id), CREATE_FAILED(못 만듦, 끝), STOP_REQUESTED(끄기 시작), STOPPED(껐음, 끝),
-- STOP_FAILED(끄기 실패, 다시 시도), GONE(공급자에 인스턴스가 없음, 끝).
-- actor: 관리자 user_id 또는 NULL(서버 자동). reason_code: 자동 정지 사유·실패 코드
CREATE TABLE gpu_launch_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY,
    launch_id uuid NOT NULL REFERENCES gpu_launch (launch_id),
    kind text NOT NULL CHECK (kind IN ('CREATED', 'CREATE_FAILED', 'STOP_REQUESTED', 'STOPPED', 'STOP_FAILED', 'GONE')),
    instance_id text,
    reason_code text,
    detail text,
    actor_user_id bigint REFERENCES app_user (user_id),
    at timestamptz NOT NULL,
    PRIMARY KEY (event_id),
    CHECK (kind <> 'CREATED' OR instance_id IS NOT NULL)
);
CREATE INDEX gpu_launch_event_launch_idx ON gpu_launch_event (launch_id, event_id DESC);
-- 만들기 결과와 끝은 한 번만
CREATE UNIQUE INDEX gpu_launch_event_created_key ON gpu_launch_event (launch_id) WHERE kind IN ('CREATED', 'CREATE_FAILED');
CREATE UNIQUE INDEX gpu_launch_event_end_key ON gpu_launch_event (launch_id) WHERE kind IN ('CREATE_FAILED', 'STOPPED', 'GONE');
-- 세션(G1 등록)과 잇는다: 공급자 + 인스턴스 ID
CREATE INDEX gpu_launch_event_instance_idx ON gpu_launch_event (instance_id) WHERE kind = 'CREATED';

COMMENT ON TABLE gpu_launch IS 'GPU 켠 요청(추가만). 세션(gpu_session)과는 provider + instance_id 로 잇는다';
COMMENT ON TABLE gpu_launch_event IS 'GPU 켠 요청의 만들기·끄기 사건(추가만)';
