#!/usr/bin/env bash
# 히스토리 화면(작업 목록·다시 보기) 확인용 목 작업·위치·이벤트 적재와 정리. 운영 EC2 에서만 돌린다(/opt/simai 의 DB 컨테이너를 쓴다).
# 히스토리가 읽는 표는 work_unit(목록), world_track_state·system_health·risk_event_update(다시 보기)다.
# 세 질의 모두 gateway 로 거르지 않으므로, 실제 기록과 시간이 겹치는 칸은 넣지 않고 목 행에는 전용 gateway_id 를 붙인다.
# 목 행의 표시: gateway_id = tier0-mock-history(world·health·event·update), work_unit.created_by IS NULL(API 로 만든 작업은 로그인 사용자가 찬다).
# 이벤트는 모두 RESOLVED 로 끝나 활성 경보로 남지 않고, DB 직접 INSERT 라 알림·워치로 나가지 않는다.
#
# 사용:
#   deploy/ec2/seed-mock-history.sh seed [--end-date YYYY-MM-DD(KST, 기본 오늘)] [--days N(기본 6)] [--dry-run]
#   deploy/ec2/seed-mock-history.sh status
#   deploy/ec2/seed-mock-history.sh cleanup [--dry-run]
# --dry-run 은 한 트랜잭션으로 다 돌려 보고 되돌린다(표별 행 수만 본다).
# 필요: docker, flock, /opt/simai/.env. 배포(deploy.sh)와 같은 잠금을 잡아 DB 재시작과 겹치지 않게 한다.
set -euo pipefail

# ── 상수 ──────────────────────────────────────────────────────────────────
SIMAI=${SIMAI:-/opt/simai}
ENV_FILE=$SIMAI/.env
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$SIMAI/repo/deploy/ec2/compose.yaml")
LOCK_FILE=$SIMAI/deploy.lock
LOCK_WAIT_S=600
GATEWAY_ID=tier0-mock-history
SITE_TZ=Asia/Seoul
WORLD_HZ=2
HEALTH_HZ=1
EVENT_SOURCE=LIVE
DEFAULT_DAYS=6

die() { echo "$*" >&2; exit 2; }

# 인자를 읽어 COMMAND·END_DATE·DAYS·DRY_RUN 에 둔다.
parse_args() {
    COMMAND=${1:-}; [ $# -gt 0 ] && shift
    END_DATE=$(TZ=$SITE_TZ date +%F)
    DAYS=$DEFAULT_DAYS
    DRY_RUN=0
    SEED_OPTIONS=0
    while [ $# -gt 0 ]; do
        case $1 in
            --end-date) END_DATE=${2:?--end-date 값이 없다}; SEED_OPTIONS=1; shift 2 ;;
            --days) DAYS=${2:?--days 값이 없다}; SEED_OPTIONS=1; shift 2 ;;
            --dry-run) DRY_RUN=1; shift ;;
            *) die "모르는 옵션: $1" ;;
        esac
    done
    case $DAYS in ''|*[!0-9]*) die "--days 는 자연수다: $DAYS" ;; esac
    [ "$DAYS" -ge 1 ] || die "--days 는 1 이상이다"
    if [ "$COMMAND" != seed ] && [ "$SEED_OPTIONS" -gt 0 ]; then die "--end-date·--days 는 seed 에만 쓴다"; fi
    if [ "$COMMAND" = status ] && [ "$DRY_RUN" -gt 0 ]; then die "--dry-run 은 seed·cleanup 에만 쓴다"; fi
}

# 표준 입력의 SQL 을 psql 에 한 번에 보낸다. psql 변수는 -v 로 넘긴다.
run_sql() {
    "${COMPOSE[@]}" exec -T db psql -U postgres -d simai -X -q -v ON_ERROR_STOP=1 \
        -v gateway="$GATEWAY_ID" -v site_tz="$SITE_TZ" -v world_hz="$WORLD_HZ" -v health_hz="$HEALTH_HZ" \
        -v event_source="$EVENT_SOURCE" -v end_date="$END_DATE" -v days="$DAYS" -v dry_run="$DRY_RUN" "$@" -f -
}

