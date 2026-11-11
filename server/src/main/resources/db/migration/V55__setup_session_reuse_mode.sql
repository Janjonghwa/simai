-- 세팅 모드: 새 모델(NEW) / 이전 모델 재사용(REUSE) (S15P31A101-614, docs/design/S15P31A101-614-setup-reuse-mode.md).
-- requested_mode: POST /api/setup-sessions 의 mode(재시도도 같은 값). mode: 실제로 따른 모드(site_setup 1.11.0 스냅샷 mode).
-- REUSE 를 요청했어도 World 다시 맞추기(world_anchor, -584)가 거부되면 같은 작업 안에서 새 모델로 이어 가며 mode=NEW, 이유는 reuse_fallback_reason.
-- world_anchor: 스냅샷의 World 다시 맞추기 결과 요약(accepted·reason·reference_calibration_version, 세팅 완료 -615 의 sameWorld 근거).
-- 이전 세션은 모두 NEW 다(기본값).

ALTER TABLE setup_session
    ADD COLUMN requested_mode text NOT NULL DEFAULT 'NEW' CHECK (requested_mode IN ('NEW', 'REUSE')),
    ADD COLUMN mode text NOT NULL DEFAULT 'NEW' CHECK (mode IN ('NEW', 'REUSE')),
    ADD COLUMN reuse_fallback_reason text,
    ADD COLUMN world_anchor jsonb CHECK (world_anchor IS NULL OR jsonb_typeof(world_anchor) = 'object');

ALTER TABLE setup_session ADD CONSTRAINT setup_session_reuse_mode_check
    CHECK ((mode = 'NEW' OR requested_mode = 'REUSE')
       AND (reuse_fallback_reason IS NULL OR (requested_mode = 'REUSE' AND mode = 'NEW')));

COMMENT ON COLUMN setup_session.requested_mode IS '요청한 세팅 모드(NEW 새 모델 / REUSE 이전 모델 재사용, -614)';
COMMENT ON COLUMN setup_session.mode IS '실제로 따른 세팅 모드. REUSE 요청이 World 다시 맞추기 거부로 새 모델로 이어 가면 NEW';
COMMENT ON COLUMN setup_session.reuse_fallback_reason IS 'REUSE 요청을 새 모델로 이어 간 이유(site_setup reuse_fallback_reason)';
COMMENT ON COLUMN setup_session.world_anchor IS 'World 다시 맞추기 결과 요약(site_setup world_anchor_summary). 받은 적 없으면 NULL';
