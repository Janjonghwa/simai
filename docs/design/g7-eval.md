# G7 평가 설계 (S15P31A101-35)

관련 명세: 15.1 G7 행·"G7 사람 평가", 5.4(VLM 빠른 경로 입력, 권한), 부록 D.8, 16장 "VLM 모델"(Cosmos3-Nano 확정, G7 은 이 모델의 합격 여부만).
도구 `ai/experiments/g7_eval/`(`g7_samples.py`·`g7_answers.py`·`g7_score.py`), 시험 `ai/tests/experiments/g7_eval/test_g7_eval.py`.
이 문서는 평가 설계와 도구까지다. VLM 답 생성은 GPU 세션, 채점은 구현자가 아닌 평가자가 한다.

## 기준과 판정 규칙

| 행 | 기준 | 판정 근거 |
|---|---|---|
| 근거-상태 일치 | 표본 100건 이상, 사람 평가 일치율 ≥ 95% | `g7_score.py aggregate` |
| 이벤트 → 근거(CRITICAL 빠른 경로) | p95 ≤ 5 s | 답 생성 지연(answers) 또는 `vlm_int.py` 지속 동시 4 결과. 3차 세션 p95 2.12 s(`unified-env.md` "3차 GPU 세션 결과"), 4차 v4 재측정(S15P31A101-291) |
| 보고서 인용 존재 / 적절성 | 100% / 사람 평가 ≥ 80% | 존재: 225 인용 검사(`tbm.check_draft`) 자동, 적절성: `cite-aggregate` |

- **세 행 모두 통과해야 G7 합격**이다. 미달이면 15.1 대응을 따른다: 근거는 입력 구조화(프롬프트·구조화 상태 정리), 지연은 입력 프레임 수·해상도 축소, 인용은 RAG 문서·질의 보강. 근거 일치 미달이 이어지면 16장 VLM 모델 결정을 재검토한다.
- 다음이면 집계는 내도 **G7 판정을 내지 않는다**(`verdict_blocked_by`):
  - 자리 표시 프롬프트로 만든 답(25 전).
  - 제품 지표가 아닌 출처: SIM, 시나리오 골든, `is_test_input` 녹화(AGENTS 7).
- 미채점 표본이 있으면 통과로 보지 않는다. 평가자 판정이 갈렸는데 조정이 없으면 불일치로 센다(보수).
- 지연 p95 는 최근접 순위로 잰다. answers 지연은 동시 1 이다. 제품 조건(동시 4·Tier 0 동시)의 p95 는 `vlm_int.py` 결과를 `--latency-summary` 로 넣는다.

## 표본 100건 구성안

### 출처 조사

| 출처 | 내용 | 장점 | 단점 | 제품 지표 |
|---|---|---|---|---|
| (a) 교실 녹화 세션 + Tier 0 재생 | 15.3 시나리오 데이터셋 촬영(테이프 구역 진입 150회 이상, 40cm 단상 60회 이상, 매트 위 눕기, 오경보용 활동). 파일 모드 Tier 0(`risk_event.jsonl`·`world_state.jsonl`)로 이벤트를 만들고 녹화에서 프레임을 뽑는다 | 실제 카메라 각도·압축·조명·얼굴 처리(270)가 제품과 같다. 이벤트가 실제 규칙 판정에서 나온다 | 촬영·정답 요약 작성에 사람 시간이 든다. 저장소에 아직 세션 manifest 가 없다(`docs/data/sessions/` 없음) | **예**(RTSP 녹화, `is_test_input` false) |
| (b) 시나리오 골든·시뮬레이터 | `ai/tests/scenarios/cases/` 7개(R1·R2·R3·R7, 한 사람). 그림은 프레임 번호를 새긴 합성 H.264 이고 사람이 없다 | 정답이 설계값으로 정확하다. 도구 리허설(묶음→답→채점표→집계)을 사람 영상 없이 돌릴 수 있다 | 그림에 사람이 없어 VLM 시각 근거를 평가할 수 없다. 사례 7개뿐이다 | **아니오**(SIM, AGENTS 7) |

**고른 구성:**
- G7 판정 표본 100건은 (a) 녹화에서만 뽑는다.
- (b)는 GPU 세션 전 리허설과 도구 회귀에만 쓴다(`--source-kind SIM`, 프레임 없이 묶기 가능).

### 층