# pg_temp 도우미: 세션이 끝나면 사라져 운영 DB 에 객체를 남기지 않는다.
helpers_sql() {
    cat <<'SQL'
-- 36진수 문자열(실제 Tier 0 의 w-{세션}-{번호} 세션 접두어 형식)
CREATE FUNCTION pg_temp.b36(n bigint) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE digits constant text := '0123456789abcdefghijklmnopqrstuvwxyz'; out text := ''; v bigint := n;
BEGIN
    IF v = 0 THEN RETURN '0'; END IF;
    WHILE v > 0 LOOP out := substr(digits, (v % 36)::int + 1, 1) || out; v := v / 36; END LOOP;
    RETURN out;
END $f$;
-- 키마다 같은 msg_id
CREATE FUNCTION pg_temp.uuid_of(key text) RETURNS uuid LANGUAGE sql IMMUTABLE AS $f$ SELECT md5(key)::uuid $f$;
-- 계약의 event_id 는 UUIDv7 이어야 한다(gen_random_uuid() 는 v4 라 화면에서 버려진다). 시각 + 키로 결정적으로 만든다
CREATE FUNCTION pg_temp.uuid7(ts timestamptz, key text) RETURNS uuid LANGUAGE sql IMMUTABLE AS $f$
    SELECT (lpad(to_hex((extract(epoch FROM ts) * 1000)::bigint), 12, '0') || '7' || substr(md5(key), 1, 3)
            || '8' || substr(md5(key), 4, 3) || substr(md5(key), 7, 12))::uuid $f$;
SQL
}

# 이미 목 행이 있으면 멈춘다(재실행 막기).
guard_sql() {
    cat <<'SQL'
SELECT (EXISTS (SELECT 1 FROM system_health WHERE gateway_id = :'gateway')
     OR EXISTS (SELECT 1 FROM risk_event WHERE gateway_id = :'gateway'))::int AS mock_exists \gset
\if :mock_exists
    DO $$ BEGIN RAISE EXCEPTION '목 행이 이미 있다. cleanup 을 먼저 돌린다'; END $$;
\endif
SQL
}

# 실제 값(site·구역·카메라·최신 판)을 DB 에서 읽어 임시 표에 둔다. 하드코딩하지 않는다.
context_sql() {
    cat <<'SQL'
CREATE TEMP TABLE ctx ON COMMIT DROP AS
SELECT (SELECT site_id FROM site ORDER BY site_id LIMIT 1) AS site_id,
       r.calibration_version AS cal, r.scene_version AS scene, r.config_version AS cfg
FROM (SELECT calibration_version, scene_version, config_version FROM world_track_state
      WHERE gateway_id <> :'gateway' ORDER BY observation_at DESC LIMIT 1) r;

CREATE TEMP TABLE cams ON COMMIT DROP AS
SELECT array_agg(camera_id ORDER BY camera_id) AS ids
FROM (SELECT camera_id FROM camera WHERE removed_at IS NULL ORDER BY camera_id LIMIT 3) c;

-- 본뜰 실제 world·health 행이 없으면 값을 읽을 수 없다
SELECT (NOT EXISTS (SELECT 1 FROM ctx) OR NOT EXISTS (SELECT 1 FROM system_health WHERE gateway_id <> :'gateway'))::int AS no_base \gset
\if :no_base
    DO $$ BEGIN RAISE EXCEPTION '본뜰 실제 world_track_state·system_health 행이 없다'; END $$;
\endif

-- 활성 구역과 중심점
CREATE TEMP TABLE zones ON COMMIT DROP AS
SELECT row_number() OVER (ORDER BY zone_id) AS n, zone_id, severity, ST_X(c) AS cx, ST_Y(c) AS cy
FROM (SELECT zone_id, severity, ST_Centroid(polygon) AS c FROM zone
      WHERE valid_to IS NULL AND site_id = (SELECT site_id FROM ctx)) z;
SQL
}

