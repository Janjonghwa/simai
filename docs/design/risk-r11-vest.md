# R11 작업 중 조끼 미착용 (계약·Tier 0) 설계

S15P31A101-548. 팀 작업 나눔의 2번(계약 `ppe_policy` 에 조끼 요구)과 4번(AI 조끼 미착용 규칙)이다. 기준은 R9 설계([`risk-r9-helmet.md`](risk-r9-helmet.md))와 명세 §5.5(PPE 상태·만료), §6.4(발생 지속·해소), §7.2 R9, §8.10, §12.3 이다. 계약 v7.23.0(`data/ppe_policy` 1.1.0).

**명세와 다른 점(보고).** 지금 명세 §8.10 은 "조끼 요구는 두지 않는다", §7.2 규칙 표에는 R11 이 없다. 조끼 미착용도 경보를 내기로 한 팀 결정(이원준 2026-11-09)에 따라 계약·Tier 0 를 먼저 준비하고, 명세 수정(1번)은 별도 이슈다. R11 은 기본 꺼짐이라 명세 개정·게이트 전에는 판정이 나가지 않는다.

## 코드와 시험

| 부분 | 코드 | 시험 |
|---|---|---|
| 계약 | `contracts/data/ppe_policy.schema.json` 1.1.0(선택 `vest`), 예시 `ppe_policy.vest`·`.vest_no_work`, 반례 `.work_without_vest`·`.vest_without_work`·`.vest_in_1_0` | `validate_examples.py`, `ai/tests/contracts` |
| 요구·상태 입력 | `ai/src/simai_ai/risk/ppe.py`(`PpePolicy.vest`, `WearObservation`·`PpeItem`, `PpeSnapshot.vests`·`vest_state`, `PpeStateStore` 조끼 저장) | `ai/tests/risk/test_ppe_r11.py`, `ai/tests/risk/test_ppe_r9.py`(그대로 통과) |
| R11 규칙 | `ai/src/simai_ai/risk/rules/r11_vest.py`(`VestRequiredRule`, R11 v1, CAUTION) | 공통 벡터 `ai/tests/risk/vectors/r11_*.json` 7개 |
| 등록·시드 | `registry.py`(R11 등록), `rule_catalog.py`(`DEFAULT_SEVERITY`, `DISABLED_BY_DEFAULT` 에 R11) | `test_rule_catalog.py::test_r11_is_registered_but_not_seeded`, `--check`(시드 파일 변화 없음) |
| 생명주기 | `lifecycle.py` 기본 정책: R11 해소 유지 0, 관측 상실 미리 발생 예외 | `test_ppe_r11.py::test_lifecycle_defaults_treat_r11_like_r9`, 벡터 |
| 구성 묶음 검증 | `config/verify.py`(바꾸지 않음, 스키마로 1.1.0 검사) | `ai/tests/config/test_ppe_policy.py`(1.1.0 통과·반례 거부) |

## 판정

R9 와 같고 안전모 대신 조끼를 본다.

| 조끼 요구 | 조끼 상태(같은 World ID·identity_revision, 만료 전) | 판정 |
|---|---|---|
| REQUIRED | NOT_WORN | 발생 판정 CAUTION(지속 조건 6.4) |
| REQUIRED | UNKNOWN(없음·만료·차수 불일치) | 유지 판정만(확인 필요 `PPE_UNKNOWN`), 새 이벤트 없음 |
| REQUIRED | WORN | 해소 근거(그 사람) |
| 없음(`vest` 없음·NOT_REQUIRED·`ppe_policy` 없음) | — | 판정 없음, 모두 해소 근거(작업 종료 요구 해제) |

## 명세에 없어 정한 것과 근거

| 항목 | 값 | 근거 |
|---|---|---|
| 규칙 ID | R11 | R8(장비 근접)·R10(불확실 점유 근접)이 명세 7.1 표에 있어 다음 빈 번호 |
| 심각도 | CAUTION 하나 | R9 와 같다(관제 화면 안내, 현장 경보 없음, 7.3). 팀 작업 나눔안 |
| 요구 규칙 | 실으면 안전모와 같게 작업 중 REQUIRED, 아니면 NOT_REQUIRED(스키마 allOf) | 이원준 결정 2026-11-09 "안전모와 같게". 작업별로 켜고 끄는 설정은 두지 않는다 |
| `vest` 선택 필드 | 없으면 조끼 요구 없음 | 1.0.0 문서·지금 EC2 가 그대로 통한다. 필수로 두면 EC2 와 동시 배포가 필요하다 |
| `vest` 와 판 | `format_version` 1.1.0 이상에서만 | 1.0.x 문서에 vest 가 섞이는 것을 막는다(반례 `vest_in_1_0`) |
| 기본 꺼짐 | `DISABLED_BY_DEFAULT` 에 R11 | 이원준 결정 2026-11-09. G9(조끼)·G6-R11 통과 전이고, EC2 RuleCatalog 가 R10 까지만 안다 |
| 근거 조항 | 산업안전보건기준규칙 제32조 | R9 와 같은 보호구 조항(조항 파일에 이미 있음). 조끼 조문 특정은 명세 수정에서 다시 본다 |
| 입력 이름 | `HelmetObservation` 을 `WearObservation` 으로 넓히고 이전 이름은 별칭으로 둠 | 안전모·조끼가 같은 형식(`ppe_state` item_state)이고 R9 코드·시험을 바꾸지 않는다 |
| 벡터 형식 | 1.6 그대로(`ppe_policy` 내용이 1.1.0 이면 `vest` 를 읽는다) | 벡터의 `ppe_policy` 는 계약 문서 그대로라 형식 판을 올리지 않았다 |

## 버린 대안

- R9 에 조끼를 합쳐 한 규칙으로 판정: 이벤트·해소·게이트(G6-R9)가 장비마다 달라 섞인다. 사람당 이벤트 하나 규칙(lifecycle 사람 대상 키)도 장비 둘을 구분하지 못한다.
- `vest` 를 필수로: 1.0.0 문서를 보내는 지금 EC2 와 함께 배포해야 한다.
- 조끼 요구를 구역별로: -517 에서 폐기한 방식이다(작업 기준, 8.2).

## 배포 순서와 잔여 위험

- **Tier 0 먼저.** 1.0.0 만 아는 Tier 0 는 닫힌 객체라 `vest` 가 실린 `ppe_policy` 를 받으면 묶음 전체를 거부한다(이전 묶음 유지). 이 판이 Tier 0 에 올라간 뒤 EC2(3번)가 `vest` 를 싣는다.
- R11 을 켜려면: 명세 수정(7.2 표·8.10, 1번), EC2 RuleCatalog·워치 단어(3·5번), 프런트 표시(6번), G9(조끼)·G6-R11 측정 뒤 `DISABLED_BY_DEFAULT` 에서 빼고 시드를 다시 만든다.
- 조끼 검출 품질은 아직 잰 적이 없다(G9 학습 제외 데이터 없음, [`ppe-service.md`](ppe-service.md)).
- 요구사항 FR-74(또는 새 FR) 갱신 필요.
