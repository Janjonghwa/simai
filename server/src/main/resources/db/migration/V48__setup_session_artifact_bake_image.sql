-- 세팅 굽기 입력 사진 보관(S15P31A101-593, docs/design/S15P31A101-593-setup-bake-images.md).
-- site_setup 워커가 artifacts.bake_images(site_setup 1.6.0)로 알린 카메라별 사람 지운 배경 사진(미리보기·정식 GLB 를 구운 바로 그 파일)을
-- 이 세션·이 대여 귀속으로 남긴다. GET /api/scenes/{scene_version}/camera-images 가 같은 대여가 등록한 Scene(PREVIEW_SCENE·SCENE)의
-- 카메라 시점 VDT 사진으로 서명해 준다(Visual 층, 위치·판정에 들어가지 않음). 추가만(덮어쓰지 않음).
-- 사진의 version 은 "<camera_id>@<lease_id>" 다(대여마다 한 행: 재시도가 같은 묶음을 써도 그 대여의 Scene 에 묶인다).
-- 얼굴 정책은 외형 텍스처 규칙(AGENTS 규칙 8)이라 OBSERVED 도 받는다. bundle_id 는 그 사진의 Tier 0 배경 묶음 ID 다.

ALTER TABLE setup_session_artifact DROP CONSTRAINT setup_session_artifact_kind_check;
ALTER TABLE setup_session_artifact
    ADD CONSTRAINT setup_session_artifact_kind_check
        CHECK (kind IN ('CALIBRATION', 'PREVIEW_SCENE', 'SCENE', 'CAMERA_FRAME', 'BAKE_IMAGE'));

ALTER TABLE setup_session_artifact ADD COLUMN bundle_id text;

ALTER TABLE setup_session_artifact DROP CONSTRAINT setup_session_artifact_frame_cols;
ALTER TABLE setup_session_artifact
    ADD CONSTRAINT setup_session_artifact_frame_cols
        CHECK (
            (kind = 'CAMERA_FRAME' AND camera_id IS NOT NULL AND camera_id ~ '^[A-Za-z0-9_-]+$'
                AND face_policy IN ('BLURRED', 'EXCLUDED') AND bundle_id IS NULL)
            OR (kind = 'BAKE_IMAGE' AND camera_id IS NOT NULL AND camera_id ~ '^[A-Za-z0-9_-]+$'
                AND face_policy IN ('OBSERVED', 'BLURRED', 'EXCLUDED') AND bundle_id ~ '^bundle-[0-9a-f]{32}$')
            OR (kind NOT IN ('CAMERA_FRAME', 'BAKE_IMAGE') AND camera_id IS NULL AND face_policy IS NULL AND bundle_id IS NULL)
        );

CREATE INDEX setup_session_artifact_bake ON setup_session_artifact (setup_session_id, job_id, lease_id) WHERE kind = 'BAKE_IMAGE';

COMMENT ON COLUMN setup_session_artifact.camera_id IS 'CAMERA_FRAME·BAKE_IMAGE 의 카메라 ID(그 밖의 종류는 NULL)';
COMMENT ON COLUMN setup_session_artifact.face_policy IS 'CAMERA_FRAME 의 VLM 입력 얼굴 정책(BLURRED·EXCLUDED) 또는 BAKE_IMAGE 의 사진 얼굴 정책(OBSERVED·BLURRED·EXCLUDED, 외형 텍스처 규칙)';
COMMENT ON COLUMN setup_session_artifact.bundle_id IS 'BAKE_IMAGE 의 Tier 0 사람 지운 배경 묶음 ID(그 밖의 종류는 NULL)';
