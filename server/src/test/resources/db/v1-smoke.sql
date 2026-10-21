-- V1__init.sql 점검 (S15P31A101-211). V1 을 적용한 빈 DB 에서 실행한다. 데이터는 마지막에 되돌린다.
--   psql "$SIMAI_DB_URL" -v ON_ERROR_STOP=1 -f server/src/test/resources/db/v1-smoke.sql
-- 실패해야 하는 문장이 성공하거나, 성공해야 하는 문장이 실패하면 오류로 멈춘다.
\pset tuples_only on
BEGIN;

CREATE FUNCTION pg_temp.must_fail(label text, stmt text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE stmt;
    EXCEPTION WHEN others THEN
        RAISE NOTICE 'OK   거부됨: % (%)', label, SQLERRM;
        RETURN;
    END;
    RAISE EXCEPTION 'FAIL 통과하면 안 됨: %', label;
END $$;

-- 기준 데이터: site ↔ floor_plan 은 서로 가리키므로 site 를 먼저 넣는다
INSERT INTO site (site_id, name, created_at) VALUES ('site-1', '교실 테스트베드', now());
INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES ('admin', 'x', 'ADMIN', now());
SELECT pg_temp.must_fail('역할 값 밖', $q$INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES ('x', 'x', 'OWNER', now())$q$);
SELECT pg_temp.must_fail('로그인 ID 중복', $q$INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES ('admin', 'x', 'VIEWER', now())$q$);

-- 구역은 새 버전을 추가한다(복합 PK)
INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from)
VALUES ('z1', 1, 'site-1', '개구부', 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 0))', 0), 'HIGH', now() - interval '1 day'),
       ('z1', 2, 'site-1', '개구부', 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0,2 0,2 2,0 0))', 0), 'HIGH', now());
SELECT pg_temp.must_fail('같은 구역 버전 두 번', $q$INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from) VALUES ('z1', 2, 'site-1', 'x', 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 0))', 0), 'HIGH', now())$q$);
SELECT pg_temp.must_fail('구역 SRID 가 0 이 아님', $q$INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from) VALUES ('z9', 1, 'site-1', 'x', 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 0))', 4326), 'HIGH', now())$q$);
SELECT pg_temp.must_fail('없는 현장', $q$INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from) VALUES ('z8', 1, 'nope', 'x', 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 0))', 0), 'HIGH', now())$q$);

-- 이벤트: LIVE 와 REPLAY 하나씩
INSERT INTO risk_event (event_id, source, rule_id, rule_version, lifecycle, verification, severity, confidence, needs_check, world_ids, metrics, resume_count, retention_hold)
VALUES ('01900000-0000-7000-8000-000000000001', 'LIVE', 'R1', 1, 'RAISED', 'VERIFIED', 'HIGH', 'HIGH', false, ARRAY['w1'], '{}', 0, false),
       ('01900000-0000-7000-8000-000000000002', 'REPLAY', 'R1', 1, 'RAISED', 'VERIFIED', 'HIGH', 'HIGH', false, ARRAY['w1'], '{}', 0, false);
SELECT pg_temp.must_fail('PLANNED 는 risk_event 에 넣지 않음(sim 스키마)', $q$INSERT INTO risk_event (event_id, source, rule_id, rule_version, lifecycle, verification, severity, confidence, needs_check, world_ids, metrics, resume_count, retention_hold) VALUES ('01900000-0000-7000-8000-000000000009', 'PLANNED', 'R1', 1, 'RAISED', 'V', 'HIGH', 'HIGH', false, ARRAY['w1'], '{}', 0, false)$q$);

