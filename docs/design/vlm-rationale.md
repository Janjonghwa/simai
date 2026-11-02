# 이벤트 근거 설명 빠른 경로 — 분석 워커 `vlm_rationale` 처리기 (S15P31A101-25, S15P31A101-273, S15P31A101-384, S15P31A101-408)

명세 5.4(VLM 빠른 경로·VLM 입력 프레임·권한), 12.5(I5 작업), 15.1 G7(이벤트→근거 p95 5 s, 하향·직접 변경 0건, 사후 클립 대기 0건).
계약 `contracts/messages/vlm_rationale.schema.json`(input·result), `job_request`(priority `VLM_FAST`, timeout_ms 5000), `job_callback`.
분석 워커와 Tier 0 프레임 업로드(-328)를 다룬다. EC2 의 작업 생성·콜백 반영·STOMP 는 아래 "넘긴 일".

## 코드

| 위치 | 내용 |
|---|---|
| `ai/src/simai_ai/vlm/rationale.py` | 시스템·사용자 프롬프트, 응답 JSON 스키마(`RESPONSE_FORMAT`), 응답 파싱, 상향 의견 검증(`check_proposal`·`upward_only`, 사유 코드 `PROPOSAL_DISCARD_REASON = "NOT_UPWARD"`), G7 도구와 같이 쓰는 `PROMPT` (순수 함수) |
| `ai/services/worker/vlm_rationale.py` | 처리기: 계약 검사 → 프레임 정렬·사후 프레임 제외 → 얼굴 처리 관문(270) → VLM(22) → 파싱·상향 강제(버린 사유 로그) → 결과 계약 검사 |
| `ai/services/worker/__main__.py` | 객체 저장소·VLM 설정·pose 가중치가 다 있을 때만 등록 |
| `ai/tests/services/worker/test_vlm_rationale.py` | 가짜 저장소·pose·VLM 시험(계약 예시 입력, 상향만 남음, 같은 등급·하향은 사유 코드 `NOT_UPWARD` 와 함께 버림, CRITICAL 입력은 늘 null, `severity` 불변, 사후 프레임, 얼굴 처리 0개, 형식 오류 재질문, VLM 오류, VlmFrame 만) |

## 흐름

1. 입력을 계약으로 검사한다. 위반은 재시도 불가 `VLM_INPUT_INVALID`.
2. 프레임을 `estimated_capture_time_ms` 순으로 놓고 `observation_time` 뒤 프레임은 뺀다(로그 경고, 이벤트 ID 와 개수만).
   사후 프레임·클립을 기다리거나 찾지 않는다(D-G7-02). 이벤트 시점까지의 프레임이 없으면 `VLM_INPUT_INVALID`.
3. `gate_input_frames`(270)로 객체 저장소에서 받아 프레임마다 그 `face_policy` 로 얼굴을 처리한다. VLM 에는 그 결과 `VlmFrame` 만 들어간다
   (22 클라이언트가 그 밖의 이미지를 거부한다). 남는 프레임이 없으면 관문의 `NO_FACE_SAFE_FRAMES` 그대로.
4. `PROMPT.build` 로 질문을 만들고 `response_format` json_schema 로 VLM 을 부른다.
5. 응답을 파싱한다. 형식이 어긋나거나 `finish_reason == "length"`(잘림)면, 첫 호출 지연만큼 시간이 남았을 때(처리기 시작부터 5000 ms 기준)
   "앞 답이 형식에 맞지 않았다" 한 줄을 붙여 한 번 다시 묻는다. 그래도 어긋나면 재시도 불가 `VLM_ANSWER_INVALID`. 응답 원문은 로그에 남기지 않는다.
6. 상향 강제(`check_proposal`) 후 결과를 조립하고 결과 계약으로 다시 검사한다(`severity_upward_only`).

결과: `event_id`·`event_sequence`·`severity` 는 입력 그대로, `rationale` 은 VLM 문장(앞뒤 공백 제거), `proposed_severity` 는 상향만,
`clause_ids` 는 `[]`, `model_version` 은 vLLM 응답의 `model`(없으면 `SIMAI_VLLM_MODEL`), `latency_ms` 는 처리기 시작부터 결과까지.

## 프롬프트

시스템(`SYSTEM_PROMPT`): 역할, 등급을 내리거나 경보를 끄거나 해소할 수 없음, 위치·좌표·경보를 바꾸지 않음(구조화 상태가 정본), 보이는 것만, 얼굴·신원 언급 금지,
상향 의견은 더 위험하다는 근거가 보일 때만, 법령·조항 번호 금지, JSON 하나만.

사용자(`build_prompt`): 규칙 ID·이름(명세 7.1 표)·버전, 등급·신뢰도·확인 필요, (있으면) 신뢰도 사유, 구역 ID·판정 방식, 수치(`metrics`),
트랙별 `world_id`·위치(m)·머리 높이, 프레임별 카메라와 이벤트 시점 기준 상대 시각(초, 이미지 순서와 같음).

응답 스키마(`RESPONSE_FORMAT`, strict):

```json
{"type": "object",
 "properties": {"rationale": {"type": "string", "minLength": 1, "maxLength": 150},
                "proposed_severity": {"enum": ["CRITICAL", "HIGH", "CAUTION", "INFO", null]}},
 "required": ["rationale", "proposed_severity"], "additionalProperties": false}
```

- `rationale` 150자: 1~3문장. 한글은 글자당 토큰이 1 안팎이라 클라이언트 `SIMAI_VLM_MAX_TOKENS` 기본 256 안에 JSON 이 끝나게 잡았다.
- 프롬프트·스키마를 바꾸면 `PROMPT.prompt_id`(지금 `vlm-rationale-1`)를 올린다. G7 답 묶음이 어느 프롬프트로 만든 것인지 이 값으로 가린다.

## 상향 의견 검증과 제안 (S15P31A101-273)

- **등급 순서와 상향만 허용:** 계약·명세 정의(`INFO < CAUTION < HIGH < CRITICAL`, `contracts/common/defs.schema.json` `severity_upward_only`)를 그대로 쓴다. `check_proposal(severity, proposed_severity)`는 `proposed_severity` 가 입력 이벤트의 현재 `severity` 보다 **높을 때만** `(proposed_severity, None)`으로 남긴다.
- **같거나 낮은 의견 폐기와 사유 코드:** 같은 등급이거나 낮은 등급이면 결과를 `null` 로 바꾸고 단일 사유 코드 `NOT_UPWARD`(`PROPOSAL_DISCARD_REASON`, 계약 `event_escalate_result.reject_reason` 과 동일)를 돌려준다. `vlm_rationale` 결과 스키마(`additionalProperties: false`)는 바꾸지 않고 워커 경고 로그에 `reason=NOT_UPWARD`, 이벤트 ID, 현재 등급, 제안 등급(검증된 enum 값만, 응답 원문 제외)을 남긴다. 원래 `proposed_severity` 가 `null` 이면 버린 것이 아니므로 경고 로그를 남기지 않는다.
- **CRITICAL 입력과 `severity` 불변(D-G7-01):** CRITICAL 입력은 더 높은 등급이 없으므로 어떤 의견이 오든 `proposed_severity` 는 늘 `null` 이다(명세 5.4 MVP). 결과의 `severity` 는 항상 입력값 그대로이며, 모든 입력 등급(4) × VLM 의견(5) 조합에서 등급 변경 0건과 결과 계약(`severity_upward_only`) 통과를 자동 시험으로 확인한다.

