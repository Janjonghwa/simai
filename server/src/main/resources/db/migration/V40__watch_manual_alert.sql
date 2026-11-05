-- simAI V40: 수동 워치 알림 (S15P31A101-475, 확장 부록 C.6, FR-45·46)
-- 관리자·반장이 POST /api/watch-alerts 로 워치에 직접 보낸 알림(origin MANUAL)도 같은 watch_alert 표에 남긴다.
-- MANUAL 은 위험 이벤트가 없다: event_id 는 그 알림만의 UUIDv7(계약 watch_alert.event_id 필수), source 는 NULL 이라 복합 FK(MATCH SIMPLE)가
-- 검사하지 않는다. EVENT 는 전과 같이 source LIVE + risk_event FK. 보낸 사람은 requested_by_user_id(사람 이름은 두지 않는다, 11.4-5).
ALTER TABLE watch_alert
    ADD COLUMN origin text NOT NULL DEFAULT 'EVENT' CHECK (origin IN ('EVENT', 'MANUAL')),
    ADD COLUMN requested_by_user_id bigint REFERENCES app_user (user_id),
    ALTER COLUMN source DROP NOT NULL,
    ALTER COLUMN world_id DROP NOT NULL,
    ADD CONSTRAINT watch_alert_origin_columns_check CHECK (
        (origin = 'EVENT' AND source IS NOT NULL AND world_id IS NOT NULL AND requested_by_user_id IS NULL)
        OR (origin = 'MANUAL' AND source IS NULL AND requested_by_user_id IS NOT NULL));
-- 기존 행은 EVENT 로 채운 뒤 기본값을 뺀다(새 행은 출처를 늘 명시한다)
ALTER TABLE watch_alert ALTER COLUMN origin DROP DEFAULT;

-- 조회 API(GET /api/watch-alerts)가 워치별 최근순으로 읽는다
CREATE INDEX watch_alert_device_sent_idx ON watch_alert (device_id, sent_at DESC);

COMMENT ON TABLE watch_alert IS '워치 진동 알림과 ACK·격상(확장 C.6, FR-45·46). EVENT 는 LIVE 이벤트만, MANUAL 은 관리자·반장이 직접 보낸 알림(S15P31A101-475)';
COMMENT ON COLUMN watch_alert.origin IS 'EVENT 위험 이벤트로 보냄(-338), MANUAL 관리자·반장이 POST /api/watch-alerts 로 보냄(-475)';
COMMENT ON COLUMN watch_alert.requested_by_user_id IS 'MANUAL 알림을 보낸 사용자(app_user). EVENT 는 NULL';
COMMENT ON COLUMN watch_alert.world_id IS '보낼 때 그 워치에 배정된 익명 World ID. MANUAL 은 배정이 없으면 NULL';