-- 알림은 LIVE 이벤트만(11.4-3): 복합 FK + CHECK
INSERT INTO notification (event_id, source, event_sequence, channel, severity, summary, delivery, status)
VALUES ('01900000-0000-7000-8000-000000000001', 'LIVE', 1, 'WEB_PUSH', 'HIGH', '진입', 'REALTIME', 'QUEUED');
SELECT pg_temp.must_fail('REPLAY 이벤트에 LIVE 로 속여 알림', $q$INSERT INTO notification (event_id, source, event_sequence, channel, severity, summary, delivery, status) VALUES ('01900000-0000-7000-8000-000000000002', 'LIVE', 1, 'WEB_PUSH', 'HIGH', 'x', 'REALTIME', 'QUEUED')$q$);
SELECT pg_temp.must_fail('REPLAY 출처 알림', $q$INSERT INTO notification (event_id, source, event_sequence, channel, severity, summary, delivery, status) VALUES ('01900000-0000-7000-8000-000000000002', 'REPLAY', 1, 'WEB_PUSH', 'HIGH', 'x', 'REALTIME', 'QUEUED')$q$);

-- 전이는 (event_id, event_sequence) 와 저널 위치로 중복을 막는다(FR-95)
INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition, occurred_at, observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version, scene_version, source, severity, confidence, verification, needs_check, world_ids, metrics, resent, resumed, delivery_mode, received_at)
VALUES ('01900000-0000-7000-8000-000000000001', 1, gen_random_uuid(), 'gw-1', 100, 'RAISED', now() - interval '5 min', now() - interval '5 min', now() - interval '5 min', 'R1', 1, 'cfg-1', 'cal-1', 'scene-1', 'LIVE', 'HIGH', 'HIGH', 'VERIFIED', false, ARRAY['w1'], '{}', false, false, 'REALTIME', now());
SELECT pg_temp.must_fail('같은 전이 두 번', $q$INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition, occurred_at, observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version, scene_version, source, severity, confidence, verification, needs_check, world_ids, metrics, resent, resumed, delivery_mode, received_at) VALUES ('01900000-0000-7000-8000-000000000001', 1, gen_random_uuid(), 'gw-1', 101, 'RAISED', now(), now(), now(), 'R1', 1, 'c', 'c', 's', 'LIVE', 'HIGH', 'HIGH', 'V', false, ARRAY['w1'], '{}', true, false, 'REALTIME', now())$q$);
SELECT pg_temp.must_fail('같은 저널 위치 두 번', $q$INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition, occurred_at, observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version, scene_version, source, severity, confidence, verification, needs_check, world_ids, metrics, resent, resumed, delivery_mode, received_at) VALUES ('01900000-0000-7000-8000-000000000001', 2, gen_random_uuid(), 'gw-1', 100, 'UPDATED', now(), now(), now(), 'R1', 1, 'c', 'c', 's', 'LIVE', 'HIGH', 'HIGH', 'V', false, ARRAY['w1'], '{}', false, false, 'REALTIME', now())$q$);

-- 클립: (event_id, resume_count, camera_id) 하나
INSERT INTO media_clip (clip_id, event_id, gateway_id, event_sequence, resume_count, rule_id, severity, source, camera_id, stream_epochs, anchor_capture_at, requested_start_at, requested_end_at, clip_start_capture_at, clip_end_capture_at, frame_count, starts_with_keyframe, truncated_start, truncated_end, has_gaps, content_type, container, size_bytes, sha256, face_redaction, clip_created_at, delete_after, retention_hold, object_key, uploaded_at, received_at)
VALUES (gen_random_uuid(), '01900000-0000-7000-8000-000000000001', 'gw-1', 1, 0, 'R1', 'HIGH', 'LIVE', 'cam1', ARRAY[0], now(), now(), now(), now(), now(), 10, true, false, false, false, 'video/mp4', 'mp4', 1, 'h', 'NONE', now(), now() + interval '14 days', false, 'k1', now(), now());
SELECT pg_temp.must_fail('같은 구간 클립 두 번', $q$INSERT INTO media_clip (clip_id, event_id, gateway_id, event_sequence, resume_count, rule_id, severity, source, camera_id, stream_epochs, anchor_capture_at, requested_start_at, requested_end_at, clip_start_capture_at, clip_end_capture_at, frame_count, starts_with_keyframe, truncated_start, truncated_end, has_gaps, content_type, container, size_bytes, sha256, face_redaction, clip_created_at, delete_after, retention_hold, object_key, uploaded_at, received_at) VALUES (gen_random_uuid(), '01900000-0000-7000-8000-000000000001', 'gw-1', 1, 0, 'R1', 'HIGH', 'LIVE', 'cam1', ARRAY[0], now(), now(), now(), now(), now(), 10, true, false, false, false, 'video/mp4', 'mp4', 1, 'h', 'NONE', now(), now(), false, 'k2', now(), now())$q$);