## 결정

- **상향만 강제:** VLM 이 같거나 낮은 등급을 내면 `null` 로 바꾸고 사유 코드(`NOT_UPWARD`)와 함께 경고 로그를 남긴다. 결과 계약 검사(`severity_upward_only`)로 한 번 더 막는다.
  결과에는 하향·해소를 표현할 필드가 없다(invalid 예시 `vlm_downgrade`·`vlm_resolve` 로 가는 경로가 없음을 등급 4 × 의견 5 조합 시험으로 보인다).
- **clause_ids 는 `[]`:** 계약상 "RAG 로 검색된 조항 ID 만"이다. 워커에는 조항 검색이 없다(`tbm_report` 는 EC2 가 검색한 조항을 입력으로 받는다,
  `simai_ai.rag` 는 시드·임베딩만). VLM 응답 스키마에 조항 칸을 두지 않고 시스템 프롬프트로 조항 번호를 금지한다. VLM 이 쓴 조항 문자열은 결과에 들어갈 길이 없다.
  규칙 근거 조항(`rule_version.legal_basis`)을 붙이려면 EC2 가 입력에 조항(검색 결과)을 넣어 주는 계약 변경이 필요하다.
- **다시 묻기 1회:** 형식 강제(json_schema)를 걸어도 vLLM structured outputs 가 꺼졌거나 잘리면 어긋날 수 있다. 두 번째도 어긋나면 같은 입력으로 다시 해도
  같을 가능성이 커서 재시도 불가로 끝낸다(GPU 를 더 쓰지 않게).
- **시간 예산 전파(S15P31A101-384):** 러너(`Worker.process`)가 대여의 `timeout_ms` 와 `deadline`(`received + lease.time_left_ms / 1000`)을 `JobContext` 에 넣어 넘기고, 직접 호출(단위 시험 등)에서 `deadline` 이 비어 있으면 처리기가 시작 시각(`t0`) 기준으로 `JOB_BUDGET_MS`(5000 ms) 상한을 묶는다. 객체 서명·전송(`HttpObjectStore`), 프레임 병렬 다운로드·얼굴 처리 관문(`gate_input_frames`·`prepare_vlm_frames`), VLM 호출(`VlmClient.complete(timeout_s=...)`), 재질문 판단이 모두 `ctx.remaining_s()` 와 `ctx.cancelled` 를 단계마다 확인해 남은 시간 예산 안에서만 실행된다.
- **pose 검출기:** `SIMAI_TIER0_WEIGHTS`(deploy/compose.yaml, YOLO26x-pose `.pt`)를 `calibration.extrinsics.extract.UltralyticsPose` 로 열어
  `face_blur.PoseDetectorProvider` 로 감싼다. 새 검출 코드는 없다. Tier 0 의 TensorRT 엔진(`SIMAI_TIER0_ENGINE`)은 Tier 0 프로세스 몫이라 쓰지 않는다.
  가중치가 없거나 ultralytics 가 없으면 등록하지 않고 시작 로그에 이유를 남긴다(다른 처리기는 돈다).

## 오류 코드

| 코드 | 재시도 | 경우 |
|---|---|---|
| `VLM_INPUT_INVALID` | 아니오 | 입력 계약 위반, 이벤트 시점까지의 프레임 없음 |
| `FRAME_FETCH_FAILED` | 저장소 오류대로 | 프레임 받기 실패(270 관문) |
| `NO_FACE_SAFE_FRAMES` | 검출 실패가 있으면 예, 디코드 실패뿐이면 아니오 | 얼굴을 처리한 프레임이 하나도 없음(270 관문) |
| `VLM_CONFIG` | 아니오 | VLM 환경 변수 오류(등록 때 걸러지므로 보통 안 남) |
| `VLM_INPUT` | 아니오 | VlmFrame 아닌 이미지 등(22 클라이언트) |
| `VLM_REQUEST_REJECTED` | 아니오 | vLLM 4xx(429 제외), 예: 문맥 길이 초과 |
| `VLM_TIMEOUT` | 예 | 요청 시간 초과 |
| `VLM_UNAVAILABLE` | 예 | 연결 실패·429·5xx |
| `VLM_BAD_RESPONSE` | 예 | chat completions 형식 아님·빈 내용 |
| `VLM_ANSWER_INVALID` | 아니오 | 다시 물어도 응답 JSON 이 스키마에 안 맞음·잘림 |
| `VLM_RESULT_INVALID` | 아니오 | 조립한 결과가 계약 위반(나오면 버그) |
| `CANCELLED` | 아니오 | 러너가 취소(대여 상실·시간 초과) |
| `TIMEOUT`(러너) | — | 실행 상한 초과, status TIMED_OUT |

## 넘긴 일

- **Tier 0(이벤트 프레임):** -328 구현·선택 기준·운영 로그는 아래 절. 실제 교실에서 지연·선택 품질 측정은 9차 GPU 세션에 남긴다.
- **EC2(작업 생성·콜백):** `job_type vlm_rationale`, priority `VLM_FAST`, timeout_ms 5000, CRITICAL 이벤트에만·병합 이벤트당 1회(5.4, D-G7-03).
  각 프레임 `face_policy` 는 BLURRED(기본) 또는 EXCLUDED. `structured_state` 는 `tracks[]`(`world_id`·`position_xy`·`head_z`)와 `zone`(`zone_id`·`judge_type`) 모양을 읽는다
  (계약은 객체만 정한다). 콜백 반영 때 `severity` 가 이벤트의 현재 등급과 같은지, `proposed_severity` 는 의견으로만 저장하는지(경보 등급을 바꾸지 않음, 273) 확인한다.
- **G7 도구(35):** `--prompt simai_ai.vlm.rationale:PROMPT` 로 제품 프롬프트를 쓴다. 도구가 처리기와 같은 프레임 순서·사후 프레임 제외·프레임 줄(카메라·상대 시각)과
  같은 답 규칙(잘림·형식 오류는 한 번 다시 묻고, 그래도 어긋나면 `VLM_ANSWER_INVALID` 로 채점에서 뺌)을 쓰도록 맞췄다(`docs/design/g7-eval.md` "제품 처리기와 맞춘 점").
- **실제 녹화 재생 결과가 오면:** 얼굴 처리 영역이 실제 교실 각도에서 눈·코·입을 덮는지, 근거 문장이 프레임·구조화 상태와 맞는지(G7 표본), p95 지연(관문 + VLM),
  `VLM_ANSWER_INVALID` 비율, 150자 상한에서 잘림이 없는지.

## 잔여 위험

