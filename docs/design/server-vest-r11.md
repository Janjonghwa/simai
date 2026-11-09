# 조끼 미착용 R11: 명세·EC2 규칙 목록·워치 단어 (S15P31A101-549)

팀이 2026-11-09 조끼 미착용도 경보를 내기로 했다(시연 장면 3, 모델이 조끼를 인식함). 계약 `data/ppe_policy` 1.1.0 의 `vest` 와
Tier 0 판정 규칙 R11 은 S15P31A101-548(이원준)이 만들고, 이 이슈는 그 앞뒤의 명세 개정, EC2 규칙 목록, 워치 단어, EC2 `vest` 발행을 맡는다.
EC2 `ppe_policy` 발행 자체의 설계는 `server-ppe-policy-publish.md` 이고, `vest` 발행을 더할 때 그 문서도 고친다.

## 관련 명세

- 7.1·7.2 R11 행과 R9 문단의 조끼 읽기 규칙, 8.10 조끼 요구, 12.3 `ppe_policy` 1.1.0, 13.3, 15.1 G6-R11·G9 조끼, 16 PPE 판정 범위,
  17 PPE 일반화 위험, 19.1, 부록 D D-G6-10·11 (v1.3 수정 68)
- 부록 C.6 워치 알림

## 코드·시험

| 위치 | 내용 |
|---|---|
| `server/.../rule/RuleCatalog.java` | R11 "작업 중 조끼 미착용"(MVP) 추가. R9 이름·단계를 7.1 표에 맞춤("작업 중 안전모 미착용", MVP) |
| `server/.../watch/WatchAlertService.java` | 단어 표에 R11 "조끼". 11쌍이라 `Map.of`(10쌍 한도) 대신 `Map.ofEntries` |
| `RuleSceneIntegrationTest` | `/api/rules` 가 R1~R11 11개, R9·R11 이름·단계, R11 기본 비활성 |
| `WatchAlertIntegrationTest` | LIVE R11 CAUTION 이벤트가 BOUND 워치로 "조끼"·`CAUTION_WEAK` 로 나감(계약 검증 포함) |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 규칙 번호 | R11 | -548 과 맞춤. R10 은 부록 C 로 옮긴 번호라 다시 쓰지 않는다 |
| R9 와 묶을지 | 따로 연다(사람당 R9·R11 각각) | R9 의미를 바꾸면 메이저 변경. 워치에도 "안전모"·"조끼"가 따로 보여 작업자가 무엇을 고칠지 안다 |
| 워치 단어 | "조끼" | 계약 1~8자, R9 "안전모"와 같은 한 단어 방식 |
| `vest` 없는 묶음의 열린 R11 | 요구 해제로 VERIFIED 판정 해소 | R9 의 `ppe_policy` 없음·요구 해제 규칙과 같게(7.2) |

## 순서

1. -548 머지(계약 1.1.0, Tier 0 R11 기본 꺼짐).
2. EC2 `ppe_policy` `vest` 발행(이 MR, `ConfigBundleService.ppePolicy`·`ConfigBundleViews` 요약·`ConfigBundleIntegrationTest`)(작업 진행 중 REQUIRED, 아니면 NOT_REQUIRED, `format_version` 1.1.0).
3. 배포: Tier 0 를 1.1.0 판으로 먼저 올린 뒤 EC2. 1.0.0 Tier 0 는 `vest` 가 실린 문서를 거부한다(12.3). dev 머지가 아니라 실행 중인 Tier 0 가 -548 판인지 확인한다. 거부되면 Tier 0 는 이전 묶음을 계속 써서 작업 시작의 안전모 요구(R9)도 전해지지 않는다. 배포 직후 `GET /api/config-bundles/{version}` 의 `last_ack.reject_reason` 이 SCHEMA_INVALID 인 묶음이 없는지 본다. 되돌릴 때는 EC2 를 먼저 되돌린다(발행 스위치는 두지 않았다)
4. R11 을 켜는 것은 G9 조끼·G6-R11 뒤 시드에 넣는 별도 MR(8.10).

## 남은 일

- 프런트(-531 또는 후속): `features/events/constants.ts` `RULE_NAME` 에 R11, `PPE_UNKNOWN` 문구("안전모 상태 모름")를 규칙별로,
  `ppePolicy.ts` `PERSON_ONLY_RULES`·요구 해제 표시를 R11 에도, `PpePolicyNotice` 의 R11 미판정 안내. 묶음 보기 요약에 `vest` 가 더해졌다.
- R11 켜기: G9 조끼·G6-R11 뒤 `DISABLED_BY_DEFAULT` 에서 빼고 시드 재생성(8.10).