- 차원: 규칙(`rule_id`), 등급(`severity`), 관측 상태(`check`: 확인 필요 = `confidence_reasons` 있음 / 확정), 카메라 수(관련 트랙을 본 카메라 2대 이하 / 3대 이상 / 모름), 사람 수(그 시각 트랙 수 1 / 2+).
- 기본 뽑기 차원은 `rule_id,check,people` 이다. 칸마다 섞고 칸을 돌아가며 하나씩 뽑아 칸 사이 차이를 1 이하로 둔다. 모자란 칸은 있는 만큼만 넣는다.
- 나머지 차원(등급·카메라 수)은 manifest `distribution` 에 남긴다.
- 기본 범위는 제품 빠른 경로와 같다: R1~R4, CRITICAL(5.4 MVP).
  - R3(HIGH)·R5~R7 은 빠른 경로 밖이라 기본에서 빠진다.
  - 넣으려면 `--rules`·`--severities` 로 넓힌다. 이것은 결정 항목이다(아래).
- 목표 칸 수(예시, 녹화 결과에 따라 조정):

| 규칙 | 확정 | 확인 필요 | 비고 |
|---|---|---|---|
| R1 구역 진입(CRITICAL 구역) | 15 | 15 | 경계 머물기 포함 |
| R2 가장자리 근접 | 15 | 15 | 단상, `Z_UNKNOWN`·`PREDICTED` 포함 |
| R4 낙상 의심 | 15 | 15 | 매트 위 눕기, 급하강 직후 관측 상실 |
| R6 이 붙은 위 규칙 | 5 | 5 | 구역 안 관측 상실(UNVERIFIABLE) |

  - 사람 수 2+ 장면이 각 칸의 3분의 1 이상 되도록 촬영한다(추적 시나리오와 겹쳐 찍기).
  - 오경보용 활동(빠르게 앉기·물건 줍기)에서 나온 이벤트도 규칙이 CRITICAL 로 냈다면 표본에 들어간다. VLM 이 그때도 상태와 모순되지 않게 설명하는지가 근거 일치의 대상이다(등급은 그대로).

### 표본 한 건

`samples/<id>/`:
- `input.json`: vlm_rationale 입력 모양(`frames[]` 의 `frame_uri` 대신 묶음 안 `file`)과 `confidence_reasons`.
- `frames/<camera>_m4000.jpg`·`_p0.jpg`: 이벤트 시점 + 4초 전. 카메라마다 기준 40 ms 안 프레임만 넣고, 없으면 비운다.
- `truth.json`: 사람이 재생을 보고 쓴 정답 상태 요약.

`manifest.json` 에는 출처, `product_metric`, 층 분포, 해시가 들어간다.

- 흐림은 묶음 단계에서 하지 않는다. 답 생성기가 VLM 직전에 270 관문으로 처리한다(제품과 같은 자리).
- 사람이 찍힌 그림이라 묶음·답·채점표·채점 결과 원본은 저장소 밖(또는 `.gitignore` 의 `runs/`·`recordings/`)에 둔다.
- 커밋하는 것은 `manifest.json` 사본과 `g7_result.json`(점수만, 그림·답 원문 없음) 뿐이다.
- 정답 요약(`truth.jsonl`) 한 줄: `{"event_id", "summary", "people", "notes"}`. 재생 뷰어로 이벤트 전후를 본 사람이 쓴다. 평가자와 다른 사람이면 좋다.

## 채점 기준표 초안

### 근거-상태 일치 (표본마다)

평가자는 흐린 그림, 구조화 상태, 정답 요약, VLM 답을 보고 세 칸을 채운다.

| 칸 | 값 | 정의 |
|---|---|---|
| verdict | 일치 | 답의 모든 사실 진술이 구조화 상태(규칙·구역·위치·관측 상태)와 정답 요약에 맞는다. 확인 필요 이벤트에서는 불확실함을 단정하지 않는다 |
| | 부분 | 핵심(무엇이 왜 위험한가)은 맞지만 부수 사실이 틀리거나 빠졌다. 예: 위치는 맞는데 사람 수를 틀림, 확인 필요를 단정형으로 씀 |
| | 불일치 | 핵심이 상태와 모순된다. 예: R2 가장자리 근접을 "구역 진입"으로 설명, 관측 상실 이벤트를 "사람이 서 있다"로 설명 |
| 지어냄 | Y/N | 그림·상태에 없는 사람·물체·행동을 만들어 냈다. 예: 없는 사다리, 안전모 미착용 단정(PPE 는 VLM 판정 대상 아님) |
| 등급변경 | Y/N | 입력 등급을 바꾸거나 낮추는 표현이 있다. 예: "위험하지 않다", "경미하다", "경보를 해제해도 된다". 상향 의견은 결과의 `proposed_severity` 몫이라 문장 안 상향 표현도 Y 로 적고 메모한다 |

