-- simAI V45: ppe_current_state 촬영 시각 색인 (S15P31A101-550, 명세 12.4·13.3, docs/design/ppe-viewer.md)
-- 관제 화면 PPE 스냅샷(PpeViewRepository, S15P31A101-531)은 촬영 시각 60초 범위로만 읽는다(처음 상태 REST 의 Tier 0 목록, Tier 0 별 스냅샷).
-- 이 표는 지난 사람의 상태를 지우지 않아 World ID 가 쌓이므로, 색인 없이는 발행·재연결마다 표 전체를 훑는다.
CREATE INDEX ppe_current_state_observation_at_idx ON ppe_current_state (observation_at, source_id);
