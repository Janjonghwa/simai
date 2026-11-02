# TBM 작업 단위 보고서 API (`POST·GET /api/reports/tbm`, S15P31A101-418)

관련 명세: `.agy/tbm-interface.md`(인터페이스 A·B·C), `.agy/ref/tbm-legal-graph.md` 5.3·5.4, `.agy/ref/db-tables-rag.md`, `contracts/messages/risk_plan_request.schema.json`.

## 1. 스키마 및 버전 불변성 (V27)

- `V27__tbm_report_legal_graph.sql` 에서 `pg_trgm` 확장, 법령 그래프(`rag_source`·`rag_node`·`rag_edge`), `rag_query_template` 확장 칸(`rule`·`target`·`query_input`), `work_unit` 연결 칸(`layout_id`·`work_type`), `report` 표와 부분 유일 색인(`report_one_generating_per_work`)을 정의한다.
- 보고서는 재생성할 때마다 `(work_id, report_version)` 의 새 행을 추가하며 기존 판을 덮어쓰지 않는다(상태 전이 `GENERATING → SUCCEEDED/FAILED` 갱신만 허용 — 같은 행의 완료 기록이라 AGENTS 6 위반 아님).
- `sim.*` 는 후속이며, 이번 단계에서 `PLANNED` 판정 결과는 `report.risk_items` 에 저장한다.
- 서버 기동 시 이전 실행에서 `GENERATING` 으로 남은 행은 `FAILED`(`failure_reason = 'INTERRUPTED'`)로 마감한다.

## 2. API 및 비동기 생성 흐름

| 메서드·경로 | 권한 | 설명 |
|---|---|---|
| `GET /api/work-types` | 열람 | 7개 표준 공종 상수(`code`, `label`, `rule_tags`) 반환 |
| `POST /api/reports/tbm` | 관리자 | `{"work_id"}` 접수 → `202 Accepted` (`GENERATING`). 레이아웃 미연결 시 `409 WORK_HAS_NO_LAYOUT`, 동일 작업 생성 중일 때 `409 REPORT_IN_PROGRESS`(`report_id` 포함) |
| `GET /api/reports/tbm/{report_id}` | 열람 | 단건 `TbmReport` 조회(`header` 는 `work_unit` 에서 조합) |
| `GET /api/reports/tbm?work_id=` | 열람 | 해당 작업의 최신 보고서(`latest`)와 판 목록(`versions`, 최신순) |

비동기 실행 순서:
1. 작업에 연결된 `layout_plan`(`layout_plan_revision`)의 현재 revision 항목을 읽고 현장의 적용 규칙 판(`config_bundle` 또는 `OPERATION` 기본 판)을 조회한다.
2. `layout → risk_plan_request` 최소 변환으로 `POST /v1/plan/evaluate`(출처 `PLANNED`)를 호출한다:
   - `OPENING`: `zones` 의 `GROUND_FOOTPRINT`(`z_range = null`, `severity = CRITICAL`, `zone_type = OPENING`, OBB 바닥 4점 다각형)
   - `EQUIPMENT`: `zones` 의 `VOLUME_3D`(`z_range = [max(0, cz - sz/2), cz + sz/2]`, `severity = HIGH`, `zone_type = EQUIPMENT_PARKING`)
   - `SCAFFOLD`: `work_surfaces`(`top_height_m = cz + sz/2`, `center_xy`, `size_xy`, `yaw_rad`, `top_height_sigma_m = 0.02`)
3. 작업 구역 및 레이아웃 구역의 최근 7일 `LIVE` `risk_event` 만 집계해 `zone_history` 를 만든다.
4. `POST /v1/report/generate` 호출 시 개인정보·작업 메타(`name`, `planned_people`, `lead`, `place`, 사람 이름)는 제외하고 구조화 값만 전달한다.
5. 호출 타임아웃(기본 90s)은 `ENGINE_TIMEOUT`, 연결 실패·5xx·비정상 응답은 `ENGINE_UNAVAILABLE`, 컨테이너 보고서 생성 실패 응답(`status = FAILED`)은 전달된 `failure_reason`(`CITATION_VIOLATION` 등)과 그때까지 생성된 `subgraph`·`steps` 를 기록한다.
