# 이벤트 근거 설명 빠른 경로 — 분석 워커 `vlm_rationale` 처리기 (S15P31A101-25, S15P31A101-273)

명세 5.4(VLM 빠른 경로·VLM 입력 프레임·권한), 12.5(I5 작업), 15.1 G7(이벤트→근거 p95 5 s, 하향·직접 변경 0건, 사후 클립 대기 0건).
계약 `contracts/messages/vlm_rationale.schema.json`(input·result), `job_request`(priority `VLM_FAST`, timeout_ms 5000), `job_callback`.
이번 범위는 분석 워커 몫이다. Tier 0 의 이벤트 프레임 업로드·`event_frame_ready`, EC2 의 작업 생성·콜백 반영·STOMP 는 아래 "넘긴 일".

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
- **시간:** 처리기는 작업의 `timeout_ms` 를 받지 않는다(`JobContext` 에 없음). 다시 묻기 판단은 계약의 VLM_FAST 값 5000 ms 를 처리기 시작부터 잰다.
  실제 상한은 러너가 `leased_at + timeout_ms` 로 `ctx.cancelled` 를 켜고 TIMED_OUT 을 낸다. 처리기는 단계 사이(관문 전, 호출 전)마다 취소를 본다.
  VLM 요청 하나는 중간에 멈출 수 없다(클라이언트 `SIMAI_VLM_TIMEOUT_S` 기본 30 s).
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

- **Tier 0(이벤트 프레임):** `event_frame_ready` 로 이벤트 시점 프레임과 직전 3~5초 프레임을 올린다. 프레임마다 `camera_id`·`frame_uri`·촬영 시각 세 값(3.9)을
  그대로 넘긴다. 처리기는 `observation_time` 뒤 프레임을 쓰지 않으므로 사후 프레임은 올려도 버려진다. 얼굴은 워커가 처리하므로 원본을 올려도 된다(206).
  프레임 수가 지연을 정한다(3차 세션 G7 p95 2.0~2.1 s 는 그림 4장 기준). 카메라·시각별 개수는 Tier 0·EC2 가 정하되 4장 안팎을 권한다.
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
