# 규칙 시드의 첫 운영 배포 전 예외 (S15P31A101-252)

명세 11.4-1 의 "첫 운영 배포 전 규칙 보완 예외"(Lead 승인 2026-10-22)를 규칙 값 시드 생성기에 넣는다.
-211 의 생성기는 같은 (rule_id, rule_version) 의 값이 DB 와 다르면 무조건 멈췄다. -233(18dd634)이 R1 v1 `thresholds` 에
`calibration_margin`·`calibration_widened_states` 를 더한 뒤로는, 그 전 시드가 든 DB(2026-10-22 EC2 배포 등)에서 Flyway 반복
마이그레이션이 멈추고 server 가 뜨지 않는다. -27 조사에서 찾았다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 11.1 `rule_version` | 값은 AI 규칙 등록부에서 뽑고 같은 버전의 값은 바꾸지 않는다(첫 운영 배포 전 예외) | 예외 기간의 갱신 |
| 11.4-1 | 첫 실제 운영 배포 전에는 같은 `rule_version` 안에서 고칠 수 있고 시드를 다시 만들어 같은 행에 반영한다. 첫 운영 배포 시점은 그 배포 MR 이 개정 이력(README)에 날짜와 함께 기록하고, 그 뒤 같은 버전 값 변경은 마이그레이션 검사가 막는다 | 표시 위치·끄는 방법 |

## 정의

- 표시는 생성기 상수 `ai/src/simai_ai/risk/rule_catalog.py` 의 `FIRST_OPERATION_DEPLOY: str | None`(ISO 날짜)다. 생성기가 값에 맞는
  SQL 을 `server/src/main/resources/db/migration/R__rule_versions.sql` 에 쓰고, 머리 주석 `-- 표시:` 줄에 상태를 적는다.
- 시드 SQL 은 세 블록이다. 생성기도 블록마다 함수(`_seed_table_block`·`_value_block`·`_insert_block`)다.
  1. ① 등록부 값을 임시 테이블 `rule_version_seed` 에 담는다.
  2. ② 이미 있는 (rule_id, rule_version) 의 `profile`·`thresholds`·`default_severity` 가 등록부와 다를 때:
     - `None`(첫 운영 배포 전): 그 세 칼럼을 등록부 값으로 `UPDATE` 하고, 행마다 `RAISE NOTICE '첫 운영 배포 전 규칙 보완 예외(11.4-1): R1 v1 값을 등록부 값으로 갱신했다'`.
       Flyway 는 이를 `DB: …` 로 로그에 남긴다.
     - 날짜(첫 운영 배포 뒤): `RAISE EXCEPTION '규칙 값이 바뀌었는데 rule_version 이 같다: R1 v1. 첫 운영 배포(날짜) 뒤라 같은 버전 값 변경 금지, 등록부에서 버전을 올린다'`.
  3. ③ 새 (rule_id, rule_version) 만 `INSERT … ON CONFLICT DO NOTHING`.
- 갱신하지 않는 칼럼: `legal_basis`·`reads`·`valid_from`(·`stage`·`valid_to`). 이미 있는 행에서는 지금처럼 그대로 둔다.
  `valid_from` 을 그대로 두는 것은 예외의 전제(그 버전으로 남은 운영 판정 기록이 없음)에 기댄다.
- 날짜가 `YYYY-MM-DD` 형식이 아니면 생성기가 `ValueError` 로 멈춘다. risk 패키지는 `datetime` 을 import 하지 않으므로(`tests/risk/test_isolation.py`) 형식만 정규식으로 본다.

## 끄는 방법과 시점

- 시점은 **Lead** 가 정한다(첫 실제 운영 배포).
- 그 배포 MR 이 한꺼번에 한다.
  1. `FIRST_OPERATION_DEPLOY = "YYYY-MM-DD"`.
  2. `ai/` 에서 `python -m simai_ai.risk.rule_catalog --write ../server/src/main/resources/db/migration/R__rule_versions.sql`.
  3. 명세 README 개정 이력에 같은 날짜로 첫 운영 배포를 적는다(11.4-1).
- 시드 내용이 바뀌므로 그 배포에서 Flyway 가 반복 마이그레이션을 한 번 더 돌린다. 이때 DB 값이 등록부와 같으면 통과하고,
  그 뒤로 같은 버전 값을 바꾼 시드는 멈춘다. 그래서 끄기 전 마지막 배포에서 예외 갱신이 끝나 있어야 한다(같은 MR 에서 값도 바꾸면 그 배포가 멈춘다).
- `docs/design/ec2-deploy.md` 의 "DB 초기화(첫 운영 배포 전 예외)" 절에 이 문서를 가리키는 한 줄을 두었다.

## S15P31A101-256 과의 경계

- ② 는 `legal_basis` 를 다루지 않는다. `legal_basis` 는 ②와 ③ 사이의 ②' 가 같은 표시(`FIRST_OPERATION_DEPLOY`)로 맡는다(아래 절).

## 근거 조항(legal_basis) 블록 ②' (S15P31A101-256)

R3 v1 `RuleSpec.legal_basis` = `산업안전보건기준규칙 제32조; 산업안전보건기준규칙 제44조`(RAG 조항 ID 조 단위, `ai/src/simai_ai/rag/data/clauses.json`).
R3 v1001(교실 시험 프로파일)과 다른 규칙은 NULL. 생성기 `_legal_basis_block` 이 ②와 ③ 사이에 둔다.

