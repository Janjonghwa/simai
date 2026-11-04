-- =============================================================================
-- V28: 3D 배경(Scene) 재생성 요청 이력 및 상태 전이 추적 (S15P31A101-428)
-- =============================================================================
-- 관리자가 POST /api/scenes/regenerate 로 요청한 3D 배경 재생성의 단계
-- (REQUESTED -> CAPTURING -> RECONSTRUCTING -> CANDIDATE_READY | FAILED)를 추적한다.
-- 동시에 진행 중인 요청(REQUESTED·CAPTURING·RECONSTRUCTING)은 최대 1개로 제한한다.

CREATE TABLE scene_regenerate_request (
    id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status                    text        NOT NULL,
    failure_reason            text,
    reason                    text,
    requested_by              bigint      NOT NULL REFERENCES app_user (user_id),
    job_id                    text        REFERENCES analysis_job (job_id),
    scene_version             text        REFERENCES scene_version (scene_version) ON DELETE SET NULL,
    static_reconstruct_job_id text        REFERENCES analysis_job (job_id),
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_scene_regenerate_status
        CHECK (status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING', 'CANDIDATE_READY', 'FAILED')),
    CONSTRAINT chk_scene_regenerate_failure_reason
        CHECK (failure_reason IS NULL OR failure_reason IN (
            'NO_WORKER',
            'TIER0_NOT_RUNNING',
            'BACKGROUND_NOT_READY',
            'UPLOAD_FAILED',
            'RECONSTRUCT_FAILED',
            'TIMEOUT'
        )),
    CONSTRAINT chk_scene_regenerate_reason_len
        CHECK (reason IS NULL OR char_length(reason) <= 120)
);

-- 동시에 진행 중인 재생성 요청은 현장 전체에서 최대 1개만 허용
CREATE UNIQUE INDEX uq_scene_regenerate_request_in_progress
    ON scene_regenerate_request ((1))
    WHERE status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING');

CREATE INDEX idx_scene_regenerate_request_job_id
    ON scene_regenerate_request (job_id)
    WHERE job_id IS NOT NULL;

CREATE INDEX idx_scene_regenerate_request_reconstruct_job_id
    ON scene_regenerate_request (static_reconstruct_job_id)
    WHERE static_reconstruct_job_id IS NOT NULL;
