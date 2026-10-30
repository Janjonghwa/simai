-- S15P31A101-322: 관리자가 정적 배경 Scene glTF(GLB)를 서명 주소로 올린다(POST /api/uploads/sign, purpose SCENE_GLTF).
-- 등록(POST /api/scenes)은 이 발급 기록으로 키를 확인하고 쓰인 것으로 표시한다. 발급 주체는 사람(requester USER, requested_by_user)이다.
ALTER TABLE upload_grant DROP CONSTRAINT upload_grant_purpose_check;
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_purpose_check
    CHECK (purpose IN ('EVENT_FRAME', 'EVENT_CLIP', 'TICKET_PHOTO', 'AVATAR_ASSET', 'VIDEO_SEGMENT', 'SCENE_GLTF'));
