# R9 작업 중 안전모 미착용 (Tier 0) 설계

S15P31A101-519. 기획서 §7.2 R9 행과 R9 설명(작업 기준 요구, `ppe_policy` 없음·형식 오류, 사람당 하나, 해소, UNKNOWN), §7.1(확인 필요 이유), §6.4(발생 지속·해소), §5.5(PPE 상태·만료), §8.10(작업 시작·종료 묶음, 기본 꺼짐), §12.3(`config_bundle` 1.3.0·`ppe_policy`), §15.1(G9·G6-R9 통과 뒤 켬)이 기준이다. 명세 v1.3 수정 67, 계약 v7.17.0.

Jira 본문의 "구역 덮어쓰기 우선(확실한 위치)"은 -517(v1.3 수정 66)에서 폐기됐다. 요구는 진행 중 작업이 있으면 현장 전체 REQUIRED, 없으면 NOT_REQUIRED 이고 위치·구역을 보지 않는다.

## 코드와 시험

| 부분 | 코드 | 시험 |
|---|---|---|
| 요구·상태 입력 | `ai/src/simai_ai/risk/ppe.py`(`PpePolicy`, `HelmetObservation`, `PpeSnapshot`, `PpeStateStore`) | `ai/tests/risk/test_ppe_r9.py` |
| R9 규칙 | `ai/src/simai_ai/risk/rules/r9_helmet.py`(`HelmetRequiredRule`, R9 v1) | 같은 파일, 공통 벡터 `ai/tests/risk/vectors/r9_*.json`(형식 1.6) |
| 사람 대상 해소 근거 | `rule.PersonClearEvidence`, `Evaluation.person_clear_evidence`·`rules_cleared_for_all`, `lifecycle` 검증 분기 | 벡터 `r9_work_end_release` |
| 생명주기 기본값 | `LifecyclePolicy.clear_hold_ms_by_rule`(R9 0), `pending_loss_exempt_rules`(R9) | `test_pending_r9_is_not_raised_on_observation_loss`(대조군 포함) |
| 구성 묶음 | `config/bundle.py`(`OPTIONAL_COMPONENT_NAMES`, `ConfigBundle.ppe_policy`), `config/verify.py`(`_ppe_policy`), `config/archive.py` | `ai/tests/config/test_ppe_policy.py` |
| Tier 0 연결 | `services/tier0/service.py`(`PpeStateStore`, `SIMAI_PPE_RELAY` 중계 함수), `services/tier0/judge.py`(`ppe_snapshot`) | `ai/tests/services/tier0/test_tier0_judge.py` R9 절 |
| 기본 꺼짐 | `risk/rule_catalog.py`(`DISABLED_BY_DEFAULT`, `seed_entries`) | `test_r9_is_registered_but_not_seeded` |

## 판정

요구 REQUIRED 일 때 World 트랙마다 같은 World ID·`identity_revision` 의 최신 안전모 상태로 판정한다.

| 안전모 | 판정 | 생명주기 |
|---|---|---|
| NOT_WORN | 발생(CAUTION, `helmet_not_worn`) | 3프레임 중 2프레임이면 RAISED. 관제 화면 안내만(7.3), 노트북 로컬 경보 없음 |
| UNKNOWN(상태 없음·만료·revision 불일치) | 유지(holding, `PPE_UNKNOWN`) | 새 이벤트 없음. 열린 R9 는 등급 유지·확인 필요 |
| WORN | 없음, 해소 근거 | 그 판정에 VERIFIED 해소 |

요구가 없으면(NOT_REQUIRED·`ppe_policy` 없음) 판정하지 않고, 모든 사람의 R9 해소 근거(`clears_all`)를 낸다.

