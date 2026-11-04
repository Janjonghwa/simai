-- VLM 위험구역 초안(vlm_zone_draft, S15P31A101-455).
-- 초안은 세팅 세션에 귀속된 제안이며 관리자가 수락해 구성 묶음(config_bundle)으로 발행하기 전까지 Tier 0 판정에 절대 들어가지 않는다(AGENTS 규칙 4·6).

CREATE TABLE setup_zone_draft_run (
    draft_run_id          uuid PRIMARY KEY,
    setup_session_id      uuid         NOT NULL REFERENCES setup_session(setup_session_id) ON DELETE CASCADE,
    job_id                varchar(64)  NOT NULL UNIQUE REFERENCES analysis_job(job_id) ON DELETE CASCADE,
    site_id               varchar(160) NOT NULL,
    calibration_version   varchar(160) NOT NULL,
    preview_scene_version varchar(160) NOT NULL,
    status                varchar(32)  NOT NULL CHECK (status IN ('QUEUED', 'SUCCEEDED', 'NO_OPINION', 'FAILED')),
    model_version         varchar(160),
    prompt_version        varchar(160),
    latency_ms            double precision,
    error_code            varchar(128),
    error_message         text,
    created_at            timestamptz  NOT NULL DEFAULT now(),
    completed_at          timestamptz,
    CONSTRAINT uq_setup_zone_draft_run_session_preview UNIQUE (setup_session_id, preview_scene_version)
);

CREATE INDEX idx_setup_zone_draft_run_session ON setup_zone_draft_run (setup_session_id, created_at DESC);

CREATE TABLE setup_zone_draft (
    draft_id             uuid PRIMARY KEY,
    draft_run_id         uuid         NOT NULL REFERENCES setup_zone_draft_run(draft_run_id) ON DELETE CASCADE,
    setup_session_id     uuid         NOT NULL REFERENCES setup_session(setup_session_id) ON DELETE CASCADE,
    job_id               varchar(64)  NOT NULL REFERENCES analysis_job(job_id) ON DELETE CASCADE,
    draft_index          integer      NOT NULL CHECK (draft_index >= 0),
    candidate_id         varchar(128) NOT NULL,
    candidate_kind       varchar(64)  NOT NULL CHECK (candidate_kind IN ('ELEVATED_WORK_SURFACE', 'OPENING', 'FLOOR_EDGE')),
    zone_kind            varchar(64)  NOT NULL,
    eval_type            varchar(32)  NOT NULL CHECK (eval_type IN ('GROUND_FOOTPRINT', 'VOLUME_3D')),
    polygon_xy           jsonb        NOT NULL,
    z_min_m              real         NOT NULL,
    z_max_m              real         NOT NULL,
    top_height_m         real,
    obb                  jsonb,
    recommended_name     varchar(160) NOT NULL,
    recommended_severity varchar(32)  NOT NULL CHECK (recommended_severity IN ('CRITICAL', 'HIGH')),
    rationale            varchar(500) NOT NULL,
    vlm_status           varchar(32)  NOT NULL CHECK (vlm_status IN ('VLM', 'NO_OPINION')),
    evidence_cameras     jsonb        NOT NULL DEFAULT '[]'::jsonb,
    confidence           real,
    created_at           timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_setup_zone_draft_run_index UNIQUE (draft_run_id, draft_index)
);

CREATE INDEX idx_setup_zone_draft_session ON setup_zone_draft (setup_session_id, draft_index);
