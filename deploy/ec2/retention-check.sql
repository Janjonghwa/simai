-- 데이터 삭제 확인(명세 18.4 주 1회, S15P31A101-72). 읽기만 한다. 결과 해석은 docs/design/server-data-retention.md.
-- 실행: docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml exec -T db psql -U postgres -d simai < deploy/ec2/retention-check.sql
-- 1·2·4 는 0 이어야 한다. 3 은 정책 7줄이 모두 Success 여야 한다. 5 는 보존 사유를 검토한다.

-- 1. 클립: 삭제 예정일이 지났는데 남은 클립(0 이어야 한다)
SELECT count(*) AS overdue_clips FROM media_clip
WHERE deleted_at IS NULL AND NOT retention_hold AND delete_after <= now() - interval '1 hour';
-- 2. 클립 기록이 오지 않은 업로드(0 이어야 한다)
SELECT count(*) AS orphan_grants FROM upload_grant
WHERE purpose = 'EVENT_CLIP' AND used_at IS NULL AND expires_at <= now() - interval '14 days 1 hour';
-- 3. 보관·압축 정책이 등록돼 있고 마지막 실행이 성공했는지
SELECT j.hypertable_name, j.proc_name, j.config ->> 'drop_after' AS drop_after, j.config ->> 'compress_after' AS compress_after,
       s.last_run_status, s.last_successful_finish
FROM timescaledb_information.jobs j LEFT JOIN timescaledb_information.job_stats s USING (job_id)
WHERE j.proc_name IN ('policy_retention', 'policy_compression') ORDER BY 1, 2;
-- 4. 보관 정책 표에서 30일+청크 간격(7일)보다 오래된 청크(0 이어야 한다)
SELECT h AS hypertable, count(c) AS chunks_past_retention
FROM unnest(ARRAY['system_metric', 'device_heartbeat']) AS h
LEFT JOIN LATERAL show_chunks(h::regclass, older_than => interval '37 days') AS c ON true GROUP BY h;
-- 5. 보존 표시된 클립과 사유(18.4 검토)
SELECT c.clip_id, c.event_id, c.severity, c.delete_after, r.reason, r.actor_user_id, r.issued_at
FROM media_clip c
LEFT JOIN LATERAL (SELECT reason, actor_user_id, issued_at FROM media_clip_retention r
                   WHERE r.clip_id = c.clip_id ORDER BY retention_id DESC LIMIT 1) r ON true
WHERE c.retention_hold AND c.deleted_at IS NULL ORDER BY c.delete_after;
