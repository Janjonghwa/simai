-- S15P31A101-403: 분석 워커가 static_reconstruct 를 등록하기 전에 정적 3D 입력(사람 지운 배경 사진·보정 문서 JSON)을 올린다
-- (POST /api/uploads/sign, purpose STATIC_RECONSTRUCT_INPUT, 워커 토큰, 키 static-inputs/<bundle_id>/<이름>).
-- 번호: V22 는 S15P31A101-400(cctv_remote_usage)이 쓴다.
ALTER TABLE upload_grant DROP CONSTRAINT upload_grant_purpose_check;
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_purpose_check
    CHECK (purpose IN ('EVENT_FRAME', 'EVENT_CLIP', 'TICKET_PHOTO', 'AVATAR_ASSET', 'VIDEO_SEGMENT', 'SCENE_GLTF',
        'STATIC_RECONSTRUCT_INPUT'));