# 날짜×시간대 칸을 만들고, 미래이거나 실제 기록과 겹치는 칸은 뺀다.
slots_sql() {
    cat <<'SQL'
CREATE TEMP TABLE slots ON COMMIT DROP AS
SELECT d::date AS day, s.slot,
       ((d::date + s.start_t) AT TIME ZONE :'site_tz') AS starts_at,
       ((d::date + s.end_t) AT TIME ZONE :'site_tz') AS ends_at,
       NULL::text AS skip
FROM generate_series(:'end_date'::date - (:days - 1), :'end_date'::date, interval '1 day') AS d
CROSS JOIN (VALUES (1, time '08:00', time '09:30'), (2, time '17:00', time '18:00')) AS s(slot, start_t, end_t)
WHERE extract(isodow FROM d) <> 7;

UPDATE slots SET skip = '아직 끝나지 않음' WHERE ends_at > now();
UPDATE slots s SET skip = '실제 기록과 겹침'
WHERE skip IS NULL AND (
    EXISTS (SELECT 1 FROM world_track_state w WHERE w.gateway_id <> :'gateway'
            AND w.observation_at BETWEEN s.starts_at - interval '5 seconds' AND s.ends_at + interval '5 seconds')
    OR EXISTS (SELECT 1 FROM system_health h WHERE h.gateway_id <> :'gateway'
               AND h.reported_at BETWEEN s.starts_at - interval '5 seconds' AND s.ends_at + interval '5 seconds')
    OR EXISTS (SELECT 1 FROM risk_event_update u WHERE u.gateway_id <> :'gateway'
               AND u.observation_at BETWEEN s.starts_at - interval '5 seconds' AND s.ends_at + interval '5 seconds'));

SELECT day AS "뺀 칸", slot AS "시간대", skip AS "이유" FROM slots WHERE skip IS NOT NULL ORDER BY day, slot;
SQL
}

# 칸에 작업 템플릿을 돌려 붙인다(날짜마다 돌려 쓴다). 구역 없는 작업은 구역 중심 평균 근처를 중심으로 한다.
works_sql() {
    cat <<'SQL'
CREATE TEMP TABLE works ON COMMIT DROP AS
SELECT k.n, k.day, k.slot, k.starts_at, k.ends_at, t.name, t.place, t.planned, t.team, t.work_type,
       z.zone_id, z.severity AS zone_severity,
       COALESCE(z.cx, (SELECT avg(cx) FROM zones) - 1.5, 0) AS cx,
       COALESCE(z.cy, (SELECT avg(cy) FROM zones) + 1.0, 0) AS cy,
       -- 데이터 없음 45 초: 오전 작업 앞의 둘에만 +30분에 넣는다
       CASE WHEN k.slot = 1 AND row_number() OVER (PARTITION BY k.slot ORDER BY k.starts_at) <= 2 THEN 1800 END AS gap_from_s
FROM (SELECT row_number() OVER (ORDER BY starts_at) AS n, * FROM slots WHERE skip IS NULL) k
JOIN (VALUES
        (0, '개구부 옆 비계 설치', 1, '로비 개구부 앞', 4, 'B조', 'OPENING_WORK'),
        (1, '자재 양중',           2, '로비 북측 반입구', 3, 'A조', 'GENERAL'),
        (2, '전기 배선 정리',      NULL::bigint, '로비 서측 벽면', 2, 'B조', 'GENERAL')
     ) AS t(idx, name, zone_n, place, planned, team, work_type) ON t.idx = (k.n - 1) % 3
LEFT JOIN zones z ON z.n = t.zone_n;

INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, zone_id, place, planned_people, lead_team,
                       created_by, created_at, updated_at, work_type, status, actual_started_at, actual_ended_at)
SELECT (SELECT site_id FROM ctx), w.day, w.name, w.starts_at, w.ends_at, w.zone_id, w.place, w.planned, w.team,
       NULL, w.starts_at - interval '1 day', w.starts_at - interval '1 day', w.work_type, 'DONE', w.starts_at, w.ends_at
FROM works w;
SQL
}

# 작업자: 계획 인원 그대로 또는 1명 적게, 0~3분 사이에 엇갈려 들어온다. 둘째는 +20분에 5분 자리를 비운다.
people_sql() {
    cat <<'SQL'
CREATE TEMP TABLE people ON COMMIT DROP AS
SELECT w.n AS wn, p AS i,
       'w-' || pg_temp.b36((extract(epoch FROM w.starts_at) * 1000)::bigint) || '-' || lpad(p::text, 4, '0') AS world_id,
       (p - 1) * 40 AS enter_s,
       CASE WHEN p = 2 THEN 1200 END AS away_from_s
FROM works w CROSS JOIN LATERAL generate_series(1, GREATEST(1, w.planned - (w.n % 2))) p;
SQL
}

