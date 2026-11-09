# 작업 레이아웃 구역 동기화 (S15P31A101-557)

## 명세 차이(사용자 결정 11-09)

명세 8.2·8.3은 현장 세팅과 구역 발행 뒤 TBM 가상 배치를 만드는 흐름을 설명한다. 시연 장면 1③에 대한 사용자 결정은 TBM 가상 배치에 추가한 계획 개구부·고소 작업면을 작업 시작 시 관제 구역으로 발행하고, 작업 중 배치를 저장하면 다시 발행하며 종료 시 해당 구역을 끝내는 것이다. 이 결정에 따라 구현한다. 명세 본문 정리는 후속 작업이다.

## 구현

- `LayoutZonePlans`: front `layoutZonePlans`·`zoneShape`와 같은 0.3m 여유, 회전, 0.001m 반올림, 개구부 최소 높이 및 작업면 상판 규칙. `source_zone_id`가 있는 개구부와 장비는 생성하지 않는다. SCAFFOLD는 WORK_SURFACE와 같다.
- `LayoutZoneService`: 작업·레이아웃·항목 출처를 버전에 보관하고 현재 항목과 비교한다. 바뀐 버전은 닫고 추가하며 삭제된 항목과 작업 종료 시 구역·연결 작업면을 닫는다. 같은 형상은 버전을 추가하지 않는다. API 직접 변경은 이력 포함 잠근다.
- 작업 시작·종료와 진행 중 레이아웃 저장은 동기화와 `ConfigBundleService` 발행을 한 트랜잭션에서 수행한다. 발행 실패 시 변경 전체가 되돌아가고 송신은 커밋 뒤다. 배치 관련 트랜잭션은 advisory lock으로 직렬화한다.
- V46은 zone·static_object 버전에 `work_id`, `layout_id`, `layout_item_id`, 비교용 `layout_signature`를 추가한다. 기존 구성 묶음의 Tier 0 문서 형식에는 출처를 넣지 않아 계약 변경이 없다.
- 단위 시험 `LayoutZonePlansTest`, 통합 시험 `WorkSessionIntegrationTest`의 작업 시작 → 발행 → 잠금 → 저장·보관 → 종료 사례. 통합 시험은 PostgreSQL에서 Lead가 실행한다.

## 남은 위험

- 이 환경에서 PostgreSQL 통합 시험을 실행하지 않았다. 실제 PostGIS 마이그레이션·트랜잭션·묶음 내용과 종료 시 동작을 Lead가 확인해야 한다.
- 기존 작업 수정 API가 진행 중 작업의 `layout_id`를 바꾸는 경우는 이 티켓의 동기화 진입점에 포함되지 않는다. 운영 화면에서 이를 허용한다면 구역이 기존 배치에 남을 수 있어 별도 제한이나 동기화가 필요하다.
- `server/CONVENTION.md`가 이 worktree에 없어 기존 서버 관례를 따랐다.
