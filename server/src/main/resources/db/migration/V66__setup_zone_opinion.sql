-- 변경 의심 구역 VLM 의견(vlm_zone_draft 1.1.0 zone_opinions, S15P31A101-631, docs/design/S15P31A101-631-zone-vlm-opinion.md).
-- 높이 비교(-630)가 변경 의심으로 판정한 이어 쓴 구역마다 이전·이번 세팅의 같은 카메라 크롭을 VLM 이 비교한 유지·수정·삭제 권고다.
-- 화면 표시용 제안이라 구역 목록·needs_review·관제 구역을 바꾸지 않고 Tier 0 판정에 쓰지 않는다(AGENTS 규칙 4).
-- 추가만 한다(AGENTS 규칙 6): 초안 작업(draft_run)마다 새 행이고, 화면은 세션의 지금 미리보기로 낸 작업의 행만 본다.
-- 사진은 새로 올리지 않는다. 전·후 CAMERA_FRAME 주소와 워커가 낸 정규화 크롭 박스를 남기고, 조회 때 서명 GET 주소를 만든다.
CREATE TABLE setup_zone_opinion (
    opinion_id                uuid         PRIMARY KEY,
    draft_run_id              uuid         NOT NULL REFERENCES setup_zone_draft_run(draft_run_id) ON DELETE CASCADE,
    setup_session_id          uuid         NOT NULL REFERENCES setup_session(setup_session_id) ON DELETE CASCADE,
    job_id                    varchar(64)  NOT NULL REFERENCES analysis_job(job_id) ON DELETE CASCADE,
    zone_id                   varchar(100) NOT NULL,
    status                    varchar(16)  NOT NULL CHECK (status IN ('OPINION', 'NO_OPINION')),
    reason                    varchar(64),
    recommended_action        varchar(16)  CHECK (recommended_action IN ('KEEP', 'MODIFY', 'REMOVE')),
    rationale                 varchar(200),
    confidence                real         CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    camera_id                 varchar(64),
    before_crop_box           jsonb,
    after_crop_box            jsonb,
    before_frame_uri          text,
    after_frame_uri           text,
    previous_setup_session_id uuid,
    model_version             varchar(160),
    prompt_version            varchar(160) NOT NULL,
    created_at                timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_setup_zone_opinion_run_zone UNIQUE (draft_run_id, zone_id),
    -- 의견 없음은 사유만 남기고 권고·이유를 지어내지 않는다(AGENTS 규칙 2)
    CONSTRAINT ck_setup_zone_opinion_status CHECK (
        (status = 'OPINION' AND reason IS NULL AND recommended_action IS NOT NULL AND rationale IS NOT NULL
            AND camera_id IS NOT NULL AND model_version IS NOT NULL)
        OR (status = 'NO_OPINION' AND reason IS NOT NULL AND recommended_action IS NULL AND rationale IS NULL
            AND confidence IS NULL AND model_version IS NULL)
    ),
    CONSTRAINT ck_setup_zone_opinion_crop CHECK (
        (camera_id IS NULL AND before_crop_box IS NULL AND after_crop_box IS NULL)
        OR (camera_id IS NOT NULL AND before_crop_box IS NOT NULL AND after_crop_box IS NOT NULL)
    )
);

CREATE INDEX idx_setup_zone_opinion_session ON setup_zone_opinion (setup_session_id, created_at DESC);
