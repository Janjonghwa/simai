-- TBM 작업(히스토리 작업 다시 보기 GET /api/work-units, docs/design/server-work-units.md).
-- 작업자 이름은 두지 않는다(NFR-14). 담당은 조 이름(예: B조)만. 위험성 평가 문장은 tbm_report(AI, report 표)가 쓰므로 여기 두지 않는다.
-- zone_id 는 구역 ID(버전 무관)라 FK 를 걸 수 없어(zone 키는 (zone_id, zone_version)) 쓰기 때 존재를 검사한다.
CREATE TABLE work_unit (
    work_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id text NOT NULL REFERENCES site (site_id),
    work_date date NOT NULL,
    name text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    zone_id text,
    place text,
    planned_people integer NOT NULL CHECK (planned_people >= 0),
    lead_team text,
    created_by bigint REFERENCES app_user (user_id),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (ends_at > starts_at)
);

COMMENT ON TABLE work_unit IS 'TBM 작업. 히스토리 화면이 작업 구간을 다시 본다(GET /api/work-units)';
COMMENT ON COLUMN work_unit.work_date IS '작업 날짜(현장 시각). 화면이 목록을 날짜로 묶는다';
COMMENT ON COLUMN work_unit.zone_id IS '구역 ID(버전 무관). null 이면 구역 밖 작업이고 place 에 장소를 적는다';
COMMENT ON COLUMN work_unit.lead_team IS '담당 조 이름. 사람 이름을 넣지 않는다(NFR-14)';

CREATE INDEX work_unit_site_starts_at_idx ON work_unit (site_id, starts_at DESC);

-- 작업 목록이 작업마다 구간 안 LIVE 이벤트를 센다(구역 작업은 그 구역만, 구역 밖 작업은 현장 전체)
CREATE INDEX risk_event_live_zone_raised_at_idx ON risk_event (zone_id, raised_at) WHERE source = 'LIVE';
CREATE INDEX risk_event_live_raised_at_idx ON risk_event (raised_at) WHERE source = 'LIVE';