- 실제 vLLM(Cosmos3-Nano)에서 json_schema 강제와 한국어 응답을 아직 보지 않았다(GPU 세션 없음). 다음 GPU 세션에서 합성 이미지 1건으로 스키마 적합과 지연을 본다.
- 관문의 pose 검출은 워커 프로세스에서 프레임마다 한 번 돈다. 그 지연이 G7 5 s 안에 드는지 실제 GPU 에서 재야 한다.

## EC2 작업 생성·결과 반영 (S15P31A101-329)

관련 명세는 5.4·11.3·12.3·12.5·15.1 G7이다. 계약은 변경하지 않았다.

### 서버 흐름과 코드·시험 위치

1. `domain/event/VlmRationaleIngestor`를 I1 저장기로 등록했다. `event_frame_ready` 1.x를 원본 계약으로 검사하고,
   `source_id`가 이벤트의 `gateway_id`와 같은지 확인한다. 이벤트 행을 잠근 뒤 LIVE·CRITICAL이고 AUDIT_ONLY가 아니며,
   현재 sequence의 관측 시각에서 5초 전이고 기존 `vlm_rationale` 작업이 없을 때만 등록한다.
   재전송의 msg_id가 달라도 이벤트 ID로 중복을 거른다. V18의 부분 유일 인덱스가 DB에서도 병합 이벤트당 한 작업을 보장한다.
   이미 성공·실패·취소된 작업도 재생성하지 않는다. 이벤트 전이가 아직 없거나 스냅샷만 있어 관측 시각을 모르면 생성하지 않는다.
2. 작업은 `AnalysisJobService`에 `vlm_rationale`로 넣는다. 우선순위 `VLM_FAST`, `timeout_ms=5000`, `max_attempts=2`다.
   두 시도는 기존 계약 예시와 같은 초기값이며, 재시도가 GPU를 계속 점유하지 않게 제한했다.
   이벤트의 규칙·등급·확인 필요·지표·World ID·관측 시각을 입력에 복사하고 프레임 주소와 촬영 시각 세 값은 그대로 둔다.
   모든 프레임의 `face_policy`는 BLURRED다. 사후 클립을 기다리지 않으며 사후 프레임 제외는 -25 워커가 한다.
3. `structured_state.tracks[]`는 같은 gateway·World ID의 이벤트 `evidence_ref`가 가리킨 World 상태만 읽는다.
   ref가 없을 때는 이벤트 관측 시각과 정확히 같은 상태만 쓴다. 이벤트보다 미래의 상태를 붙이지 않고, 없으면 빈 tracks다.
   위치는 미터 XY 그대로다. `head_z_validity=VALID`일 때만 머리 높이를 넣고 나머지는 null이며,
   `head_z_validity`·`observation_state`·존재하는 `input_provenance`도 함께 둔다.
   zone은 이벤트 관측 시각에 유효한 판의 `eval_type`을 `judge_type`에 넣는다. 없으면 null로 둔다.
4. 이벤트 관련 VLM 작업의 `analysis_job.deadline_at`은 입력 `observation_time+5000ms`다.
   기준은 이번 작업이 설명하는 이벤트 상태의 Tier 0 관측 시각이며, EC2 프레임 수신·큐 등록·워커 실행 시작 시각으로 미루지 않는다.
   대여 SELECT가 이 시각부터 작업을 제외하고, 스케줄러/대여 요청의 만료 정리에서 대기 작업을 TIMED_OUT으로 끝낸다.
   재시도 가능한 실패는 5초 전이면서 시도가 남았을 때만 다음 attempt로 돌린다. 시도 미소비 반납도 5초가 지나면 끝낸다.
   대여 만료 후 재할당에도 같은 제한을 둔다. 실패하면 근거를 만들거나 기존 등급을 바꾸지 않는다.
5. I5 컨트롤러가 `job_callback` 계약을 검사한다. `JobLeaseService`가 소유 워커·대여 유효성·중복을 확인한 뒤
   `VlmRationaleListener`가 입력의 event_id·event_sequence·severity와 결과를 비교하고 현재 이벤트의 LIVE·severity도 확인한다.
   MVP의 `proposed_severity`는 null, `clause_ids`는 없거나 빈 배열만 허용한다. 어긋난 결과는 제출 전체를 롤백한다.
   나중에 이벤트 sequence가 증가했어도 입력 상태에 대한 근거는 저장할 수 있다. 결과 sequence를 현재 sequence로 바꾸지는 않는다.
6. 성공 제출 트랜잭션 안에서 `vlm_assessment`에 추가하고 `risk_event.rationale`·`proposed_severity` 최신 투영만 갱신한다.
   등급·생명주기·전이 이력·World 값은 변경하지 않는다. 저장 중 오류가 나면 제출 상태·평가·투영 모두 롤백된다.
   중복 대여 제출은 DUPLICATE이고, 평가의 job_id 유일성도 중복 추가를 막는다.
7. `RiskEventsDelivered`를 발행하면 기존 `Tier0Relay`가 커밋 뒤 `/topic/events`에 `risk_event` 2.0.0 봉투로 보낸다.
   payload는 `EventQueryRepository`의 기존 화면 투영이므로 `rationale` 문자열과 null인 `proposed_severity`를 그대로 읽는다.
   이 이벤트는 새 현장 경보·알림 생성에 쓰지 않는다.

V18은 로컬 `origin/dev`의 최신 V17 다음 번호다. V1에는 `vlm_assessment`가 주석에만 있었으므로
`docs/design/db-tables.md` H절의 칸에 G7용 `event_observed_at`·`event_to_rationale_ms`를 더해 신설했다.
`created_at`은 EC2 저장 시계, `latency_ms`는 워커 처리 지연이라 서로 다르다.
이미 유효한 대여의 성공 결과는 I5 계약대로 5초 뒤에도 받으며, 실제 지연을 그대로 기록한다. 5초는 신규 대여·재시도 중단선이며,
늦은 성공을 버려 G7 지표가 좋아 보이게 하지 않는다.

시험 위치:

- `domain/event/VlmRationaleTest`: I1 입력 계약·필터·BLURRED·중복·출처·콜백 일치·근거 투영·발행 요청·G7 시각.
- `domain/job/VlmDeadlineTest`: 4999/5000ms 경계, 남은 시도, 시도 미소비 반납 제한.
- `domain/job/VlmRationaleIntegrationTest`: 동시 프레임 통지의 실제 잠금·한 작업, World 입력, 결과 저장·I2 릴레이의 화면 계약,
  중복 콜백, 잘못된 결과의 제출 전체 롤백, 새 lease/attempt 재시도, 5초 뒤 대여 중단.
- 기존 `WorkerJobIntegrationTest`: 다른 작업 유형과 I5 대여·연장·제출·반납 회귀.

### 9차 GPU 세션에서 볼 로그·DB

Pod는 검증 큐를 준비한 뒤에만 켠다. LIVE CRITICAL 한 건에서 Tier 0의 `event_frame_ready` 전송,
EC2 `simai.i1.messages{msg_type=event_frame_ready,result=stored}` 증가, 워커의 `vlm_rationale` 대여·처리를 연결해 본다.
성공 시 EC2 로그 `VLM 근거 저장: event_id=... job_id=... event_to_rationale_ms=...`가 한 번 나온다.
로그에는 프레임 주소·영상·근거 본문을 남기지 않는다. 다음 SQL은 읽기 전용이다.

