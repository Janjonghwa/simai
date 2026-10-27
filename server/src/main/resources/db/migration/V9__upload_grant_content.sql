-- S15P31A101-69: 업로드 서명 기록에 요청 주체 종류와 서명에 묶은 내용(형식·크기·해시)·이벤트를 더한다.
-- Tier 0 토큰으로 받은 발급은 기기·사용자 칸이 둘 다 비므로 requester 로 가른다. 내용 칸은 같은 클립 키를 다시 받을 때
-- 같은 내용인지 보고(다르면 거부), 올라온 키를 기록할 때(media_clip 등) 기록의 크기·해시와 맞춘다.
-- V9 이전에는 이 표에 쓰는 코드가 없어 행이 없다(NOT NULL 을 기본값 없이 더한다).
ALTER TABLE upload_grant ADD COLUMN requester text NOT NULL CHECK (requester IN ('TIER0', 'DEVICE', 'USER'));
ALTER TABLE upload_grant ADD COLUMN content_type text NOT NULL;
ALTER TABLE upload_grant ADD COLUMN size_bytes bigint NOT NULL CHECK (size_bytes > 0);
ALTER TABLE upload_grant ADD COLUMN sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE upload_grant ADD COLUMN event_id uuid;
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_requester_ref_check CHECK (
    (requester = 'DEVICE') = (requested_by_device IS NOT NULL)
    AND (requester = 'USER') = (requested_by_user IS NOT NULL));

COMMENT ON COLUMN upload_grant.requester IS '요청 주체 종류(TIER0 토큰·기기 토큰·사용자 JWT)';
COMMENT ON COLUMN upload_grant.content_type IS '서명에 묶은 Content-Type';
COMMENT ON COLUMN upload_grant.size_bytes IS '서명에 묶은 크기(Content-Length)';
COMMENT ON COLUMN upload_grant.sha256 IS '서명에 묶은 내용 SHA-256 16진수(x-amz-checksum-sha256)';
COMMENT ON COLUMN upload_grant.event_id IS '이벤트 프레임·클립의 이벤트(조치 사진은 비움)';
