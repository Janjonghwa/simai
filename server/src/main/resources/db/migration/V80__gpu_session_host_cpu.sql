-- GPU 인스턴스 호스트 CPU 측정(S15P31A101-740, docs/design/S15P31A101-740-gpu-host-cpu-check.md).
-- gpu_session_report 1.1.0 의 선택 host_cpu 를 진행 사건 행에 그대로 둔다(추가만, AGENTS 6). 지금 값은 host_cpu 가 있는 가장 최근 PROGRESS 행.
ALTER TABLE gpu_session_event ADD COLUMN host_cpu jsonb CHECK (host_cpu IS NULL OR jsonb_typeof(host_cpu) = 'object');
COMMENT ON COLUMN gpu_session_event.host_cpu IS
    '호스트 CPU 측정 {model, logical_cpus, cpu_quota, single_thread_score, measured_at}(계약 gpu_session_report 1.1.0). 못 잰 칸은 null, 보고에 없으면 이 열이 null';