# 이벤트(전부 RESOLVED 로 끝남): 작업 시작 기준 상대 시각. 구역 없는 작업에는 R1·R5 가 없다.
events_sql() {
    cat <<'SQL'
CREATE TEMP TABLE event_defs (kind text, rule_id text, off_s int, dur_s int, worker int, sev text,
                              needs_zone boolean, min_work_s int, once boolean, metrics jsonb) ON COMMIT DROP;
INSERT INTO event_defs VALUES
    ('R5',  'R5',  600,  6, 1, 'CAUTION',  true,  0,    false, '{"eta_s": 4.0, "zone_distance_m": 1.4}'),
    ('R1',  'R1',  606, 45, 1, 'ZONE',     true,  0,    false, '{"zone_distance_m": 0.3, "calibration_margin_m": 0.3}'),
    ('R1b', 'R1', 2400, 30, 2, 'ZONE',     true,  5400, false, '{"zone_distance_m": 0.5, "calibration_margin_m": 0.3}'),
    ('R7',  'R7', 1500, 20, NULL, 'CAUTION', false, 5400, false, '{"coverage_ratio": 0.55}'),
    ('R4',  'R4', 3000, 20, 3, 'CRITICAL', false, 5400, true,  '{"head_z_m": 0.35, "fall_duration_s": 1.2}');

CREATE TEMP TABLE evs ON COMMIT DROP AS
SELECT row_number() OVER (ORDER BY w.starts_at, d.off_s) AS en, w.n AS wn, d.kind, d.rule_id, d.dur_s, d.metrics, w.zone_id,
       w.starts_at + make_interval(secs => d.off_s) AS raised_at,
       CASE d.sev WHEN 'ZONE' THEN w.zone_severity ELSE d.sev END AS severity,
       CASE WHEN d.worker IS NULL THEN NULL
            ELSE (SELECT p.world_id FROM people p WHERE p.wn = w.n AND p.i = LEAST(d.worker, (SELECT max(i) FROM people WHERE wn = w.n))) END AS world_id,
       CASE WHEN d.rule_id IN ('R1', 'R5', 'R7') THEN w.zone_id END AS ev_zone
FROM works w
JOIN event_defs d ON (NOT d.needs_zone OR w.zone_id IS NOT NULL)
                 AND w.ends_at - w.starts_at >= make_interval(secs => d.min_work_s)
WHERE NOT d.once OR w.n = (SELECT min(n) FROM works WHERE ends_at - starts_at >= make_interval(secs => d.min_work_s));

ALTER TABLE evs ADD COLUMN event_id uuid, ADD COLUMN last_seq int;
UPDATE evs SET event_id = pg_temp.uuid7(raised_at, 'mock-ev-' || en), last_seq = 2 + floor((dur_s - 1) / 5.0)::int;

-- 전이: 발생 → 5 초마다 갱신 → 해소
CREATE TEMP TABLE ev_updates ON COMMIT DROP AS
SELECT e.en, 1 AS seq, 'RAISED' AS transition, e.raised_at AS at FROM evs e
UNION ALL
SELECT e.en, 1 + k, 'UPDATED', e.raised_at + make_interval(secs => 5 * k)
FROM evs e CROSS JOIN LATERAL generate_series(1, floor((e.dur_s - 1) / 5.0)::int) k
UNION ALL
SELECT e.en, e.last_seq, 'RESOLVED', e.raised_at + make_interval(secs => e.dur_s) FROM evs e;

INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity, confidence,
                        needs_check, confidence_reasons, world_ids, zone_id, metrics, rationale, proposed_severity, resume_count,
                        event_sequence, config_version, calibration_version, scene_version, raised_at, updated_at, resolved_at,
                        delivery, retention_hold)