| DB 값 | 시드 값 | 첫 운영 배포 전(`None`) | 첫 운영 배포 뒤(날짜) |
|---|---|---|---|
| NULL | 있음 | 채움 + NOTICE `근거 조항(legal_basis)이 비어 있어 등록부 값으로 채웠다: R3 v1` | 같음(채움 + NOTICE) |
| 같음 | 같음 | 아무것도 안 함 | 아무것도 안 함 |
| 있음 | 다름(NULL 포함) | 갱신 + NOTICE `첫 운영 배포 전 규칙 보완 예외(11.4-1): R3 v1 근거 조항(legal_basis)을 등록부 값으로 갱신했다` | `RAISE EXCEPTION '근거 조항(legal_basis)이 바뀌었는데 rule_version 이 같다: …'`(채우기 전에 멈춤) |

- Jira 256 의 "다르면 `RAISE EXCEPTION`"을 첫 운영 배포 뒤에만 적용하고, 그 전은 ② 와 같이 갱신한다(② 와 같은 분기·메시지 형식).
- 시험: `test_rule_catalog.py` 의 R3 근거 조항·조항 파일 존재(JSON 만 읽음, rag import 없음)·②' 분기. DB(2026-10-26, `simai-rag-db`): 새 DB Flyway 전체 적용 뒤 R3 v1 만 값, 기존 DB(NULL) 채움, 다른 값은 첫 운영 전 갱신·날짜를 넣은 시드에서 멈춤.

## 시험

| 시험 | 보는 것 |
|---|---|
| `ai/tests/risk/test_rule_catalog.py::test_before_first_operation_deploy_updates_same_version` | 표시 없음: EXCEPTION 없음, 세 칼럼 UPDATE·NOTICE, UPDATE 에 `legal_basis`·`reads`·`valid_from` 없음 |
| `…::test_after_first_operation_deploy_raises` | 날짜: UPDATE·NOTICE 없음, EXCEPTION 메시지·머리 주석에 날짜 |
| `…::test_first_operation_deploy_must_be_iso_date` | 날짜가 아니면 거부 |
| `…::test_blocks_in_order` | ①·②·②'·③ 순서 |
| `…::test_committed_seed_is_up_to_date` | 커밋된 시드가 생성기 출력과 같음(기존) |

로컬 DB 확인(2026-10-26, 별도 컨테이너 `timescale/timescaledb-ha:pg17-all@sha256:26e355e…`, compose 와 같은 digest, 포트 55433):

- psql(마이그레이션마다 한 트랜잭션): V1·V2·V3 → 18dd634^ 의 `R__rule_versions.sql`(R1 v1 에 `calibration_margin` 없음).
  1. 새 시드(표시 없음): 종료 코드 0, NOTICE `R1 v1 값을 등록부 값으로 갱신했다` 한 줄. R1 v1 `thresholds` 가 등록부 값과 같아짐
     (`calibration_margin` 0.3, `calibration_widened_states` `["DRIFTED","PROVISIONAL","UNCALIBRATED"]`). 미리 넣은 `legal_basis`·`valid_from` 그대로,
     다른 8행 md5 그대로. 같은 시드 재실행은 NOTICE 없이 통과.
  2. 같은 시작 상태에 표시를 켠 시드(`2026-11-02`): 종료 코드 3, `RAISE EXCEPTION … R1 v1. 첫 운영 배포(2026-11-02) 뒤라 …`, R1 v1 은 옛 값 그대로(트랜잭션 롤백).
     새 시드로 맞춘 DB 에 표시를 켠 시드는 통과(값이 같음).
- Spring Boot Flyway: 빈 DB 에 옛 시드로 서버 기동(5개 적용) → 새 시드로 재기동: 반복 마이그레이션 `rule versions` 한 번 더 적용,
  로그 `DB: 첫 운영 배포 전 규칙 보완 예외(11.4-1): R1 v1 값을 등록부 값으로 갱신했다`, 서버 기동 완료, R1 v1 `calibration_margin` 0.3.

## 버린 대안

- **B. DB 쪽 표시**(설정 테이블 행, Flyway placeholder 를 환경 변수로, 운영 DB 에 손으로 넣는 행): 환경마다 다르게 둘 수 있지만,
  새 V 마이그레이션이나 server `application.yaml` 을 바꿔야 하고 저장소 리뷰에 남지 않는다. ec2-deploy 의 DB 초기화(DROP DATABASE)로 표시가
  사라지고, 명세의 "배포 MR 이 기록"과 진실이 두 군데가 된다.
- **C. 커밋 값 + DB 확인 둘 다**: 표시 하나로 충분해 이득이 작다.
- **예외 기간에 `reads` 도 갱신**: Jira 작업에 없고 판정 값이 아니다. 지금도 비교하지 않는다.

## 잔여 위험

- 첫 운영 배포 MR 이 상수를 잊으면 예외가 계속된다(ec2-deploy 한 줄과 이 문서가 대책). README 개정 이력을 시험이 읽어 맞추는 것은 문구 형식이 정해지면 더한다.
- 예외 기간에 갱신한 행은 `valid_from` 이 처음 넣은 때로 남는다. 그 기간의 판정을 리플레이하면 새 값으로 보인다(예외의 전제상 지킬 운영 판정이 없음).
- `reads` 는 같은 버전에서 바뀌어도 DB 에 반영되지 않는다(기존과 같음).

## 넘긴 일

- R3 `legal_basis` 채우기와 다르면 멈춤: S15P31A101-256 에서 함(②').
