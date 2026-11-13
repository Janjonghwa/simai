-- S15P31A101-668 (V68, V67 은 -669): 관제 녹화의 작업 구간을 관제가 끝난 뒤 게이트웨이(노트북)가 S3 에 올린다(사용자 결정 2026-11-13).
-- 새 표를 만들지 않고 상시 영상 조각 표(video_segment, V15)의 두 번째 생산자로 붙인다. 삭제 작업(14일)·관리자 재생 주소·조회 기록을 그대로 쓴다.
-- 설계 docs/design/S15P31A101-668-recording-s3-playback.md, 계약 messages/recording_segment·recording_windows 1.0.0.

-- 업로드 목적: 게이트웨이 녹화 조각(기기만, mp4 만). Tier 0 전용 VIDEO_SEGMENT 와 주체·형식이 달라 따로 둔다
ALTER TABLE upload_grant DROP CONSTRAINT upload_grant_purpose_check;
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_purpose_check
    CHECK (purpose IN ('EVENT_FRAME', 'EVENT_CLIP', 'TICKET_PHOTO', 'AVATAR_ASSET', 'VIDEO_SEGMENT', 'SCENE_GLTF',
                       'REPORT_SIGNATURE', 'STATIC_RECONSTRUCT_INPUT', 'RECORDING_SEGMENT'));

-- 조각 출처. 예전 행은 모두 Tier 0 조각이다. 게이트웨이 조각은 작업·녹화 세션을 함께 남긴다(조회·정리용, 추가 전용이라 FK 를 두지 않는다)
ALTER TABLE video_segment
    ADD COLUMN origin text NOT NULL DEFAULT 'TIER0' CHECK (origin IN ('TIER0', 'GATEWAY_RECORDING')),
    ADD COLUMN work_id bigint,
    ADD COLUMN recording_id text,
    ADD CONSTRAINT video_segment_gateway_recording_check
        CHECK (origin <> 'GATEWAY_RECORDING' OR (work_id IS NOT NULL AND recording_id IS NOT NULL));

-- 삭제 작업이 고르는 쓰이지 않은 녹화 조각 발급(VIDEO_SEGMENT 부분 색인과 같은 모양)
CREATE INDEX upload_grant_unused_recording_idx ON upload_grant (expires_at)
    WHERE used_at IS NULL AND purpose = 'RECORDING_SEGMENT';

COMMENT ON COLUMN video_segment.origin IS 'TIER0: Tier 0 상시 조각(I1 video_segment). GATEWAY_RECORDING: 관제가 끝난 뒤 게이트웨이가 올린 작업 구간 mp4(recording_segment)';
COMMENT ON COLUMN video_segment.work_id IS '게이트웨이 조각이 속한 작업(work_unit.work_id). Tier 0 조각은 비움';
COMMENT ON COLUMN video_segment.recording_id IS '게이트웨이 녹화 세션 ID(녹화 폴더 이름). Tier 0 조각은 비움';
