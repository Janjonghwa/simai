-- S15P31A101-330: 카메라 영상 상시 저장(팀 결정 2026-10-29, 명세 3장·5.1·11.3·11.5).
-- Tier 0 가 카메라별 조각을 객체 저장소에 올리고(POST /api/uploads/sign, purpose VIDEO_SEGMENT) I1 video_segment 로 알린다.
-- EC2 는 기록만 남기고 영상 바이트는 다루지 않는다. 보관 기간(14일)이 지나면 EC2 삭제 작업이 객체를 지우고 deleted_at 을 쓴다(행은 남김).

ALTER TABLE upload_grant DROP CONSTRAINT upload_grant_purpose_check;
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_purpose_check
    CHECK (purpose IN ('EVENT_FRAME', 'EVENT_CLIP', 'TICKET_PHOTO', 'AVATAR_ASSET', 'VIDEO_SEGMENT'));

-- E. video_segment (일반): 상시 영상 조각 기록. 받은 기록은 고치지 않는다(삭제 작업의 deleted_at 만 쓴다)
CREATE TABLE video_segment (
    segment_id uuid NOT NULL,
    gateway_id text NOT NULL,
    camera_id text NOT NULL,
    stream_epochs integer[] NOT NULL,
    segment_start_capture_at timestamptz NOT NULL,
    segment_end_capture_at timestamptz NOT NULL,
    frame_count integer NOT NULL CHECK (frame_count > 0),
    starts_with_keyframe boolean NOT NULL,
    has_gaps boolean NOT NULL,
    content_type text NOT NULL,
    container text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    sha256 text NOT NULL,
    face_redaction text NOT NULL CHECK (face_redaction = 'NONE'),
    segment_created_at timestamptz NOT NULL,
    delete_after timestamptz NOT NULL,
    object_key text NOT NULL UNIQUE,
    uploaded_at timestamptz NOT NULL,
    upload_attempts integer,
    received_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (segment_id),
    CHECK (segment_end_capture_at >= segment_start_capture_at)
);

-- 구간 조회(카메라·촬영 시각). 구간이 겹치는 조각을 찾으므로 시작 시각으로 좁힌다
CREATE INDEX video_segment_camera_start_idx ON video_segment (camera_id, segment_start_capture_at);
CREATE INDEX video_segment_start_idx ON video_segment (segment_start_capture_at);
-- 삭제 작업이 고르는 행
CREATE INDEX video_segment_overdue_idx ON video_segment (delete_after) WHERE deleted_at IS NULL;

-- 삭제 작업이 고르는 쓰이지 않은 조각 발급(조각 발급은 카메라당 분당 하나라 upload_grant 가 빨리 커진다)
CREATE INDEX upload_grant_unused_video_idx ON upload_grant (expires_at) WHERE used_at IS NULL AND purpose = 'VIDEO_SEGMENT';

-- E. video_access_log (일반): 상시 영상 열람 기록(14장 조회 기록, clip_access_log 와 같은 모양)
CREATE TABLE video_access_log (
    access_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    segment_id uuid NOT NULL REFERENCES video_segment (segment_id),
    user_id bigint NOT NULL REFERENCES app_user (user_id),
    accessed_at timestamptz NOT NULL,
    signed_url_expires_at timestamptz NOT NULL,
    PRIMARY KEY (access_id)
);

COMMENT ON TABLE video_segment IS 'Tier 0 가 올린 상시 영상 조각(I1 video_segment). 영상은 객체 저장소, 여기는 기록만';
COMMENT ON COLUMN video_segment.delete_after IS '삭제 예정일(Tier 0 가 정한 created + 14일). 지나면 EC2 삭제 작업이 객체를 지운다';
COMMENT ON COLUMN video_segment.deleted_at IS '삭제 작업이 객체를 지운 시각. 기록은 남는다';
COMMENT ON COLUMN video_access_log.signed_url_expires_at IS '발급한 임시 주소 만료';