SELECT e.event_id, :'event_source', :'gateway', e.rule_id, 1, 'RESOLVED', 'VERIFIED', e.severity, 'HIGH',
       false, NULL, CASE WHEN e.world_id IS NULL THEN ARRAY[]::text[] ELSE ARRAY[e.world_id] END,
       e.ev_zone, e.metrics, NULL, NULL, 0,
       e.last_seq, c.cfg, c.cal, c.scene, e.raised_at, e.raised_at + make_interval(secs => e.dur_s),
       e.raised_at + make_interval(secs => e.dur_s), 'REALTIME', false
FROM evs e CROSS JOIN ctx c;

INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition, occurred_at, observation_at,
                               evaluated_at, rule_id, rule_version, config_version, calibration_version, scene_version, source,
                               severity, confidence, verification, needs_check, confidence_reasons, world_ids, zone_id, metrics,
                               evidence_ref, resent, resumed, related_event_id, ack_id, close_id, escalation_id, delivery_mode,
                               received_at, journal_epoch)
SELECT e.event_id, u.seq, pg_temp.uuid_of('mock-evu-' || e.en || '-' || u.seq), :'gateway',
       row_number() OVER (ORDER BY u.at, e.en, u.seq), u.transition, u.at, u.at, u.at, e.rule_id, 1, c.cfg, c.cal, c.scene, 'LIVE',
       e.severity, 'HIGH', 'VERIFIED', false, NULL,
       CASE WHEN e.world_id IS NULL THEN ARRAY[]::text[] ELSE ARRAY[e.world_id] END,
       e.ev_zone, e.metrics,
       NULL, false, false, NULL, NULL, NULL, NULL, 'REALTIME', u.at, 0
FROM ev_updates u JOIN evs e ON e.en = u.en CROSS JOIN ctx c;
SQL
}

# 위치(2 Hz): 작업 장소 둘레를 타원으로 걷는다(약 0.6 m/s). 구역 진입 이벤트 동안 중심으로 들어가고, 낙상 동안 head_z 가 0.35 로 떨어진다.
tracks_sql() {
    cat <<'SQL'
INSERT INTO world_track_state (observation_at, world_id, msg_id, gateway_id, t_last_valid, observation_state, track_lifecycle,
                               identity_confidence, identity_revision, position, position_xy_sigma, pelvis_xyz,
                               support_surface_validity, contact_validity, z_validity, head_z, head_z_sigma, head_z_validity,
                               velocity, cameras, calibration_version, scene_version, config_version, input_provenance, received_at)
SELECT k.ts, k.world_id, pg_temp.uuid_of('mock-wt-' || (extract(epoch FROM k.ts) * 1000)::bigint), :'gateway', k.ts,
       'OBSERVED', 'CONFIRMED', 'HIGH', 0, ST_MakePoint(k.x, k.y), ARRAY[0.06, 0.06], ARRAY[k.x, k.y, 0.95],
       'UNKNOWN', 'UNAVAILABLE', 'UNAVAILABLE', k.head_z, 0.03, 'VALID',
       ARRAY[k.vx, k.vy, 0], (SELECT ids FROM cams), c.cal, c.scene, c.cfg,
       '{"frames": {"source": "SIM", "dataset_id": "history-mock/v1"}, "tracks": {"source": "SIM", "provider_version": "history-mock-seed-1"}, "is_upper_bound": true}'::jsonb,
       k.ts
FROM (
    SELECT t.ts, p.world_id,
           w.cx + rx * (1 - ap) * cos(ang) AS x, w.cy + ry * (1 - ap) * sin(ang) AS y,
           -rx * (1 - ap) * sin(ang) * om AS vx, ry * (1 - ap) * cos(ang) * om AS vy,
           1.69 - 0.03 * p.i - fall * (1.69 - 0.03 * p.i - 0.35) AS head_z
    FROM works w
    JOIN people p ON p.wn = w.n
    CROSS JOIN LATERAL generate_series(w.starts_at, w.ends_at, make_interval(secs => 1.0 / :world_hz)) AS t(ts)
    CROSS JOIN LATERAL (SELECT extract(epoch FROM t.ts - w.starts_at) AS el) q
    CROSS JOIN LATERAL (SELECT 1.2 + 0.25 * (p.i - 1) AS rx, 0.8 + 0.2 * (p.i - 1) AS ry) r
    CROSS JOIN LATERAL (SELECT 0.6 / ((r.rx + r.ry) / 2) AS om, (p.i - 1) * 1.7 + 0.6 / ((r.rx + r.ry) / 2) * q.el AS ang) a
    CROSS JOIN LATERAL (
        -- 이 작업자의 구역 진입(R1) 전후 8 초에 걸쳐 중심으로 모였다 돌아간다
        SELECT COALESCE(max(GREATEST(0, LEAST(1, LEAST(
                   (extract(epoch FROM t.ts - e.raised_at) + 8) / 8,
                   (extract(epoch FROM e.raised_at + make_interval(secs => e.dur_s) - t.ts) + 8) / 8)))), 0) AS ap
        FROM evs e WHERE e.wn = w.n AND e.world_id = p.world_id AND e.rule_id = 'R1') ap
    CROSS JOIN LATERAL (
        -- 낙상(R4): 3 초에 걸쳐 쓰러졌다 일어난다
        SELECT COALESCE(max(GREATEST(0, LEAST(1, LEAST(
                   (extract(epoch FROM t.ts - e.raised_at)) / 3,
                   (extract(epoch FROM e.raised_at + make_interval(secs => e.dur_s) - t.ts) + 3) / 3)))), 0) AS fall
        FROM evs e WHERE e.wn = w.n AND e.world_id = p.world_id AND e.rule_id = 'R4') f
    WHERE q.el >= p.enter_s
      AND NOT (p.away_from_s IS NOT NULL AND q.el >= p.away_from_s AND q.el < p.away_from_s + 300)
      AND NOT (w.gap_from_s IS NOT NULL AND q.el >= w.gap_from_s AND q.el < w.gap_from_s + 45)
) k CROSS JOIN ctx c;
SQL
}

