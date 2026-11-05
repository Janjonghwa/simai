-- 기존 토큰은 서로의 교체 관계를 알 수 없으므로 각각 독립 계보로 둔다.
ALTER TABLE refresh_token ADD COLUMN family_id UUID;
UPDATE refresh_token SET family_id = token_id;
ALTER TABLE refresh_token ALTER COLUMN family_id SET NOT NULL;
CREATE INDEX idx_refresh_token_family_id ON refresh_token (family_id);