## 명세에 없어 정한 것과 근거

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 만료 기준 시각 | `expires_at_ms < t_latest_global` 이면 UNKNOWN | PPE 시각과 같은 촬영 시각 축이라 REPLAY 도 같은 결과. PPE 가 멈춰 새 메시지가 없어도 Tier 0 가 스스로 만료시킨다(7.2 에 문장 추가) |
| 해소 유지 시간 | R9 0 | 7.2 "그 tick 의 판정 해소". PPE 상태가 이미 3초 창(5.5)으로 걸러진 값 |
| 관측 상실 사전 발생 | R9 는 하지 않음 | 근거가 위치가 아니라 PPE 상태이고 "UNKNOWN 이면 새 R9 를 내지 않는다"(7.2). 다른 규칙은 그대로 |
| 요구 해제 해소의 검증 상태 | 관측과 관계없이 VERIFIED | 19.1 미결을 v1.3 수정 67 로 닫음(이원준 결정 2026-11-06). 요구 해제는 구성으로 확정된 사실. 착용 해소와는 해소 전이의 `config_version` 으로 구분(계약 변경 없음) |
| UNKNOWN 확인 필요 이유 | `PPE_UNKNOWN` 신설 | `UNSPECIFIED` 는 이전 형식 옮길 때만이라 쓰지 않음. 계약 v7.17.0(I1 LIVE, 이원준 승인 2026-11-06) |
| 기본 꺼짐 | 등록부에는 두고 시드에서만 뺌 | 서버는 DB 에 유효한 규칙을 모두 묶음에 싣는다(`RuleSelection.ACTIVE_FOR_PROFILE`). 시드에 넣는 MR 이 켜는 설정이고, Tier 0·EC2 Risk Engine 이 같은 규칙 버전·벡터를 쓴다 |
| “PPE 요구 미수신” | Tier 0 는 알리지 않음 | EC2 가 적용 묶음(`config_ack` APPLIED)의 `ppe_policy` 유무를 안다. 메시지 추가 없음(12.3) |
| 사람 상태 저장 상한 | 256명, 촬영 시각이 오래된 사람부터 지움 | 상한 있는 메모리. 교실·현장 동시 인원보다 충분히 큼 |
| 근거 조항 | `산업안전보건기준규칙 제32조` | 7.2 R9 설명(①1호 안전모). 시드에 들어갈 때 `rule_version.legal_basis` 로 간다 |

## 버린 대안

- Tier 0 환경 변수로 R9 켜기: 묶음 `rules` 와 실제 실행 규칙이 달라지고 EC2 Risk Engine(REPLAY·PLANNED)과 규칙 버전이 어긋난다.
- 시드에 R9 를 넣고 `valid_to` 를 채워 끄기: 시드 형식(같은 버전 값 갱신 블록)에 맞지 않고 켤 때 버전 의미가 흐려진다.
- `UNSPECIFIED` 재사용: 계약 정의(이전 형식 전용)와 어긋나고 관제 화면이 원인을 보일 수 없다.
- Tier 0 가 outbox 를 따로 읽기: 읽은 파일을 지우는 소비자가 둘이 된다. 중계 함수 하나에서 EC2 송신과 판정 입력을 함께 채운다.

## 잔여 위험과 넘긴 일

- Tier 0 PPE 입력은 `SIMAI_PPE_RELAY=1` 일 때만 채워진다. 끄면 모든 사람이 UNKNOWN 이라 R9 는 새로 나지 않는다(안전 쪽). 파일 입력(`input.mode: file`, REPLAY)은 I1 이 없어 PPE 입력이 없다. REPLAY 판정은 공통 벡터로 확인한다.
- 2.1.x 만 아는 EC2 는 `PPE_UNKNOWN` 을 거부한다. R9 를 켜기 전에 서버·프론트가 계약 v7.17.0 을 받아들여야 한다(프론트 문구 `CONFIDENCE_REASON` 은 -520).
- 서버의 작업 시작·종료 `ppe_policy` 발행은 -518, 관제 화면 “PPE 요구 미수신”·요구 해제 해소 구분 표시는 -520.
- Tier 0 지연(6.6): 판정 tick 마다 사람 수만큼 사전 조회와 스냅샷 하나(잠금 한 번)만 더한다. GPU 실측은 하지 않았다(G9 동시 실행 측정에서 함께 잰다).
- 요구사항 FR-74 갱신 필요(명세 README 요구사항 갱신 목록에 반영).