```sql
-- 등록·대여·실패 코드: 한 이벤트에 한 행, VLM_FAST/5000/최대 2시도
SELECT event_id, job_id, priority, timeout_ms, status, attempt, max_attempts,
       created_at, deadline_at, error->>'code' AS error_code
FROM analysis_job WHERE job_type = 'vlm_rationale' ORDER BY created_at DESC LIMIT 20;

-- G7: 워커 지연과 이벤트→EC2 저장 지연을 별도로 본다
SELECT a.event_id, a.job_id, a.event_sequence, a.rule_severity, e.severity,
       a.proposed_severity, a.latency_ms, a.event_observed_at, a.created_at, a.event_to_rationale_ms,
       e.rationale = a.text AS latest_matches
FROM vlm_assessment a JOIN risk_event e USING (event_id) ORDER BY a.created_at DESC LIMIT 20;

SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY event_to_rationale_ms) AS g7_p95_ms,
       count(*) AS succeeded_samples
FROM vlm_assessment WHERE job_type = 'vlm_rationale';

SELECT job_id, lease_id, attempt, status, leased_at, submitted_at, consume_attempt
FROM job_lease WHERE job_id IN (SELECT job_id FROM analysis_job WHERE job_type = 'vlm_rationale')
ORDER BY leased_at DESC LIMIT 30;
```

측정 시 해당 세션의 event_id/시간 범위로 제한한다. 성공 지연 분포와 함께 FAILED·TIMED_OUT·근거 없는 이벤트 수도 별도로 센다.
관제 화면의 AI 설명이 새로고침 없이 나타나는지, 등급과 원본 전이 수가 그대로인지, 같은 이벤트 재통지에도 작업/평가가 한 건인지 확인한다.
입력 받기/VLM 실패를 재현해 5초 전에는 남은 시도만 다시 나가고 5초부터는 대여가 없으며 근거 없이 남는지도 확인한다.

### 잔여 위험·넘긴 일

- 실제 Postgres(PostGIS·TimescaleDB) 마이그레이션·잠금·STOMP 경로는 로컬 DB 통합 시험과 9차 세션에서 확인한다.
  배포 전 기존 데이터에 같은 event_id의 `vlm_rationale` 작업이 여러 건 없는지 확인한다(유일 인덱스는 중복 데이터에서 실패한다).
- 프레임 통지가 이벤트 전이보다 먼저 오거나 관측 시각 없는 스냅샷만 있으면 작업을 생성하지 않는다.
  프레임을 별도 보관해 뒤늦게 결합하는 큐는 이번 범위 밖이다. -328 송신 순서를 함께 확인한다.
- 이벤트 상태 전이와 프레임 업로드·객체 내려받기·얼굴 관문·GPU 대기·VLM·DB 지연을 합친 G7 실측은 9차 세션 몫이다.
- STOMP는 기존 비영속 릴레이를 따른다. 브로커 오류 시 DB 근거는 보존되고 REST 재조회로 읽을 수 있으나 실시간 재전송은 보장하지 않는다.

## S15P31A101-328 — Tier 0 이벤트 프레임

관련 명세: 3.2(Tier 0 우선), 3.9(촬영 시각), 5.4(R1~R4 CRITICAL·병합 이벤트당 1회·사후 대기 없음), 12.3(I1).
코드 `ai/services/tier0/event_frames.py`, 배선 `judge.py`·`service.py`, 합성 시험 `ai/tests/services/tier0/test_event_frames.py`.
기존 I1 `event_frame_ready` 1.0.0과 업로드 API `POST /api/uploads/sign` 목적 `EVENT_FRAME`을 그대로 쓴다. 계약 변경 없음.

### 발생과 카메라·시각 선택

- LIVE 출처의 R1~R4 CRITICAL이 처음 RAISED 된 때만 이벤트 ID를 기억한다. UPDATED·RESUMED·RESTORED·중복 RAISED는 보내지 않는다.
  같은 병합 이벤트는 재개돼도 1회다. 실패·큐 초과도 재판정이나 반복 업로드를 만들지 않는다. 기억은 프로세스 메모리이며 재기동 복원 전이는 대상이 아니다.
- 해당 tick의 대상 World Track에서 `cameras`에 나타난 카메라를 우선한다. 여러 사람이면 더 많은 대상이 보인 카메라 우선, 동률·관측 근거 없음은 카메라 ID 순이다.
  별도 pose 추론·새 위치 추정은 하지 않는다. 이것은 대상 관측 카메라 우선순위이며 실제 가림 정도나 선명도의 최적화를 보장하지 않는다.
- 기본 최대 2카메라 × 2시점 = 4장. `observation_time` 기준 최근 프레임(0~500 ms 전)과 3~5초 전 중 가장 최근 프레임을 고른다.
  현재 프레임의 500 ms는 끊긴 카메라의 옛 영상을 현재 장면으로 쓰지 않기 위한 초기 상한이다. 과거 구간의 목표는 3초 전으로 두어 움직임 문맥과 G7 예산을 함께 고려했다.
  해당 창에 없으면 그 장은 비운다. 카메라에 영상·디코딩·업로드 성공분이 없으면 다음 우선순위 카메라를 확인한다. 최소 1장 성공 때만 메시지를 보낸다.
- 사후 촬영 프레임은 cut 끝과 선택 창에서 제외한다. 디코더 지연 출력은 이미 가진 패킷의 flush만 쓴다. 사후 프레임 수신이나 이벤트 클립 완료를 기다리지 않는다.
- 기존 압축 링 버퍼에 I0 헤더의 `timestamp_source`·`timestamp_uncertainty_ms`를 함께 보관하고 촬영 시각과 함께 그대로 송신한다(모르는 불확실성은 null).
  디코더 출력은 `(stream_epoch, frame_sequence)`로 원본 헤더와 결합한다. epoch·코덱 변경·손실 뒤 키프레임에는 디코더를 다시 연다.

### 부하·실패 격리와 설정

`JournalWriter.submit_step → 현장 경보 → 클립 대상 → EventFrameService.on_transitions(put_nowait)` 순서다.
판정 tick에서 영상 복사·디코딩·JPEG·HTTP·계약 검사를 하지 않는다. 작업 큐는 8건이며 영상 바이트를 담지 않는다.
단일 CPU 스레드가 기존 60초 압축 링 버퍼에서 잘라 기존 PyAV CPU 디코더로 복원하고 JPEG 품질 85로 인코딩한다.
JPEG 85는 안전모·자세 문맥을 유지하면서 전송 크기를 줄이기 위한 초기값으로, 실제 입력 품질·G7은 세션에서 확인한다.
상시 JPEG 저장이나 두 번째 RTSP 연결은 만들지 않는다. 얼굴은 기존 워커 관문이 VLM 호출 전에 처리한다.

