-- S15P31A101-365: 결과 원문은 불변으로 보관하고 활성 전환은 별도 이력에 추가한다.
ALTER TABLE scene_version ADD COLUMN scene_output jsonb;
CREATE UNIQUE INDEX scene_version_job_unique ON scene_version (job_id) WHERE job_id IS NOT NULL;

CREATE TABLE scene_activation (
    activation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id text NOT NULL REFERENCES site (site_id),
    scene_version text NOT NULL REFERENCES scene_version (scene_version) ON DELETE CASCADE,
    previous_scene_version text,
    activated_at timestamptz NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL CHECK (length(trim(reason)) > 0)
);
CREATE INDEX scene_activation_site_latest ON scene_activation (site_id, activation_id DESC);
COMMENT ON COLUMN scene_version.scene_output IS 'scene_output 계약 원문: 자산·해시·출처 지도·지표·정합 및 입력 근거';
COMMENT ON TABLE scene_activation IS '현장별 활성 Scene 전환 이력(추가만). 되돌리기도 새 행';
