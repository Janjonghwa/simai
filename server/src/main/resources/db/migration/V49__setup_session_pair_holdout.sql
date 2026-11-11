-- 세팅 세션 쌍별 홀드아웃 재투영 오차(S15P31A101-602, docs/design/S15P31A101-602-setup-camera-summary.md).
-- site_setup 워커가 하트비트·결과 metrics.joint_ba 로 알린 pair_holdout{before,after}·pair_holdout_median_px·
-- holdout_worsening_tolerance_px 를 GET /api/setup-sessions/{id} 의 pair_holdout 으로 돌려주려고 남긴다.
-- 높이 검사(height_checks)·교차 확인(object_cross_checks)처럼 세션의 최신 표시 값이다. 값을 실은 마지막 스냅샷 것으로 바꾸고,
-- 값이 없는 스냅샷은 이전 값을 그대로 둔다. 표시용이며 위치·위험 판정에 쓰지 않는다.
-- 모양: {"holdout_worsening_tolerance_px": number|null,
--        "pairs": {"<camera_id>-<camera_id>": {"before": {n, invalid, median, p90, p95}|null, "after": {...}|null, "median_px": number|null}}}

ALTER TABLE setup_session ADD COLUMN pair_holdout jsonb;

COMMENT ON COLUMN setup_session.pair_holdout IS
    '쌍별 홀드아웃 재투영 오차(site_setup metrics.joint_ba 의 마지막 값, 표시용). 받은 적 없으면 NULL';
