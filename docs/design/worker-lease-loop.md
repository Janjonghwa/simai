# 분석 워커 작업 대여·실행 루프 (S15P31A101-19)

API 표 v11 FR-101 "작업 대여·실행 루프"(분석 워커, MVP)의 워커 쪽이다. 대여 루프 자체(대여·하트비트·제출·재전송·만료 처리)는
S15P31A101-157 이, Tier 0 우선 접수·GPU 상한·반납은 S15P31A101-60·-197 이 이미 구현했다. 이 티켓은 완료 조건을 진입점
(`python -m services.worker`) 수준에서 실제 HTTP 로 확인하고, 처리기 등록에 계약 검사를 더한다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 12.5 I5 | 워커가 EC2 에서 대여(long-poll) → 하트비트로 연장 → `lease_id` 를 실어 결과 제출. HMAC 없음, 워커 토큰 | 진입점 끝-끝 시험 |
| 3.2 | Tier 0 > VLM 빠른 경로 > 보고서, 분석 워커 GPU 상한 | -60 구현 그대로 (`docs/design/tier0-gpu-priority.md`) |

계약: `contracts/messages/job_lease_request`·`job_request`·`job_heartbeat`·`job_lease_status`·`job_callback`. 변경 없음.

## 티켓 항목과 코드

| 항목 | 위치 |
|---|---|
| long-poll 대여 (`lease_id`·만료 시각) | `ai/services/worker/client.py` `LeaseClient.lease`, `Lease.from_message` |
| 하트비트로 연장 | `ai/services/worker/runner.py` `Worker._heartbeat_due` |
| 결과·실패 제출, 만료 뒤 거부면 버림 | `Worker._submit` (REJECTED → 다시 보내지 않음) |
| 워커 토큰은 환경 변수 | `ai/services/worker/__main__.py` `SIMAI_WORKER_TOKEN`, Pod 는 `deploy/compose.yaml` 의 `--env-file` |
| 작업 유형별 처리기 등록 | `__main__.build_handlers` (유형 → `JobHandler`). 각 작업 스토리가 한 줄씩 넣는다 |
| 시간 제한·우선순위·GPU 상한 | `Worker._supervise`(TIMED_OUT), 우선순위는 EC2 대여 순서, GPU 는 `simai_ai.gpu` |

## 이 티켓에서 더한 것

- **처리기 유형 검사.** `Worker` 를 만들 때 처리기 키가 계약 `job_request.$defs.job_type` 밖이면 `ValueError`. 계약 밖 유형을
  `job_types` 에 실으면 EC2 가 대여 요청을 400 으로 거부하고 워커는 `RequestRejected` 로 멈추므로, 시작할 때 이유를 드러낸다.
  목록은 `client.JOB_TYPES` 상수다. 워커 런타임에는 pydantic(생성 모델)이 없어 상수로 두고, 시험이 계약 enum 과 같은지 본다.
- **진입점 끝-끝 시험** `ai/tests/services/worker/test_main.py`. 가짜 EC2(`conftest.FakeEC2`)를 루프백 HTTP 서버로 열고
  환경 변수의 주소·토큰으로 `main(["--once"])` 를 돌린다. Bearer 토큰 헤더와 모든 메시지의 계약 적합성을 서버가 본다.
  - 샘플 작업이 대여 → 실행 → 하트비트 2회 이상 → SUCCEEDED 제출로 끝나고, 대여 만료 시각이 하트비트로 늘어난다.
  - 실행 중 대여가 만료되면 제출이 `REJECTED`(LEASE_EXPIRED)로 거부되고, 워커는 다시 보내지 않으며 작업은 EC2 대기열로 돌아간다.
  - 두 시험이 실제로 잡는지 돌연변이로 확인했다: 하트비트를 보내지 않게 바꾸면 첫 시험이, 거부 뒤 다시 제출하게 바꾸면 둘째 시험이 실패한다.

## 버린 대안

- 저장소에 샘플(echo) 처리기를 넣어 진입점이 바로 뜨게 하기: 계약 `job_type` 에 샘플 유형이 없고, 실 유형 이름으로 가짜 결과를
  내면 운영에서 오인될 수 있다. 샘플은 시험에만 둔다.
- 처리기 등록을 설정 파일·플러그인으로 하기: 유형이 8개로 닫혀 있어 `build_handlers` 의 사전으로 충분하다.

## 잔여 위험·넘긴 일

- `build_handlers` 가 아직 비어 있어 `python -m services.worker` 는 종료 코드 1 로 끝난다. vlm_rationale(S15P31A101-25),
  static_reconstruct(-226), HMR(-227), bev_floorplan(-239, MVP) 등 각 작업 스토리가 처리기를 넣으면 뜬다.
  (-137·-138 은 같은 작업의 EC2 쪽 스토리다.)
- 실제 EC2 대여 API(S15P31A101-229)·워커 토큰 발급(-222)과의 연동은 그 티켓들이 끝난 뒤 확인한다. 이 시험은 계약을 흉내 낸 가짜 EC2 기준이다.
