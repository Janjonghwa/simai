# 세팅 구역의 작업 레이아웃 연결 (S15P31A101-560)

관련 명세: 8.3, 12.6. REST 요청·레이아웃 항목은 `contracts/`의 구간 JSON Schema가 정본인 항목이 아니며, `risk_plan_request` 스키마와는 별개다.

- `POST /api/zones/commit`의 선택 `setup_session_id`는 새 구역 저장에만 허용한다. 계획 작업의 현재 세션과 현장을 잠금 아래 확인하고 저장 트랜잭션에서 `setup_session_zone`을 추가한다. 기존 구역을 소급하거나 시간으로 추정하지 않는다.
- 발행 후 `PUT /api/work-units/{workId}/layout/zones/{zoneId}`는 계획 작업, 현재 세션, 현장과 발행된 구역·작업면 버전을 확인한다. 작업을 잠그고 기존 레이아웃 항목을 보존해 참조를 추가한다. 반복 호출은 중복을 만들지 않는다. 작업면은 `source_zone_id`와 `source_object_id`, 개구부는 `source_zone_id`를 쓴다. 그 밖의 위험 구역은 별도의 가상 물체로 만들지 않는다.
- 사각형이 아닌 개구부, 높이 또는 작업면 방향이 없는 경우에는 형상을 추정하지 않는다. 기존 자동 항목의 직접 형상 수정은 거부한다. 다른 구역 편집 화면에서 원본을 변경·발행하면 연결된 계획 작업을 찾아 형상을 다시 맞춘다. 갱신에 실패하면 저장과 별도로 `layout_sync_failed`를 반환한다. 원본이 사라지거나 작업면 연결이 끊긴 참조는 작업 시작 시 확인 오류로 처리한다.
- 구성 묶음 발행 실패 시 구역 저장 상태와 구분한다. 발행 후 반영이 실패해도 세션 연결을 남기며 화면을 다시 열거나 재시도하면 같은 구역을 다시 반영한다. 원본 참조는 작업 시작·종료에서 새 구역을 만들거나 원본을 보관하지 않는다.

검증: `SetupZoneLayoutServiceTest`, `LayoutZonePlansTest`, `SetupZoneLayoutIntegrationTest`, `SetupSessionIntegrationTest`, `LayoutIntegrationTest`, `WorkSessionIntegrationTest`, `ZoneCommitIntegrationTest`와 관제 앱 세팅 동기화·레이아웃 시험. PostgreSQL/PostGIS 통합 시험에서 세팅 구역 저장·실제 묶음 발행·참조 반영·작업/레이아웃 조회·재시도, 동시 요청, 원본의 시작·종료 보존을 확인했다. 실제 브라우저에서 전체 현장 세팅 작업을 실행하는 시험은 별도로 필요하다.

남은 확인: 구역 편집의 통합 저장(`/api/zones/commit`) 밖에 있는 개별 구역·작업면 수정 API와 별도 구성 묶음 발행 경로에서는 연결된 레이아웃의 자동 갱신을 아직 보장하지 않는다. 실제 브라우저 전체 흐름과 함께 후속으로 연결·검증해야 한다.