# 시스템 상태(1 Hz): 가장 새 실제 행을 본떠 만들어(계약 모양을 그대로 쓴다) 작업 앞뒤 10 초까지 넣는다. 데이터 없음 구간은 뺀다.
health_sql() {
    cat <<'SQL'
INSERT INTO system_health (reported_at, gateway_id, msg_id, system_health, t_latest_global, pipeline_lag_ms,
                           pipeline_lag_lower_bound_ms, capture_to_risk_ms, calibration_state, calibration_version, config_version,
                           config_sync, gateway_link, cameras, tier0_warnings, ec2_world_state_drops, gpu_util,
                           gpu_memory_used_ratio, active_site_alerts, active_site_alerts_as_of, laptop_clock_offset, received_at)
SELECT h.ts, :'gateway', pg_temp.uuid_of('mock-h-' || (extract(epoch FROM h.ts) * 1000)::bigint), 'OK', h.ts, 250,
       NULL, NULL, 'PROVISIONAL', c.cal, c.cfg,
       base.config_sync, base.gateway_link, base.cameras, NULL, NULL, NULL,
       NULL, NULL, NULL, NULL, h.ts
FROM works w
CROSS JOIN LATERAL generate_series(w.starts_at - interval '10 seconds', w.ends_at + interval '10 seconds',
                                   make_interval(secs => 1.0 / :health_hz)) AS h(ts)
CROSS JOIN ctx c
CROSS JOIN (SELECT config_sync, gateway_link, cameras FROM system_health
            WHERE gateway_id <> :'gateway' ORDER BY reported_at DESC LIMIT 1) base
WHERE NOT (w.gap_from_s IS NOT NULL
           AND extract(epoch FROM h.ts - w.starts_at) >= w.gap_from_s
           AND extract(epoch FROM h.ts - w.starts_at) < w.gap_from_s + 45);
SQL
}

# 목 작업: created_by 가 없고 시작 10 초 전부터의 목 health 가 있는 작업(seed 가 health 를 작업 앞 10 초부터 넣는다).
mock_work_sql() {
    cat <<'SQL'
CREATE OR REPLACE TEMP VIEW mock_work AS
SELECT w.* FROM work_unit w
WHERE w.created_by IS NULL AND EXISTS (SELECT 1 FROM system_health h WHERE h.gateway_id = :'gateway'
      AND h.reported_at BETWEEN w.starts_at - interval '10 seconds' AND w.starts_at);
SQL
}