서명 요청에는 이벤트 ID·JPEG 형식·크기·SHA-256을 보내고 기존 `HttpPutTransport`로 서명 주소에 PUT한다.
서명·PUT 시간 제한은 각각 2초다. 한 장 실패는 로그만 남기며 성공한 나머지 장을 보낼 수 있다. 재업로드 재시도는 없다.
메시지는 만료되는 업로드 URL이 아닌 서버 응답 `object_uri`(`s3://...`)를 쓴다. 계약 검사 뒤 `I1Publisher.send_event`로 신뢰 큐에 넣는다.
I1 큐가 가득 차면 같은 payload만 붙들고 50 ms 간격으로 다시 넣는다(업로드 반복 없음). 이동안 다른 프레임 작업은 8건까지만 대기한다.
신뢰 큐 이후 단절·재연결은 기존 I1 정책을 따른다. 큐 초과·종료 시 메모리 작업은 유실될 수 있고 디스크 재시도함은 추가하지 않는다.
한 이벤트의 업로드 도중 프로세스가 끝나면 일부 객체만 남을 수 있다. 프레임 보존·고아 객체 정리는 저장소 운영 정책 확인이 필요하다.

Tier 0 JSON 설정 `event_frames_enabled`는 기본 true이며 false로 끈다. 실제 LIVE 입력과 I1 연결 설정이 있을 때만 조립한다.
SIM 입력·file(REPLAY) 입력은 조립하지 않고 전이 출처도 LIVE만 통과시킨다. PLANNED·WHAT_IF·REPLAY에는 계약에 없는 출처 필드를 끼워 보내지 않는다.
종료 때 판정 스레드를 멈춘 뒤 프레임 스레드에 중지를 알리고 기존 종료 deadline 안에서 기다린 다음 I1을 닫는다.
JPEG·디코딩·HTTP 실패, 링 버퍼 부재, 계약 오류는 경보·저널·Tier 0 값에 영향을 주지 않는다.

### 9차 GPU 세션에서 볼 로그와 남은 검증

```text
event_frames_uploaded event_id=... frames=4 event_to_upload_ms=...
event_frame_ready_queued event_id=... frames=4
event_frames_failed event_id=... reason=queue_full|no_frames|...
event_frames_camera_failed event_id=... camera_id=... reason=예외종류
event_frames_upload_failed event_id=... camera_id=... reason=예외종류
```

`event_to_upload_ms`는 Tier 0 주입 시계의 전이 `occurred_at`부터 마지막 성공 업로드까지다(노트북 촬영 시각과 다른 시계를 빼지 않는다).
큐 대기·디코딩·JPEG·서명·PUT을 포함한다. EC2 -329와 워커 완료 로그의 같은 event_id를 연결해 G7 이벤트→근거 p95 5초를 잰다.
위 로그에는 토큰·서명 URL·예외 원문을 싣지 않는다. 실제 느린 네트워크에서 4장 순차 업로드와 CPU 디코딩이 5초 예산 안에 드는지는 미측정이다.
Tier 0 `capture_to_risk`·`frame_age`와 함께 비교해 CPU 공유 영향이 보이면 설정으로 끄고 우선순위·프레임 수를 후속 조정한다.
원격 API·객체 저장소·EC2 작업 생성까지의 통합 시험은 이 로컬 합성 시험으로 대체할 수 없으며 -329 연동과 함께 확인해야 한다.

## S15P31A101-384 — 이벤트 사진 병렬 다운로드와 남은 시간 예산 전파

관련 명세: 3.2(Tier 0 우선·분석 워커 시간 제한), 5.4(VLM 빠른 경로·VLM 입력 프레임·얼굴 처리 fail-closed), 12.5(I5 `vlm_rationale` `timeout_ms=5000`), 14(객체 저장소 서명·전송), 15.1 G7(이벤트→근거 p95 ≤ 5 s). 계약 변경 없음.

### 배경

9차 GPU 세션(일본 H100 ↔ 시드니 S3 `ap-southeast-2`, 프레임당 왕복 약 1.8\~1.9 s)에서 `vlm_rationale` 워커가 이벤트 사진 4장을 순차적으로 서명·GET·얼굴 처리하면서 다운로드 단계만으로 `VLM_FAST` 실행 상한 5 s를 넘겨(`TIMED_OUT`) vLLM 호출이 0회였다. 또한 `HttpObjectStore`(`sign_timeout_s=10.0`, `data_timeout_s=20.0`)와 `VlmClient`(`timeout_s=30.0`)가 작업의 남은 시간 예산을 모르고 고정 타임아웃으로 동작해, 시간 초과 뒤에도 워커 스레드가 소켓 응답이나 추가 프레임 검출을 기다리는 잔여 작업 위험이 있었다.

### 코드와 시험 위치

| 위치 | 내용 |
|---|---|
| `ai/services/worker/handlers.py`, `runner.py` | `JobContext` 에 `timeout_ms`, `deadline`(monotonic 초), `monotonic` 주입과 `bind_budget`·`remaining_s`·`is_expired` 추가. `Worker.process` 가 `received + lease.time_left_ms / 1000` 을 `deadline` 으로 전달 |
| `ai/services/worker/objects.py` | `HttpObjectStore._sign`·`_transfer` 시작 전 `ctx.cancelled` 와 `ctx.remaining_s()` 확인. 남은 시간이 0 이하면 요청을 시작하지 않고 즉시 `ObjectStoreError(..., retryable=True)` 발생, 양수면 `min(설정 상한, remaining_s)` 를 소켓 타임아웃으로 사용 |
| `ai/services/worker/vlm_frames.py` | `gate_input_frames` 가 입력 프레임을 제한된 병렬 스레드(`DEFAULT_FETCH_WORKERS = 4`, `min(max_workers, len(frames))`)로 동시에 내려받고 촬영 시각 정렬 순서를 보존. 그 뒤 남은 시간 예산 안에서만 순차적으로 얼굴 처리 수행 |
| `ai/src/simai_ai/vlm/face_blur.py` | `prepare_vlm_frames` 에 `cancelled`·`remaining_s` 인자 추가. 각 프레임 디코드 전과 `provider.people(image)` 검출 전에 확인해 예산 소진(`reason="budget"`)·취소(`reason="cancelled"`) 프레임을 건너뛰고 `VlmFrame` 생성을 차단(fail-closed 유지) |
| `ai/src/simai_ai/vlm/client.py`, `ai/services/worker/vlm_rationale.py`, `incident_summary.py` | `VlmClient.complete(..., timeout_s=...)` 지원(`min(settings.timeout_s, timeout_s)`, 0 이하면 인코딩·전송 전 즉시 `VlmTimeout`). 처리기가 VLM 초회·재질문 호출마다 `ctx.remaining_s()` 를 전달 |
| `ai/tests/services/worker/test_objects.py`, `test_vlm_frames.py`, `test_vlm_rationale.py`, `test_runner.py`, `ai/tests/vlm/test_face_blur.py`, `test_client.py` | 서명·GET 남은 시간 클램핑, 4장 병렬 다운로드 동시성·입력 순서 보존, 얼굴 처리 도중 예산 소진 시 검출 중단 및 fail-closed, 9차 세션(4장 병렬 1.8 s + 검출 0.4 s → 남은 2.8 s 로 VLM 호출) 재현 시험 |

