-- S15P31A101-397: 작업면을 구역에 잇는다(docs/design/server-zone-commit.md).
-- 관제 앱은 지금까지 "작업면 OBB 중심이 들어 있는 가장 작은 구역"으로 연결을 계산했다(front zones/zoneSurfaceLink.ts).
-- zone 키가 (zone_id, zone_version)이라 FK 를 걸 수 없어 쓰기 때 지금 구역인지 검사한다.
ALTER TABLE static_object ADD COLUMN zone_id text;

COMMENT ON COLUMN static_object.zone_id IS '작업면이 속한 구역 ID(버전 무관). 한 구역에 지금 작업면은 하나. 장애물은 null';

-- 기존 지금 작업면을 관제 앱과 같은 규칙으로 잇는다: 중심이 든 지금 구역 중 바닥 넓이가 가장 작은 것(같으면 구역 ID 순),
-- 한 구역에 여럿이 들면 작업면 ID 순 첫 번째만. 지난 버전은 그때 연결을 알 수 없어 비워 둔다
WITH candidates AS (
    SELECT DISTINCT ON (s.object_id) s.object_id, s.object_version, z.zone_id
    FROM static_object s
    JOIN zone z ON z.site_id = s.site_id AND z.valid_to IS NULL
        AND ST_Covers(z.polygon, ST_SetSRID(ST_MakePoint(
            (s.obb -> 'center' ->> 0)::double precision, (s.obb -> 'center' ->> 1)::double precision), 0))
    WHERE s.kind = 'WORK_SURFACE' AND s.valid_to IS NULL
    ORDER BY s.object_id, ST_Area(z.polygon), z.zone_id
), firsts AS (
    SELECT DISTINCT ON (zone_id) object_id, object_version, zone_id
    FROM candidates
    ORDER BY zone_id, object_id
)
UPDATE static_object s SET zone_id = f.zone_id
FROM firsts f
WHERE s.object_id = f.object_id AND s.object_version = f.object_version;

-- 한 구역에 지금 작업면 하나
CREATE UNIQUE INDEX static_object_current_zone_key ON static_object (zone_id) WHERE valid_to IS NULL AND zone_id IS NOT NULL;
