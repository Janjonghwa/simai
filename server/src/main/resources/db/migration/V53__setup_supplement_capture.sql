-- 세팅 맵 보강 촬영과 다시 굽기(S15P31A101-607, docs/design/S15P31A101-607-supplement-capture-flow.md, 계약 site_setup 1.10.0 -605).
-- 미리보기 맵이 나온 뒤 관리자가 휴대폰·PC(액션캠·DSLR 파일)로 따로 찍어 올린 동영상·사진을 세팅 세션 귀속 일회성 파일로 남기고,
-- 그 파일과 고정 카메라 4대 입력으로 맵만 다시 굽는 site_setup(SUPPLEMENT_REBAKE) 작업을 세션별로 남긴다.
-- 찍은 기기는 등록 카메라가 아니다(camera 표·스트림·관제에 들어가지 않는다). 위치·판정에도 들어가지 않는다(맵 Visual 층만).
-- V49(-602)·V50(-597)·V51(-609 TBM 양식)·V52(-606)를 피해 V53 을 쓴다(머지 순서 -602 → -597 → -609 핫픽스 → -606 → -607). 값은 추가만 하고, 목록에서 뺀 파일은 removed_at 만 남긴다(덮어쓰지 않음).

CREATE TABLE setup_supplement_media (
    setup_session_id uuid        NOT NULL REFERENCES setup_session(setup_session_id) ON DELETE CASCADE,
    media_id         text        NOT NULL CHECK (media_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    kind             text        NOT NULL CHECK (kind IN ('VIDEO', 'IMAGE')),
    source           text        CHECK (source IS NULL OR source IN ('PHONE_UPLOAD', 'FILE_UPLOAD')),
    content_type     text        NOT NULL,
    object_key       text        NOT NULL UNIQUE,
    size_bytes       bigint      NOT NULL CHECK (size_bytes > 0),
    sha256           text        CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    file_name        text        CHECK (file_name IS NULL OR length(file_name) <= 255),
    captured_at      timestamptz,
    device_make      text        CHECK (device_make IS NULL OR length(device_make) BETWEEN 1 AND 80),
    device_model     text        CHECK (device_model IS NULL OR length(device_model) BETWEEN 1 AND 80),
    requested_by     bigint      NOT NULL REFERENCES app_user(user_id),
    created_at       timestamptz NOT NULL,
    uploaded_at      timestamptz,
    removed_at       timestamptz,
    PRIMARY KEY (setup_session_id, media_id)
);

CREATE INDEX setup_supplement_media_live ON setup_supplement_media (setup_session_id, created_at) WHERE removed_at IS NULL;

COMMENT ON TABLE setup_supplement_media IS '세팅 맵 보강 촬영 파일(세션 귀속 일회성, 등록 카메라 아님). 업로드 서명을 낼 때 행을 만들고 올리기 확인(HEAD) 뒤 uploaded_at';
COMMENT ON COLUMN setup_supplement_media.sha256 IS '클라이언트가 계산해 보낸 내용 SHA-256(있을 때만 서명에 묶음). 큰 동영상은 비울 수 있다(모르면 비움)';
COMMENT ON COLUMN setup_supplement_media.captured_at IS '파일 메타데이터의 촬영 시각. 모르면 NULL(업로드 시각으로 채우지 않는다)';
COMMENT ON COLUMN setup_supplement_media.removed_at IS '관리자가 목록에서 뺀 시각. 다시 굽기 입력에 넣지 않는다';

CREATE TABLE setup_supplement_rebake (
    rebake_id                  uuid             PRIMARY KEY,
    setup_session_id           uuid             NOT NULL REFERENCES setup_session(setup_session_id) ON DELETE CASCADE,
    job_id                     text             NOT NULL UNIQUE REFERENCES analysis_job(job_id),
    status                     text             NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    stage                      text             CHECK (stage IS NULL OR stage IN ('BAKING_TEXTURE', 'COMPLETED')),
    progress_pct               double precision NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    progress_lease_id          uuid,
    media_ids                  jsonb            NOT NULL,
    base_calibration_version   text             NOT NULL,
    base_preview_scene_version text             NOT NULL,
    base_scene_version         text             NOT NULL,
    base_bake_job_id           text             NOT NULL,
    base_bake_lease_id         uuid             NOT NULL,
    outcome                    text             CHECK (outcome IS NULL OR outcome IN ('APPLIED', 'FALLBACK', 'SKIPPED')),
    result_scene_version       text,
    supplement                 jsonb,
    failure_code               text,
    failure_message            text,
    requested_by               bigint           NOT NULL REFERENCES app_user(user_id),
    requested_at               timestamptz      NOT NULL,
    completed_at               timestamptz,
    updated_at                 timestamptz      NOT NULL,
    CHECK ((outcome = 'APPLIED') = (result_scene_version IS NOT NULL))
);

-- 세션당 다시 굽기는 한 번에 하나
CREATE UNIQUE INDEX setup_supplement_rebake_one_running ON setup_supplement_rebake (setup_session_id)
    WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX setup_supplement_rebake_latest ON setup_supplement_rebake (setup_session_id, requested_at DESC);
CREATE INDEX setup_supplement_rebake_scene ON setup_supplement_rebake (result_scene_version) WHERE result_scene_version IS NOT NULL;

COMMENT ON TABLE setup_supplement_rebake IS '세팅 맵 보강 다시 굽기(site_setup SUPPLEMENT_REBAKE) 요청·진행·결과(추가만). 결과 요약 원문은 supplement';
COMMENT ON COLUMN setup_supplement_rebake.base_bake_job_id IS '기준 굽기 입력 사진(BAKE_IMAGE)을 등록한 원래 세팅 작업. 다시 구운 Scene 의 카메라 시점 사진도 이 사진을 쓴다';
COMMENT ON COLUMN setup_supplement_rebake.result_scene_version IS 'APPLIED 때 새로 등록한 Scene(세션 귀속 SCENE 후보). FALLBACK·SKIPPED 는 NULL(원래 맵 유지)';
