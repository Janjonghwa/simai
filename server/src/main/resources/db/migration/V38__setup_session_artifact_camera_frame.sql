-- 세팅 카메라 프레임 보관(S15P31A101-479, docs/design/site-setup-pipeline.md 13절).
-- site_setup 워커가 artifacts.camera_frames(site_setup 1.3.0)로 알린 카메라별 대표 프레임(JPEG)을 이 세션 귀속으로 남긴다.
-- EC2 는 이 프레임 주소·얼굴 정책을 AI 구역 초안(vlm_zone_draft) 입력 frames 에 싣는다(미리보기 맵 주소만으로는 VLM 크롭이 없었다).
-- 같은 표를 쓰므로 kind 값만 늘리고(CAMERA_FRAME), 카메라 ID·얼굴 정책 열을 더한다. 추가만(덮어쓰지 않음).
-- 프레임의 version 은 "<camera_id>@<sha256 앞 12자>" 다(내용이 다르면 다른 행: 재시도가 다른 배경 묶음을 쓸 수 있다).

ALTER TABLE setup_session_artifact DROP CONSTRAINT setup_session_artifact_kind_check;
ALTER TABLE setup_session_artifact
    ADD CONSTRAINT setup_session_artifact_kind_check
        CHECK (kind IN ('CALIBRATION', 'PREVIEW_SCENE', 'SCENE', 'CAMERA_FRAME'));

ALTER TABLE setup_session_artifact ADD COLUMN camera_id text;
ALTER TABLE setup_session_artifact ADD COLUMN face_policy text;

ALTER TABLE setup_session_artifact
    ADD CONSTRAINT setup_session_artifact_frame_cols
        CHECK (
            (kind = 'CAMERA_FRAME' AND camera_id IS NOT NULL AND camera_id ~ '^[A-Za-z0-9_-]+$'
                AND face_policy IN ('BLURRED', 'EXCLUDED'))
            OR (kind <> 'CAMERA_FRAME' AND camera_id IS NULL AND face_policy IS NULL)
        );

CREATE INDEX setup_session_artifact_frame ON setup_session_artifact (setup_session_id, camera_id) WHERE kind = 'CAMERA_FRAME';

COMMENT ON COLUMN setup_session_artifact.camera_id IS 'CAMERA_FRAME 의 카메라 ID(그 밖의 종류는 NULL)';
COMMENT ON COLUMN setup_session_artifact.face_policy IS 'CAMERA_FRAME 의 VLM 입력 얼굴 정책(BLURRED·EXCLUDED, OBSERVED 금지). vlm_zone_draft 입력 frames[].face_policy 로 그대로 간다';
