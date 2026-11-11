-- 바닥 감시 범위 보관(S15P31A101-606, docs/design/S15P31A101-606-coverage-overlay.md).
-- site_setup 워커가 artifacts.floor_coverage(site_setup 1.9.0)로 알린 바닥 칸별 카메라 수(0·1·2대 이상)를 그 보정 버전에 묶어 남긴다.
-- GET /api/scenes/current 가 Scene 의 calibration_version 과 같은 행을 floor_coverage 로 싣고, 관제 화면은 Tier 0 가 지금 쓰는
-- 보정 버전과 같을 때만 바닥 겹침으로 그린다(Visual 층, Tier 0 위치·위험 판정에 들어가지 않음).
-- 보정 버전마다 한 행이고 추가만 한다(덮어쓰지 않음, AGENTS 규칙 6). 같은 보정을 재시도 대여가 다시 알리면 먼저 등록한 것을 둔다.
-- V49 는 S15P31A101-602, V50 은 -597, V51 은 -609(TBM 양식) 몫이라 V52 를 쓴다. outOfOrder 를 켜지 않았으므로 V52 가 먼저 배포되면 뒤에 들어온 앞 번호는
-- 검증에서 막힌다. V49·V50 이 먼저 머지되지 않으면 머지 직전에 번호를 다시 고른다.

CREATE TABLE floor_coverage (
    calibration_version text PRIMARY KEY REFERENCES calibration_version (calibration_version),
    site_id text NOT NULL REFERENCES site (site_id),
    content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    setup_session_id uuid NOT NULL REFERENCES setup_session (setup_session_id),
    job_id text NOT NULL,
    lease_id uuid NOT NULL,
    registered_at timestamptz NOT NULL
);

COMMENT ON TABLE floor_coverage IS '보정 버전별 바닥 감시 범위(site_setup artifacts.floor_coverage 원문). 표시용, 추가만';
COMMENT ON COLUMN floor_coverage.content IS 'site_setup#/$defs/floor_coverage 문서(calibration_version·cell_m·origin_xy·cols·rows·cells 등)';