-- 운영자 종료: REST 멱등 키는 유일, close_id 는 EC2 발급
INSERT INTO event_close (close_id, idempotency_key, event_id, actor_user_id, requested_at, reason)
SELECT gen_random_uuid(), 'idem-1', '01900000-0000-7000-8000-000000000001', user_id, now(), '3번 카메라 철거' FROM app_user WHERE login_id = 'admin';
SELECT pg_temp.must_fail('같은 멱등 키 두 번', $q$INSERT INTO event_close (close_id, idempotency_key, event_id, actor_user_id, requested_at, reason) SELECT gen_random_uuid(), 'idem-1', '01900000-0000-7000-8000-000000000001', user_id, now(), 'x' FROM app_user WHERE login_id = 'admin'$q$);

-- World 상태(hypertable): 10일 전·지금 두 행, 오래된 청크 압축 뒤에도 리플레이 질의 Q1 이 된다
INSERT INTO world_track_state (observation_at, world_id, msg_id, gateway_id, t_last_valid, observation_state, track_lifecycle, identity_confidence, identity_revision, position, position_xy_sigma, support_surface_validity, contact_validity, z_validity, cameras, calibration_version, scene_version, received_at)
SELECT ts, 'w1', gen_random_uuid(), 'gw-1', ts, 'OBSERVED', 'ACTIVE', 'HIGH', 0, ST_SetSRID(ST_MakePoint(1, 2), 0), ARRAY[0.1, 0.1]::real[], 'VALID', 'VALID', 'VALID', ARRAY['cam1'], 'cal-1', 'scene-1', ts
FROM (VALUES (now() - interval '10 days'), (now())) v(ts);
SELECT count(*) AS compressed_chunks FROM (SELECT compress_chunk(c) FROM show_chunks('world_track_state', older_than => interval '7 days') c) s;
DO $$ BEGIN
    IF (SELECT count(*) FROM (SELECT DISTINCT ON (w.world_id) w.* FROM world_track_state w
        WHERE w.observation_at > now() - interval '10 days' - interval '1 second' AND w.observation_at <= now() - interval '10 days' + interval '1 second'
        ORDER BY w.world_id, w.observation_at DESC) s) <> 1 THEN RAISE EXCEPTION 'FAIL Q1(압축된 청크)'; END IF;
    RAISE NOTICE 'OK   Q1 이 압축된 청크에서도 된다';
END $$;

-- 리플레이 Q2: T 에 활성인 이벤트
DO $$ BEGIN
    IF (SELECT count(*) FROM (SELECT DISTINCT ON (u.event_id) u.event_id, u.transition FROM risk_event_update u
        WHERE u.observation_at <= now() AND u.transition IN ('RAISED', 'UPDATED', 'RESOLVED')
        ORDER BY u.event_id, u.event_sequence DESC) s WHERE s.transition <> 'RESOLVED') <> 1 THEN RAISE EXCEPTION 'FAIL Q2'; END IF;
    RAISE NOTICE 'OK   Q2 활성 이벤트 1건';
END $$;

-- 정책: 압축 5개, 보관 2개(system_metric·device_heartbeat 30일)
DO $$ BEGIN
    IF (SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression') <> 5 THEN RAISE EXCEPTION 'FAIL 압축 정책 수'; END IF;
    IF (SELECT string_agg(hypertable_name, ',' ORDER BY hypertable_name) FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention') <> 'device_heartbeat,system_metric' THEN RAISE EXCEPTION 'FAIL 보관 정책'; END IF;
    RAISE NOTICE 'OK   압축 정책 5개, 보관 정책 2개';
END $$;

ROLLBACK;