### 정한 수치와 근거

- **`DEFAULT_FETCH_WORKERS = 4` (`vlm_frames.py`):** Tier 0 이벤트 프레임(`-328`)이 최대 2카메라 × 2시점 = 4장을 올리므로 기본 병렬 다운로드 상한을 4로 두어 4장의 서명·GET 왕복을 1회 왕복 시간(약 1.8 s)으로 겹친다. 프레임 수가 더 적으면 `min(max_workers, len(frames))` 로 줄이고, 1장이면 스레드 풀을 띄우지 않고 현재 스레드에서 바로 받는다.
- **단계별 소켓 타임아웃 = `min(기존 상한, ctx.remaining_s())`:** 기존 `sign_timeout_s = 10.0`, `data_timeout_s = 20.0`, `SIMAI_VLM_TIMEOUT_S = 30.0` 은 `incident_summary`(120 s)나 `static_reconstruct` 같은 긴 작업에서는 단일 소켓 동작 상한으로 유효하지만 `vlm_rationale`(5 s)에서는 전체 예산보다 길다. 둘 중 짧은 값을 쓰면 긴 작업의 단일 소켓 보호와 짧은 작업의 전체 예산 상한을 동시에 만족한다.
- **예산 소진으로 모든 프레임이 빠진 경우 `NO_FACE_SAFE_FRAMES(..., retryable=True)`:** 네트워크·GPU 일시 지연으로 첫 시도에서 얼굴 처리까지 마치지 못한 경우, EC2 `deadline_at`(`observation_time + 5000 ms`) 전이고 남은 시도가 있으면 재시도할 수 있게 `detect` 실패와 같이 `retryable=True` 로 분류한다(디코드 실패만 있을 때만 `retryable=False`).

### 버린 대안

- **얼굴 검출(`provider.people`)까지 멀티스레드로 병렬 실행:** 워커의 `PoseDetectorProvider` 는 단일 `UltralyticsPose`(PyTorch/CUDA) 인스턴스를 공유한다. 여러 스레드가 동시에 `detect()` 를 호출하면 내부 predictor 상태 경합과 CUDA 메모리 급증으로 워커 GPU 메모리 상한(`cap_process_memory`)을 깨뜨릴 수 있어, 네트워크 I/O(`store.get`)만 병렬화하고 디코드·검출은 핸들러 스레드에서 남은 시간 예산 안에서 순차 실행한다.
- **시간 예산이 모자랄 때 얼굴 처리를 생략하고 원본 프레임을 VLM 에 전달:** 명세 5.4·11.5 및 `AGENTS.md` 규칙 8(fail-closed 원칙) 위반이다. 예산 부족으로 얼굴을 처리하지 못한 프레임은 반드시 `DroppedFrame(reason="budget")` 으로 버리고 `VlmFrame` 토큰을 발급하지 않는다.

### 잔여 위험과 넘긴 일

- Python `urllib` 소켓 호출은 시작 시점에 남은 시간(`timeout=min(limit_s, remaining_s)`)으로 타임아웃이 걸리므로, 요청이 이미 시작된 도중에 EC2 가 대여를 취소(`REJECTED`)하면 해당 소켓 읽기 완료(최대 남은 작업 예산, `vlm_rationale` 은 ≤ 5 s) 시점에 스레드가 정리된다. 이는 워커 `abandon_grace_s`(30 s)보다 충분히 짧아 `HandlerStuck` 을 일으키지 않는다.
- S3 버킷 리전 이전(시드니 → 서울)과 실제 GPU Pod 에서 4장 병렬 다운로드 적용 후 G7 `event_to_rationale_ms` p95 실측은 다음 GPU 세션에서 확인한다.

## S15P31A101-408 — 로컬 사진·즉시 대여

관련 명세: 3.2(Tier 0 우선·분석 워커), 5.4(VLM 빠른 경로·VLM 입력 프레임·얼굴 처리 fail-closed), 12.3(I1 `event_frame_ready`), 12.5(I5 대여 long-poll·`vlm_rationale`), 14(객체 저장소 서명), 15.1 G7(이벤트→근거 p95 5 s).
계약 v6.12.0: 공통 `pod_local_frame` 신설, I1 `event_frame_ready` 1.1.0·I5 `job_request` 3.5.0(마이너, 프레임 선택 `local_copy`, 이벤트 마감 `deadline_at`). 마이그레이션 V25(`vlm_assessment.within_deadline`).

### 10차 GPU 세션 실측(2026-11-02, 일본 H100 ↔ 시드니 S3, 서울 EC2)

자료: `simai-local/session10/results-20261102/setup/session1/{run-s10.log, worker-manual.log}`.

| 구간 | 값 | 비고 |
|---|---|---|
| Tier 0 이벤트 → 사진 4장 서명·S3 업로드 끝(`event_to_upload_ms`) | n 57, p50 2.29 s, p95 7.1 s, 최소 1.8 s, 최대 9.5 s | EC2 는 이 뒤에 오는 `event_frame_ready` 를 받아야 작업을 만든다. 디코딩은 없다(추론 프레임 참조 재사용), 거의 다 서명·시드니 PUT 왕복 |
| `event_frame_ready_queued` → 워커 `작업 시작`(워커가 놀고 접수 문이 열림) | 49~74 ms(약 30건) | 서버 대여는 이미 long-poll + 같은 프로세스 깨우기(`JobSignal`)라 폴링 지연이 아니다 |
| 같은 구간, 워커가 앞 작업을 처리 중 | 0.5~6.4 s | 워커는 한 번에 한 작업. 40 초에 CRITICAL 13건 같은 몰림에서 줄을 선다 |
| 같은 구간, Tier 0 우선 접수 문이 닫힘 | 13.8~134 s(6건, 일부 VLM 마감 초과) | `추론 대기 상한을 넘겨 버린 프레임 1개` 뒤 30 s 안정 대기 동안 vlm_rationale 도 대여하지 않는다(`AdmissionGate`, 이번 범위 밖) |
| 워커 처리(작업 시작 → 제출 반영, 성공 34건) | p50 2.89 s(2.65~3.21 s) | S3 서명·GET(시드니) + 얼굴 처리 + Cosmos 추론 |
| 이벤트 → 근거 완료(Lead 집계, 성공 건) | 7.1 s | 기준 5 s 초과. 워커 시작 전 이벤트 7건·접수 문 닫힘 2건은 시작도 못 하고 마감(`VLM_DEADLINE`) |

티켓 설명의 "작업 생성 → 집기 2.15 s"는 폴링 주기가 아니라 위 셋째·넷째 줄(앞 작업 대기·접수 문 닫힘)이다. 놀고 있는 워커는 이미 0.1 s 안에 집는다.

### 바꾼 흐름

```
이전: 이벤트 → JPEG 4장 → 서명(서울) → PUT(시드니) ×4 병렬 → event_frame_ready → EC2 작업 → 워커 → 서명·GET(시드니) ×4 → 얼굴 처리 → VLM
이후: 이벤트 → JPEG 4장 → 상태 폴더에 원자적 쓰기 + 서명(서울) → event_frame_ready(객체 주소 + local_copy) → EC2 작업 → 워커 → 로컬 사본 읽기·sha256 확인 → 얼굴 처리 → VLM
                                     └→ 뒤 업로드 풀: PUT(시드니) ×4 (보관용, 이벤트 근거·이력·클립)
```