- 표본은 verdict 가 일치이고 지어냄·등급변경이 모두 N 일 때만 **일치**로 센다. 부분은 일치로 세지 않는다.
- 답이 없는 표본(VLM 오류·얼굴 처리 실패)은 채점하지 않고 key_map `excluded` 에 사유와 함께 남긴다. 100건은 채점된 표본 기준이므로 오류가 나면 표본을 더 뽑는다.

### 보고서 인용

- 존재(자동): 결과 `sections[].paragraphs[].citations` 와 본문 조항 표기가 입력 `clauses` 에 있는지 225 `check_draft` 로 다시 본다. 모든 보고서가 문제 0 이어야 100% 다.
- 적절성(사람): 행마다 (위험 항목·문단, 인용 조항, 조항 본문)를 보고 "그 조항이 그 위험 항목에 실제로 해당하는가"를 Y/N 으로 적는다.
  - 예: 2 m 이상 가장자리 작업에 제43조(개구부 방호) Y.
  - 예: 낙상 의심 이벤트 복기 문단에 화재 조항 N.
- 표본: TBM·DAILY 보고서에서 나온 인용 행 전체다. 보고서 20건 이상, 인용 행 100개 이상을 권한다(초안).

## 평가자 운영안

- 평가자는 구현자가 아닌 사람이다(15장). **수와 누가 할지는 사용자가 정한다**(결정 항목). 이 문서는 지어내지 않는다.
- 채점표는 평가자마다 순서를 섞는다. 평가자 표시는 이름이 아닌 기호(A, B …)로 둔다.
- 채점표에는 모델 이름·프롬프트·지연·토큰·표본 ID 를 싣지 않는다(시험이 확인한다). 가린 번호 ↔ 표본 대응(`key_map.json`)은 진행자만 갖는다.
- 평가자가 두 명 이상이면 쌍마다 단순 일치율과 Cohen κ(이진: 일치/아님)를 낸다.
  - 일치도는 판정 기준이 아니라 기준표 품질 지표다. κ 가 낮으면(예: 0.6 미만) 기준표를 고치고 다시 채점할지 정한다(초안).
- 판정이 갈린 표본은 평가자들이 함께 다시 보고 조정표(adjudication CSV, 같은 칸)에 한 값을 적는다. 조정이 없으면 불일치로 센다.
- 평가자가 한 명이면 일치도는 없고, 그 판정이 최종이다.

## 실행 순서

| 단계 | 어디서 | 명령 | 선행 |
|---|---|---|---|
| 0 | 교실 | 15.3 위험 시나리오 녹화(RTSP, 동의), manifest 커밋 | 녹화 일정 |
| 1 | 노트북 | 파일 모드 Tier 0 재생 → `risk_event.jsonl`·`world_state.jsonl` → 정답 요약 `truth.jsonl` 작성 → `g7_samples.py --replay … --session … --truth … --out <묶음>` | 보정 버전 |
| 2 | GPU 세션 | 묶음 tar 업로드 → `g7_answers.py --bundle … --out … --pose-model … --prompt <25 프롬프트>` → 답 폴더 가져오기 | **25 프롬프트·JSON 스키마**, GPU 세션 큐 항목 |
| 3 | 노트북 | `g7_score.py sheet --raters A,B` → CSV·HTML 를 평가자에게(흐린 그림 포함, 저장소 밖 공유) | 평가자 |
| 4 | 평가자 | CSV 채점, 갈린 표본 조정 | 기준표 확정 |
| 5 | 노트북 | `g7_score.py aggregate --latency-summary <v4 summary.json>` → `g7_result.json`·`g7_summary.md`, 인용은 `cite-sheet`/`cite-aggregate` | 225 결과 묶음(TBM·DAILY job 입력·결과 JSONL) |

리허설: 0 없이 시나리오 골든 재생 출력(`golden.py --out`)으로 1을 `--source-kind SIM` 으로 돌린다. 2를 `--prompt placeholder` 로 돌리면 도구가 끝까지 도는지 볼 수 있다(판정 안 함).

### GPU 세션 큐 항목안 (g7)

| 항목 | 내용 | 예상 시간(추정) |
|---|---|---|
| g7 | 통합 이미지, vLLM 감시 프로세스로 띄움(LIVE 관제 아님). 묶음 업로드 → `g7_answers.py`(동시 1, 표본 100) → `answers.jsonl`·`blurred/`·`run.json` 회수 → 같은 Pod 에서 v4(`vlm_int.py`) 결과가 있으면 함께 회수 | vLLM 기동 약 70 s(3차 실측) + 표본당 약 2~4 s(그림 수 늘면 증가) × 100 ≈ 5~7분 + 업로드·회수. 가중치 받기(31.5GB) 시간 제외 약 15분 |

