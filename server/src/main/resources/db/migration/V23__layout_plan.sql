-- S15P31A101-30: 가상 레이아웃(명세 8.3, 11.1 layout_plan, 12.6 /api/layouts, docs/design/server-layouts.md).
-- TBM 계획에서 놓는 가상 개구부·가설 비계·장비(PLANNED). LIVE 구역·정적 객체·이벤트와 섞지 않는다.
-- items 는 관제 앱 PLANNED 배치 형식([{item_id, kind, center, size, yaw_rad, label}], World m·rad)이다.
-- 수정마다 revision 을 올린다(TBM 보고서·sim.evaluation_run 이 계산한 revision 을 가리킨다, 계약 layout_version "lv-{revision}").
CREATE TABLE layout_plan (
    layout_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id text NOT NULL REFERENCES site (site_id),
    name text NOT NULL,
    items jsonb NOT NULL,
    revision integer NOT NULL CHECK (revision >= 1),
    source text NOT NULL CHECK (source IN ('PLANNED')),
    created_by bigint REFERENCES app_user (user_id),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

COMMENT ON TABLE layout_plan IS '가상 레이아웃(개구부·비계·장비 주차 구역) (M3)';
COMMENT ON COLUMN layout_plan.items IS '배치 요소 [{item_id, kind(OPENING·SCAFFOLD·EQUIPMENT), center[3], size[3], yaw_rad, label}]';
COMMENT ON COLUMN layout_plan.revision IS 'PUT 마다 증가(보고서는 계산한 revision 을 참조)';

CREATE INDEX layout_plan_site_updated_at_idx ON layout_plan (site_id, updated_at DESC);

-- revision 마다 그때의 이름·항목을 남긴다(추가만). 계약 risk_plan_request.layout.layout_version 은 "덮어쓰지 않고 새 버전을 더한다"이고,
-- TBM 보고서가 계산한 lv-{revision} 의 배치를 나중에도 다시 볼 수 있어야 한다. layout_plan 은 지금 revision 의 사본이다
CREATE TABLE layout_plan_revision (
    layout_id bigint NOT NULL REFERENCES layout_plan (layout_id),
    revision integer NOT NULL CHECK (revision >= 1),
    name text NOT NULL,
    items jsonb NOT NULL,
    created_by bigint REFERENCES app_user (user_id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (layout_id, revision)
);

COMMENT ON TABLE layout_plan_revision IS '가상 레이아웃 revision 이력(추가만). 보고서가 계산한 lv-{revision} 배치';
