-- GPU 인스턴스 부팅 보고의 꺼진 기능 구조화 목록(S15P31A101-756, docs/design/S15P31A101-756-disabled-features-plain.md).
-- gpu_session_report 1.3.0 의 선택 disabled_notices({key, level, text, detail?} 배열)를 진행 사건 행에 그대로 둔다(추가만, AGENTS 6). 지금 값은 값이 있는 가장 최근 PROGRESS 행.
ALTER TABLE gpu_session_event ADD COLUMN disabled_notices jsonb CHECK (disabled_notices IS NULL OR jsonb_typeof(disabled_notices) = 'array');
COMMENT ON COLUMN gpu_session_event.disabled_notices IS
    '꺼진 기능 쉬운 말 항목 배열 {key, level, text, detail?}(계약 gpu_session_report 1.3.0). 빈 배열은 꺼진 것 없음 확인, 보고에 없으면 이 열이 null';