- 위 숫자는 3차 세션 p95 2.1 s(그림 1장)에서 미루어 본 추정이다. 표본당 그림이 4대 × 2장이면 더 걸릴 수 있다. 실제는 `run.json`·answers 지연으로 남긴다.

## D-G7-01~04 자동 시험 위치

| ID | 내용 | 지금 있는 시험 | 25·273 등에서 생길 시험 |
|---|---|---|---|
| D-G7-01 | VLM 의 직접 심각도 변경·하향 0건 | 계약 `vlm_rationale.schema.json` `severity_upward_only` + 위반 예시 `contracts/examples/invalid/job_callback.vlm_downgrade.json`·`vlm_resolve.json`(`validate_examples.py`). 운영자 상향만 바닥값: `ai/tests/risk/test_operator_escalate.py`, `ai/tests/services/tier0/test_tier0_operator_escalate.py` | 25 처리기: 결과 severity = 입력값, proposed_severity 상향만(273 검증) |
| D-G7-02 | 빠른 경로가 사후 20초 클립을 기다림 0건 | 없음(계약 예시 `invalid/job_request.incident_summary_vlm_fast.json` 은 incident_summary 의 우선순위 위반만 본다) | 25 처리기·서버 등록(229 작업 대여)이 event_frame_ready 프레임만으로 작업을 만드는지 IT |
| D-G7-03 | CRITICAL 밖 또는 병합 이벤트당 2회 이상 실행 0건 | 없음(계약 예시 `invalid/job_request.vlm_low_priority.json` 은 이벤트 근거를 낮은 우선순위로 요청한 위반만 본다) | 서버 등록 IT(CRITICAL·병합 이벤트당 1회) |
| D-G7-04 | 외부 LLM 요청에 영상·이미지·얼굴·실명 0건 | `ai/tests/report/test_tbm_report.py::test_request_is_text_only_without_addresses_or_people`(`assert_text_only`), VLM 쪽 입력 관문 `ai/tests/vlm/test_client.py`(VlmFrame 아닌 이미지·외부 URL 거부, VLM 은 같은 Pod 루프백) | 271·운영자 질의응답(확장)이 생기면 같은 검사 |

계약 예시의 위반 내용은 각 파일의 `_expect_error` 로 확인했다.

## 25 에 넘길 요구

- 프롬프트는 `g7_answers.Prompt` 꼴로 낸다. 필요한 것은 `prompt_id: str`, `placeholder: False`, `build(input) -> PromptParts(text, system, response_format)`.
  - `simai_ai` 안 모듈(예: `simai_ai.vlm.rationale:PROMPT`)에 두면 `--prompt simai_ai.vlm.rationale:PROMPT` 로 바로 쓴다.
  - 처리기와 평가가 같은 객체를 써야 G7 이 제품 프롬프트를 평가한다.
- `build` 입력은 vlm_rationale input 에서 `frames` 를 뺀 값에 `confidence_reasons` 를 더한 것이다. 구조화 상태 정리(15.1 대응 "입력 구조화")는 이 함수 안에서 한다.
- JSON 스키마(`response_format` json_schema)는 결과의 `rationale`·`proposed_severity` 를 담는다. 답 생성기는 응답 원문(`content`)만 남긴다.
  - 채점표에 무엇을 보일지(`rationale` 문장만 또는 원문 전체)는 25 의 파서가 나오면 `export_rationale_sheets` 에서 정한다.
  - 지금은 원문 전체를 보인다.
- `finish_reason == "length"`(잘린 JSON)를 처리기가 어떻게 다루는지 정해 주면, 답 생성기도 같은 규칙으로 표본을 제외한다.

## 남은 일·잔여 위험

- 녹화 세션이 아직 없다. 표본 100건의 칸 채우기는 촬영 결과에 달렸다(특히 사람 2+ 장면의 R4).
- 정답 요약은 사람이 쓴다. 작성자 기준이 흔들리면 채점이 흔들린다. 요약 지침 한 쪽을 평가 전에 정한다.
- 채점표 HTML 은 흐린 그림을 로컬 파일 주소로 건다. 평가자에게 넘길 때 폴더째(그림 포함, 저장소 밖) 넘긴다.
- 프레임 고르기·디코딩은 E-GATE-1 의 `experiments/hmr/egate1_frames.py` 와 비슷한 코드를 따로 두었다(실험끼리 import 하지 않음). 둘 다 채택되면 `simai_ai.io` 로 합친다.
