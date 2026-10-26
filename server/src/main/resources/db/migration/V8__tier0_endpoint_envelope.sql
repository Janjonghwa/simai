-- S15P31A101-290: Tier 0 엔드포인트 등록(I1 tier0_endpoint)을 저장하고 GET /api/endpoints/tier0 로 돌려준다.
-- 조회 응답은 Tier 0 가 보낸 봉투 그대로다(계약 tier0_endpoint 설명). V1 은 payload 칸만 있어 봉투 칸을 더한다.
-- msg_id 는 재전송(명세 12.1)을 한 행으로 막는다. V8 이전 행은 쓰는 코드가 없어 없어야 하고, 있으면 봉투를 만들 수 없어 조회에서 뺀다.
ALTER TABLE tier0_endpoint ADD COLUMN msg_id uuid;
ALTER TABLE tier0_endpoint ADD COLUMN schema_version text;
ALTER TABLE tier0_endpoint ADD COLUMN sent_at timestamptz;

CREATE UNIQUE INDEX tier0_endpoint_msg_id_key ON tier0_endpoint (msg_id);
-- 현재 엔드포인트: registered_at 이 가장 늦은 것, 같으면 늦게 받은 것
CREATE INDEX tier0_endpoint_current_idx ON tier0_endpoint (registered_at DESC, received_at DESC, endpoint_id DESC);

COMMENT ON COLUMN tier0_endpoint.msg_id IS '봉투 msg_id(재전송 제거)';
COMMENT ON COLUMN tier0_endpoint.schema_version IS '봉투 schema_version';
COMMENT ON COLUMN tier0_endpoint.sent_at IS '봉투 sent_at(Tier 0 시계)';
