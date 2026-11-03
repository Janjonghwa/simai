-- S15P31A101-452. V32는 -428 예약. 실패해도 마지막으로 제출한 실측 높이는 유지한다.
ALTER TABLE site ADD COLUMN default_camera_heights_m jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE setup_session (
    setup_session_id uuid PRIMARY KEY,
    site_id text NOT NULL REFERENCES site(site_id),
    work_unit_id bigint NOT NULL REFERENCES work_unit(work_id),
    retry_of uuid REFERENCES setup_session(setup_session_id),
    job_id text NOT NULL UNIQUE REFERENCES analysis_job(job_id),
    progress_lease_id uuid,
    status text NOT NULL CHECK (status IN ('COLLECTING_BACKGROUND', 'INITIAL_POSE_AND_WORLD',
        'JOINT_BA_AND_VERIFY', 'PREVIEW_AND_VLM_DRAFT', 'READY_FOR_ZONES', 'BAKING_TEXTURE', 'COMPLETED', 'FAILED')),
    progress_pct double precision NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    camera_heights_m jsonb NOT NULL,
    height_tolerance_cm double precision NOT NULL CHECK (height_tolerance_cm > 0),
    background_collection_pct jsonb NOT NULL DEFAULT '{}'::jsonb,
    height_checks jsonb,
    object_cross_checks jsonb,
    calibration_version text REFERENCES calibration_version(calibration_version),
    preview_scene_version text REFERENCES scene_version(scene_version),
    scene_version text REFERENCES scene_version(scene_version),
    config_version text REFERENCES config_bundle(config_version),
    zone_drafts_ref text,
    failure_code text,
    failure_message text,
    requested_by bigint NOT NULL REFERENCES app_user(user_id),
    started_at timestamptz NOT NULL,
    ready_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX setup_one_running_per_site ON setup_session(site_id)
    WHERE status NOT IN ('COMPLETED', 'FAILED');
CREATE INDEX setup_work_history ON setup_session(work_unit_id, started_at DESC);
ALTER TABLE work_unit ADD COLUMN active_setup_session_id uuid REFERENCES setup_session(setup_session_id);
COMMENT ON COLUMN setup_session.config_version IS '구역 발행 후 연결하는 구성 묶음. 미리보기 완료가 구성 발행은 아님';
COMMENT ON COLUMN site.default_camera_heights_m IS '성공 여부와 무관한 카메라별 마지막 제출 높이(m). 미입력은 키 없음';
