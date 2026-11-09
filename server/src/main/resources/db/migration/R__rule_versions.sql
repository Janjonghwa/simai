-- 자동 생성 파일이다. 손으로 고치지 않는다.
-- 만든 곳: ai/src/simai_ai/risk/rule_catalog.py (규칙 등록부 default_registry). 등록부를 바꾸면 ai/ 에서
--   python -m simai_ai.risk.rule_catalog --write ../server/src/main/resources/db/migration/R__rule_versions.sql
-- Flyway 반복 마이그레이션이라 내용이 바뀔 때마다 다시 실행된다. 새 (rule_id, rule_version) 은 더한다(기획서 11.4-1).
-- 표시: 첫 운영 배포 전(FIRST_OPERATION_DEPLOY = None): 같은 (rule_id, rule_version) 인데 값이 다르면 등록부 값으로 갱신하고 NOTICE 를 남긴다.
-- ① 등록부 값
CREATE TEMP TABLE rule_version_seed (
    rule_id text NOT NULL,
    rule_version integer NOT NULL,
    profile text NOT NULL,
    thresholds jsonb NOT NULL,
    default_severity text,
    legal_basis text,
    reads text[] NOT NULL
) ON COMMIT DROP;

INSERT INTO rule_version_seed (rule_id, rule_version, profile, thresholds, default_severity, legal_basis, reads) VALUES
    ('R1', 1, 'OPERATION', '{"calibration_margin": 0.3, "calibration_widened_states": ["DRIFTED", "PROVISIONAL", "UNCALIBRATED"], "k": 2.0, "margin": 0.3, "r6": {"severity": null, "sigma_k": 2.0}, "radius": 0.3, "tol": 1e-09}'::jsonb, NULL, NULL, ARRAY['tracks.observation_state', 'tracks.position_xy', 'tracks.position_xy_sigma', 'tracks.ground_anchor_xyz', 'tracks.ground_anchor_sigma', 'tracks.head_z', 'tracks.head_z_sigma', 'tracks.skeleton', 'tracks.cameras', 'tracks.identity_confidence', 'zones']::text[]),
    ('R2', 1, 'OPERATION', '{"clear": 1.3, "k": 2.0, "profile": {"floor_reference": false, "height_threshold_m": 2.0, "name": "operational", "test": false}, "radius": 0.3, "raise": 1.0, "tol": 1e-09}'::jsonb, 'CRITICAL', NULL, ARRAY['tracks.observation_state', 'tracks.position_xy', 'tracks.position_xy_sigma', 'tracks.ground_anchor_xyz', 'tracks.ground_anchor_sigma', 'tracks.support_surface_id', 'tracks.z_validity', 'tracks.cameras', 'tracks.identity_confidence', 'work_surfaces']::text[]),
    ('R2', 1001, 'CLASSROOM_TEST', '{"clear": 1.3, "k": 2.0, "profile": {"floor_reference": true, "height_threshold_m": 0.35, "name": "classroom-test-podium-40cm", "test": true}, "radius": 0.3, "raise": 1.0, "tol": 1e-09}'::jsonb, 'CRITICAL', NULL, ARRAY['tracks.observation_state', 'tracks.position_xy', 'tracks.position_xy_sigma', 'tracks.ground_anchor_xyz', 'tracks.ground_anchor_sigma', 'tracks.support_surface_id', 'tracks.z_validity', 'tracks.cameras', 'tracks.identity_confidence', 'work_surfaces']::text[]),
    ('R3', 1, 'OPERATION', '{"k": 2.0, "profile": {"floor_reference": false, "height_threshold_m": 2.0, "name": "operational", "test": false}, "radius": 0.3, "threshold": 2.0, "tol": 1e-09}'::jsonb, 'HIGH', '산업안전보건기준규칙 제32조; 산업안전보건기준규칙 제44조', ARRAY['tracks.observation_state', 'tracks.position_xy', 'tracks.position_xy_sigma', 'tracks.ground_anchor_xyz', 'tracks.ground_anchor_sigma', 'tracks.support_surface_id', 'tracks.z_validity', 'tracks.cameras', 'tracks.identity_confidence', 'work_surfaces']::text[]),
    ('R3', 1001, 'CLASSROOM_TEST', '{"k": 2.0, "profile": {"floor_reference": true, "height_threshold_m": 0.35, "name": "classroom-test-podium-40cm", "test": true}, "radius": 0.3, "threshold": 0.35, "tol": 1e-09}'::jsonb, 'HIGH', NULL, ARRAY['tracks.observation_state', 'tracks.position_xy', 'tracks.position_xy_sigma', 'tracks.ground_anchor_xyz', 'tracks.ground_anchor_sigma', 'tracks.support_surface_id', 'tracks.z_validity', 'tracks.cameras', 'tracks.identity_confidence', 'work_surfaces']::text[]),
    ('R4', 1, 'OPERATION', '{"drop_min": 0.5, "fb_clear": 0.6, "fb_low": 0.8, "followup": 3000, "hold": 1500, "lost_memory": 10000, "lost_window": 1500, "low_clear": 1.0, "low_raise": 0.8, "recovery": 3000, "support_memory": 10000, "window": 2000}'::jsonb, 'CRITICAL', NULL, ARRAY['tracks.observation_state', 'tracks.t_last_valid', 'tracks.identity_revision', 'tracks.identity_confidence', 'tracks.head_z', 'tracks.head_z_validity', 'tracks.ground_anchor_xyz', 'tracks.cameras']::text[]),
    ('R5', 1, 'OPERATION', '{"edge_reach": 1.0, "horizon_s": 2.0, "k": 2.0, "limit_s": 2.5, "margin": 0.3, "max_speed": 4.0, "max_staleness": 500, "min_closing": 0.4, "min_speed": 0.2, "profile": {"floor_reference": false, "height_threshold_m": 2.0, "name": "operational", "test": false}, "radius": 0.3, "tol": 1e-09}'::jsonb, 'CAUTION', NULL, ARRAY['tracks.observation_state', 'tracks.track_lifecycle', 'tracks.t_last_valid', 'tracks.position_xy', 'tracks.position_xy_sigma', 'tracks.velocity', 'tracks.ground_anchor_xyz', 'tracks.ground_anchor_sigma', 'tracks.head_z', 'tracks.head_z_sigma', 'tracks.skeleton', 'tracks.cameras', 'tracks.identity_confidence', 'tracks.support_surface_id', 'tracks.z_validity', 'zones', 'work_surfaces']::text[]),
    ('R5', 1001, 'CLASSROOM_TEST', '{"edge_reach": 1.0, "horizon_s": 2.0, "k": 2.0, "limit_s": 2.5, "margin": 0.3, "max_speed": 4.0, "max_staleness": 500, "min_closing": 0.4, "min_speed": 0.2, "profile": {"floor_reference": true, "height_threshold_m": 0.35, "name": "classroom-test-podium-40cm", "test": true}, "radius": 0.3, "tol": 1e-09}'::jsonb, 'CAUTION', NULL, ARRAY['tracks.observation_state', 'tracks.track_lifecycle', 'tracks.t_last_valid', 'tracks.position_xy', 'tracks.position_xy_sigma', 'tracks.velocity', 'tracks.ground_anchor_xyz', 'tracks.ground_anchor_sigma', 'tracks.head_z', 'tracks.head_z_sigma', 'tracks.skeleton', 'tracks.cameras', 'tracks.identity_confidence', 'tracks.support_surface_id', 'tracks.z_validity', 'zones', 'work_surfaces']::text[]),
    ('R7', 1, 'OPERATION', '{"camera_stall_ms": 500, "min_cameras": 2}'::jsonb, NULL, NULL, ARRAY['zones', 't_latest_global', 'monitoring.cameras', 'monitoring.zone_cameras']::text[]);