| 위치 | 내용 |
|---|---|
| `ai/services/tier0/event_frames.py` | `local_root`(Tier 0 `state_dir`) 가 있으면 `_prepare` 가 JPEG → `<state_dir>/event-frames/<event_id>/<n>-<camera>-<촬영 ms>.jpg` 원자적 쓰기(임시 파일 → `os.replace`) → 서명. 서명이 끝나면 S3 업로드를 기다리지 않고 `event_frame_ready` 1.1.0 에 `frame_uri` 와 `local_copy`(상대 경로·`sha256:`·크기)를 싣는다. PUT 은 상주 뒤 업로드 풀(4 스레드, 대기 사진 상한 `UPLOAD_BACKLOG` 32장)에서 계속하고 이벤트별로 끝나면 `event_frames_uploaded ... background=1`. 사본 쓰기에 실패한 장은 예전처럼 업로드가 끝난 뒤에만 싣는다. 사본 폴더는 1시간 뒤 지운다(`LOCAL_RETENTION_S`). `local_root` 가 없으면 예전 순서(올린 뒤 알림) 그대로 |
| `ai/services/tier0/service.py` | `local_root=s.state_dir`, 이벤트 사진 PUT 시간 제한 2 s → 5 s(뒤에서 돌므로 경보·VLM 이 기다리지 않음) |
| `ai/services/tier0/i1_client.py` | `event_frame_ready` 송신 판 1.1.0 |
| `ai/services/worker/vlm_frames.py` | `LocalFrames`: 상태 폴더(`SIMAI_STATE_DIR`, 없으면 `SIMAI_TIER0_MARKER` 의 폴더, 없으면 `$SIMAI_ROOT/state`) 기준으로 계약 꼴 경로만 받고, `resolve(strict=True)` 로 링크를 따라간 실제 위치가 `<state>/event-frames` 안인지·정규 파일·크기·sha256 을 확인한 뒤에만 쓴다. 아니면 경고(사유 코드만)와 함께 S3. S3 는 아직 없는 객체(403·404)·일시 오류를 0.25 s 간격 최대 4회(남은 시간 안) 다시 받는다. 받은 바이트는 사본이든 S3 든 같은 얼굴 처리 관문을 거친다 |
| `ai/services/worker/objects.py` | `ObjectStoreError.status`(전송 단계 HTTP 상태) |
| `ai/services/worker/vlm_rationale.py`, `__main__.py` | 처리기에 `LocalFrames.from_env` 주입, 성공 때 `근거 완료 frames= gate_ms= vlm_ms= total_ms=` 한 줄 |
| 서버 | 작업 생성 시점이 핵심이다: `VlmRationaleIngestor` 는 Tier 0 의 로컬 준비 알림(업로드 완료 전)으로 바로 작업을 만들고, 원래 프레임을 그대로 복사하므로 `local_copy` 가 작업 입력에 실린다(`frame_uri` 는 s3:// 그대로, 로컬 경로는 별도 필드). `job_request` 3.5.0 에 `deadline_at`(= `analysis_job.deadline_at`, 관측 + 5 s)을 싣는다. 성공 콜백은 늦어도 SUCCEEDED 로 저장하되 마감 판정을 따로 남긴다(`vlm_assessment.within_deadline` 생성 칸 V25, 로그 `within_deadline=true/false`, 초과는 WARN). 대여는 이미 long-poll(`wait_ms` 까지 요청을 붙잡고 `JobSignal` 로 커밋 직후 깨움)이라 바꾸지 않았다 |
| `ai/services/worker/client.py`, `runner.py` | `Lease.deadline_left_ms = deadline_at − sent_at`(EC2 시계끼리). 처리기 남은 예산(`ctx.deadline`)과 러너 취소·TIMED_OUT 은 `min(leased_at + timeout_ms, deadline_at)` 기준. 10차의 7.12 s 건처럼 이벤트 뒤 늦게 대여된 작업은 관측 + 5 s 에 멈춘다(메시지 "이벤트 관측 + 5000ms 마감 초과"). `deadline_at` 이 없는 관리·시험 작업은 예전처럼 실행 상한만 |

시험: `ai/tests/services/tier0/test_event_frames.py`(PUT 이 막혀 있어도 사본과 함께 먼저 알림·사본 바이트=서명 바이트=업로드 바이트·임시 파일 없음, 사본 쓰기 실패면 업로드 뒤에만 알림, 서명 실패 장만 빠짐·오래된 사본 정리), `ai/tests/services/worker/test_vlm_frames.py`(사본 우선·저장소 미호출, 사본 없으면 S3, 리더 없으면 무시, sha256·크기 불일치 거부, 형식 밖 경로 5종 거부, 링크로 상태 폴더 밖을 가리키면 거부(Windows 는 정션), 환경 변수, S3 403·404 짧은 재시도·상한·서명 거부는 재시도 없음), `test_vlm_rationale.py`(처리기·진입점 배선), 서버 `VlmRationaleTest`(사본 알림으로 작업 생성·입력에 사본 위치, 경로 탈출은 계약 위반), `WorkerJobIntegrationTest`(VLM 작업 등록부터 대여 응답까지 0.3 s 미만, 작업이 없으면 `wait_ms` 를 채우고 204).

### 로컬 입력 동작(누락·재시작·만료)

- **같은 규칙:** 관측 시각 뒤 프레임은 처리기가 관문 전에 빼므로 사본도 읽지 않는다. 사본 바이트도 S3 바이트와 같은 얼굴 처리 관문(BLURRED·EXCLUDED, 처리 못 하면 제외)을 거치고 VLM 에는 `VlmFrame` 만 간다. `VlmFrame.source` 는 `frame_uri` 그대로다.
- **누락:** 파일이 없거나(다른 Pod, Tier 0 상태 폴더 초기화) 폴더가 없으면 조용히(정보 로그) S3 로 받는다. 형식·경로 탈출·크기·sha256 이 어긋나면 경고(사유 코드) 뒤 S3.
- **워커 재시작:** 사본 읽기는 메모리 상태 없이 디스크만 본다. 재시작한 워커·재시도 대여도 같은 사본을 쓴다.
- **만료:** Tier 0 가 이벤트 폴더를 1시간 뒤 지운다. 그 뒤 들어온 재시도(있을 수 없음: 마감 5 s)나 수동 작업은 S3 로 받는다.
- 시험: `test_vlm_rationale.py` 의 `test_local_input_gets_same_late_frame_exclusion_and_face_processing`·`test_local_copy_restart_and_expiry_behaviour`, `test_runner.py` 의 마감 예산 두 시험, 서버 `VlmRationaleIntegrationTest`(job_request `deadline_at`, 7.12 s 늦은 성공 = SUCCEEDED + `within_deadline=false`).

### 정한 수치와 근거

- **사본 위치 `state_dir/event-frames/<event_id>/`:** Tier 0 와 워커는 기동기(`simai-bootstrap`)가 같은 `SIMAI_TIER0_MARKER = <work>/state/tier0.live` 를 주고, Tier 0 `state_dir` 은 그 폴더다(`settings.marker = state_dir/tier0.live`). 워커는 새 설정 없이 표시 파일 폴더로 상태 폴더를 찾는다(`SIMAI_STATE_DIR` 로 덮을 수 있음).
- **계약 경로 꼴을 좁게:** `event-frames/<uuid>/<[A-Za-z0-9_-]{1,96}>.jpg` 만 받아 `..`·절대 경로·역슬래시를 스키마에서 막고, 워커가 실제 위치를 한 번 더 본다(링크·정션). 파일을 쓰는 쪽도 같은 정규식으로 확인해 꼴이 어긋나면 사본을 빼고 메시지는 보낸다.
- **sha256·크기:** 서명에 쓴 값과 같은 바이트다. 사본이 바뀌었거나 덜 써졌으면 쓰지 않는다(원자적 쓰기라 덜 쓴 파일은 원래 보이지 않는다).
- **S3 재시도 0.25 s × 최대 4회:** 사본이 없을 때(다른 Pod) Tier 0 PUT p50 이 약 2 s 라 첫 GET 이 403·404 일 수 있다. 남은 시간(5 s 예산)을 넘지 않게 매번 확인한다. 403 은 설정 오류일 수도 있어 횟수를 묶었다.
- **보관 1시간:** 판단용 사본은 VLM 마감(관측 + 5 s)과 재시도 1회 뒤에는 쓰지 않는다. 디스크가 쌓이지 않게 하되 현장 확인 여유를 둔다. 보관용 정본은 S3 다.
- **PUT 5 s:** 뒤에서 돌므로 2 s 로 끊어 보관본을 잃을 이유가 없다. 종료 때 대기 업로드는 버리고 진행 중인 PUT 은 이 시간 안에 끝난다.

### 버린 대안

- **S3 버킷 서울 이전만:** 일본·인도 등 GPU 지역은 그때그때 바뀌어(AGENTS) 서울도 왕복 수십~백 ms × (TLS + 서명 + PUT/GET) 두 번이 남는다. 같은 Pod 안의 두 프로세스가 원격 저장소를 두 번 왕복할 이유가 없다. 이전은 보관·클립 지연을 줄이는 별도 결정(사용자 연기)으로 둔다.
- **사진 수·크기 줄이기(2장, JPEG 품질 낮추기):** 명세 5.4 의 2대 × 2시점 근거와 얼굴 처리·VLM 판단 품질을 깎는다. 왕복 횟수는 그대로라 지연이 크게 줄지 않는다.
- **워커가 Tier 0 에서 직접 받는 소켓·공유 메모리:** Tier 0 에 새 서버·포트를 열고 Tier 0 경로가 워커 수명에 묶인다(명세 3.2 Tier 0 우선, import-linter 의 Tier 0 ↔ 워커 분리). 파일은 원자적 쓰기만으로 충분하고, 실패해도 S3 예비가 있다.
- **이벤트 전에 서명(객체 키)을 미리 받아 두기:** 키에 `event_id` 가 들어가고(`event-frames/<event_id>/…`) PUT 서명에 크기·sha256 이 묶여(14) 사진 전에 받을 수 없다. 내용과 묶지 않는 서명은 무결성을 약하게 한다. 서명은 4장 병렬 1회 왕복(서울)이라 남겨 두고 다음 세션에서 `event_to_ready_ms` 로 잰다.
- **대여 폴링 주기 줄이기·새 긴 대기 방식:** 서버는 이미 long-poll + 깨우기다(실측 49~74 ms). 바꾸면 EC2 요청만 는다. 0.3 s 목표는 시험으로 고정했다.

### 잔여 위험

- **명세와 다름:** 명세 5.4·12.3 은 "업로드한 뒤 `event_frame_ready`" 다. 1.1.0 에서는 `local_copy` 가 있는 프레임은 업로드 완료 전에 알린다(없는 프레임은 예전대로). 명세 정리 MR 에 반영이 필요하다.
- **보관용 업로드 유실:** 알린 뒤 PUT 이 실패하면 S3 에는 객체가 없고 1시간 뒤 사본도 지워진다(이전에도 PUT 재시도는 없었다). 종료 때 대기 업로드는 버린다. 이벤트 근거 보관을 S3 로 보장하려면 업로드 재시도·사본 보존 연장이 따로 필요하다.
- **다른 Pod 의 워커:** 사본이 없어 S3 예비로 가고, 그때는 업로드 완료 전에 작업이 올 수 있어 재시도 0.75 s 안에 객체가 없으면 `FRAME_FETCH_FAILED` 다(남은 시도 1회).
- **접수 문(Tier 0 우선)과 직렬 처리:** 집기 지연의 실제 원인은 30 s 안정 대기 동안 VLM 도 막는 접수 문과 워커 한 작업씩 처리다. 이번에는 바꾸지 않았다(Lead 설계). VLM_FAST 만 안정 대기에서 빼거나(지연 초과·모름일 때는 그대로 막음), vLLM 동시 요청을 쓰는 VLM 작업 2개 병렬(pose 검출만 잠금)을 후속으로 검토한다.
- **디스크:** 이벤트당 약 4 × 0.3~0.6 MB, 1시간 보관. 몰림(분당 20건)이면 1시간에 약 2 GB 다. 실제 크기는 다음 세션에서 본다.

### 다음 GPU 세션에서 잴 것

```text
event_frames_ready event_id=... frames=4 local=4 event_to_ready_ms=...     # 목표 p95 0.5 s 이하(JPEG + 쓰기 + 서명)
event_frames_uploaded event_id=... frames=4 event_to_upload_ms=... background=1
이벤트 사진 4장 중 4장을 같은 Pod 로컬 사본으로 읽었다                      # 0장이면 상태 폴더 설정 확인
이벤트 ...: 근거 완료 frames=4 gate_ms=... vlm_ms=... total_ms=...
```

- G7 `event_to_rationale_ms` p95(위 SQL)와 성공·마감 건수. 5초 안 완료율은 `SELECT avg(within_deadline::int) FROM vlm_assessment WHERE job_type = 'vlm_rationale'`(성공 건 기준, 마감 실패 TIMED_OUT 은 `analysis_job` 에서 따로 센다). 이벤트 → 준비 알림, 알림 → 작업 시작(워커 대기·접수 문 닫힘 따로), 처리(얼굴 처리·VLM)로 나눠 본다.
- `local=` 가 4가 아닌 건, `로컬 사본을 쓰지 않는다(reason=...)` 경고 수(0 이어야 한다).
- 사본 폴더 크기(`du -sh <state>/event-frames`)와 뒤 업로드 실패(`event_frames_upload_failed`) 수, S3 객체가 정말 생겼는지(이벤트 몇 건).
- 접수 문 닫힘 때문에 시작 못 한 VLM 작업 수(워커 `Tier 0 우선 접수 문: 받지 않음` 구간과 `job_lease`·`analysis_job` 대조).
