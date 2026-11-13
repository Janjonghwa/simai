# S15P31A101-669 규칙 기준 교실 시험 고정

관련 명세: 7.2(R2·R3 높이 프로파일: 운영 2.0 m · 교실 시험 0.35 m), 7.4(교실 시험 별도 rule_version, +1000), 11.1(`config_bundle.rule_profile`). 앞선 설계: `docs/design/S15P31A101-654-ppe-r3-demo.md`.

## 사용자 결정 (2026-11-13)

시연만 하므로 규칙 기준을 교실 시험(CLASSROOM_TEST, 고소 0.35 m)으로 고정하고, 관리자 메뉴(오른쪽 위 펼침)의 「규칙 기준」 구역(운영/교실 시험 선택)을 없앤다.

## 바꾼 것

| 영역 | 내용 |
|---|---|
| DB V67 | `site.rule_profile` 기본값 CLASSROOM_TEST, 기존 현장 행도 CLASSROOM_TEST 로. CHECK(OPERATION·CLASSROOM_TEST)는 그대로. 이미 발행한 `config_bundle.rule_profile` 은 기록이라 고치지 않는다 |
| server 기본값 | `RuleSelection.DEFAULT_PROFILE`(CLASSROOM_TEST)과 `SITE_PROFILE`(현장 행이 없으면 기본값) 질의를 두고, `ConfigBundleService`(현장 행이 없을 때), `RuleViews`(GET /api/rules, 적용된 묶음이 없을 때), `TbmReportRepository.findActiveRules`(적용된 묶음이 없을 때)가 운영 대신 현장 설정(기본 교실 시험)을 쓴다. 현장 등록(POST /api/sites)은 DB 기본값을 받는다 |
| front | `RuleProfileMenu` 삭제, `TopBar` 관리자 메뉴에서 그 구역 제거, `putSiteRuleProfile`·`currentSite.rule_profile` 파싱 제거, 목업 묶음 기본 CLASSROOM_TEST. 관제 머리줄의 기준 표시(`WorkPpeStatus` 「교실 시험 기준 · 0.35m」)는 그대로 둔다 |
| 계약 | 바꾸지 않음. 스키마·예시에 `rule_profile` 기본값이 적혀 있지 않다(v7.47.0 예약은 쓰지 않았다) |

## 남긴 것과 이유

- `PUT /api/sites/current/rule-profile`(관리자)과 `POST /api/config-bundles {"rule_profile"}`: 화면에서는 부르지 않는다. 시연 중 운영 기준으로 되돌려야 할 때(예: 2.0 m 기준 비교) 화면 배포 없이 API 로 바꿀 수 있게 남겼다. 계약 값(OPERATION)도 CHECK·스키마에 남는다.
- 옛 묶음의 `rule_profile` 이 비어 있을 때(V6 전) 규칙 조회는 OPERATION 으로 보인다. 그 묶음이 실은 판이 운영 판이라 기록대로 둔다.
- ai: Tier 0 는 묶음 없이 판정하지 않는다(`services/tier0/service.py` 묶음 없음 → UNSAFE_UNKNOWN). 규칙 판은 묶음의 `rule_versions` 로 등록부(`risk/registry.py`)에서 고르고, 등록부는 운영(v1)·교실 시험(v1001) 판을 모두 가진다. 규칙 클래스의 생성자 기본(`OPERATIONAL_HEIGHT_PROFILE`)은 등록부를 거치지 않는 시험용이라 바꾸지 않았다.

## 버린 대안

- CHECK 에서 OPERATION 을 빼기: 계약·옛 묶음 기록과 어긋나고, 되돌릴 때 마이그레이션이 또 든다.
- 메뉴를 읽기 전용 표시로 남기기: 관제 머리줄에 이미 기준이 보인다(중복).

## 잔여 위험

- 배포 뒤 첫 묶음이 나가기 전까지 Tier 0 는 직전 묶음(운영일 수 있음)으로 판정한다. 배포 뒤 세팅 완료·작업 시작·구역 저장 중 하나로 새 묶음이 나가야 교실 시험 판이 적용된다.
- V67 을 쓴다. 먼저 끝나 먼저 머지될 이 MR 이 작은 번호를 쓰고 동시 진행 -668 은 V68 로 옮겼다(Flyway outOfOrder 꺼짐). 머지 직전에 dev 의 최대 번호를 다시 확인한다.