-- ② 첫 운영 배포 전 규칙 보완 예외(11.4-1): 같은 버전의 값이 다르면 등록부 값으로 갱신한다(legal_basis·reads·valid_from 은 그대로).
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT s.* FROM rule_version_seed s JOIN rule_version r USING (rule_id, rule_version)
             WHERE r.profile IS DISTINCT FROM s.profile OR r.thresholds IS DISTINCT FROM s.thresholds
                OR r.default_severity IS DISTINCT FROM s.default_severity
             ORDER BY s.rule_id, s.rule_version
    LOOP
        UPDATE rule_version SET profile = c.profile, thresholds = c.thresholds, default_severity = c.default_severity
        WHERE rule_id = c.rule_id AND rule_version = c.rule_version;
        RAISE NOTICE '첫 운영 배포 전 규칙 보완 예외(11.4-1): % v% 값을 등록부 값으로 갱신했다', c.rule_id, c.rule_version;
    END LOOP;
END $$;

-- ②' 근거 조항(legal_basis): 비어 있으면 채우고, 다르면 첫 운영 배포 전 규칙 보완 예외(11.4-1)로 등록부 값으로 갱신한다.
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT s.rule_id, s.rule_version, s.legal_basis, r.legal_basis AS db_legal_basis
             FROM rule_version_seed s JOIN rule_version r USING (rule_id, rule_version)
             WHERE r.legal_basis IS DISTINCT FROM s.legal_basis
             ORDER BY s.rule_id, s.rule_version
    LOOP
        UPDATE rule_version SET legal_basis = c.legal_basis WHERE rule_id = c.rule_id AND rule_version = c.rule_version;
        IF c.db_legal_basis IS NULL THEN
            RAISE NOTICE '근거 조항(legal_basis)이 비어 있어 등록부 값으로 채웠다: % v%', c.rule_id, c.rule_version;
        ELSE
            RAISE NOTICE '첫 운영 배포 전 규칙 보완 예외(11.4-1): % v% 근거 조항(legal_basis)을 등록부 값으로 갱신했다', c.rule_id, c.rule_version;
        END IF;
    END LOOP;
END $$;

-- ③ 새 (rule_id, rule_version) 만 더한다.
INSERT INTO rule_version (rule_id, rule_version, profile, thresholds, default_severity, legal_basis, reads, valid_from)
SELECT rule_id, rule_version, profile, thresholds, default_severity, legal_basis, reads, now() FROM rule_version_seed
ON CONFLICT (rule_id, rule_version) DO NOTHING;