# 표별 목 행 수와 작업 목록(히스토리 목록 질의와 같은 방식으로 위험·경고 수를 센다).
report_sql() {
    mock_work_sql
    cat <<'SQL'
SELECT 'work_unit' AS "표", count(*) AS "행" FROM mock_work
UNION ALL SELECT 'world_track_state', count(*) FROM world_track_state WHERE gateway_id = :'gateway'
UNION ALL SELECT 'system_health', count(*) FROM system_health WHERE gateway_id = :'gateway'
UNION ALL SELECT 'risk_event', count(*) FROM risk_event WHERE gateway_id = :'gateway'
UNION ALL SELECT 'risk_event_update', count(*) FROM risk_event_update WHERE gateway_id = :'gateway';

SELECT to_char(w.starts_at AT TIME ZONE :'site_tz', 'MM-DD HH24:MI') AS "시작(KST)", w.name AS "작업", w.planned_people AS "계획",
       count(*) FILTER (WHERE e.severity = 'CRITICAL') AS "위험", count(*) FILTER (WHERE e.severity = 'HIGH') AS "경고"
FROM mock_work w
LEFT JOIN risk_event e ON e.source = 'LIVE' AND e.raised_at BETWEEN w.starts_at AND w.ends_at
GROUP BY w.work_id, w.starts_at, w.name, w.planned_people ORDER BY w.starts_at;
SQL
}

# 끝: 시험이면 되돌리고, 아니면 확정한다.
finish_sql() {
    cat <<'SQL'
\if :dry_run
    ROLLBACK;
    \echo '(--dry-run: 되돌렸다)'
\else
    COMMIT;
\endif
SQL
}

seed_sql() {
    echo 'BEGIN;'
    helpers_sql; guard_sql; context_sql; slots_sql; works_sql; people_sql; events_sql; tracks_sql; health_sql; report_sql
    finish_sql
}

status_sql() { report_sql; }

# 목 이벤트를 가리키는 다른 표 행이 있으면(클립·확인 등) 멈추고, 없으면 자식부터 지운다.
cleanup_sql() {
    mock_work_sql
    cat <<'SQL'
BEGIN;
SELECT (SELECT count(*) FROM event_ack WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM event_close WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM event_escalation WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM vlm_assessment WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM analysis_job WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM incident_summary WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM media_clip WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM notification WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM watch_alert WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = :'gateway'))
     + (SELECT count(*) FROM report WHERE work_id IN (SELECT work_id FROM mock_work))
     + (SELECT count(*) FROM setup_session WHERE work_unit_id IN (SELECT work_id FROM mock_work))
     AS blocked \gset
\if :blocked
    DO $$ BEGIN RAISE EXCEPTION '목 이벤트·목 작업을 가리키는 다른 표 행이 있다(클립·확인·보고서·세팅 등). 그쪽을 먼저 정리한다(예: seed-mock-clips.sh cleanup)'; END $$;
\endif
DELETE FROM work_unit WHERE work_id IN (SELECT work_id FROM mock_work);
DELETE FROM risk_event_update WHERE gateway_id = :'gateway';
DELETE FROM risk_event WHERE gateway_id = :'gateway';
DELETE FROM world_track_state WHERE gateway_id = :'gateway';
DELETE FROM system_health WHERE gateway_id = :'gateway';
SQL
    report_sql
    finish_sql
}

main() {
    parse_args "$@"
    [ -f "$ENV_FILE" ] || die "env 파일이 없다: $ENV_FILE"
    case $COMMAND in
        seed|cleanup|status) ;;
        *) sed -n 2,14p "$0" >&2; exit 2 ;;
    esac
    # 쓰는 명령은 배포(deploy.sh)와 같은 잠금을 잡는다: DB 컨테이너가 재시작되는 동안 돌지 않는다
    if [ "$COMMAND" != status ]; then
        exec 9>"$LOCK_FILE"
        flock -w "$LOCK_WAIT_S" 9 || die "배포가 ${LOCK_WAIT_S}초 넘게 끝나지 않는다"
    fi
    case $COMMAND in
        seed) seed_sql | run_sql ;;
        cleanup) cleanup_sql | run_sql ;;
        status) status_sql | run_sql ;;
    esac
}

main "$@"
