-- S15P31A101-408: VLM 근거가 이벤트 관측부터 5초 안에 저장됐는지(G7 마감, 명세 5.4·15.1)를 성공 여부와 따로 남긴다.
-- 늦은 성공도 SUCCEEDED 로 저장한다(근거를 버려 지표가 좋아 보이게 하지 않는다, docs/design/vlm-rationale.md). 이 칸이 마감 판정이다.
-- 저장된 event_to_rationale_ms 에서 계산하는 생성 칸이라 기존 행 값을 고쳐 쓰지 않는다. incident_summary 는 5초 대상이 아니라 null.
-- V23(-400)·V24(-403) 예정 번호와 겹치지 않게 V25 다.
ALTER TABLE vlm_assessment
    ADD COLUMN within_deadline boolean GENERATED ALWAYS AS (
        CASE WHEN job_type = 'vlm_rationale' THEN event_to_rationale_ms <= 5000 END
    ) STORED;
COMMENT ON COLUMN vlm_assessment.within_deadline IS
    'G7: vlm_rationale 근거가 관측 시각 + 5000 ms 안에 EC2 에 저장됐는지(event_to_rationale_ms ≤ 5000). incident_summary 는 null';
