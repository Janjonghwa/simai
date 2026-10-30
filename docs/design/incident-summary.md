# 분석 워커 `incident_summary` 처리기 (사고·아차사고 구간 요약)

S15P31A101-271. 명세 5.4 용도 2(사고·아차사고 구간 요약), 12.5(I5 `incident_summary`), FR-88.
계약은 `contracts/messages/incident_summary.schema.json`(`job_request` 3.4.0, `job_callback` 2.4.0).
코드는 `ai/src/simai_ai/vlm/incident_summary.py`(클립 프레임 표본 추출·프롬프트·응답 파싱 및 구간 검증),
`ai/services/worker/incident_summary.py`(`IncidentSummaryHandler`), `ai/services/worker/__main__.py`(`build_handlers`).

## 흐름

```
job_request (incident_summary, priority REPORT)
  → 입력 스키마(incident_summary.schema.json#/$defs/input) 및 이벤트 구간(window_start_ms ≤ window_end_ms) 검사
  → 클립별 ObjectStore.get(clip_uri) 내려받기 (워커 전용 서명 API GET, -248)
  → 클립 디코딩(PyAV H.264 Annex B / MP4 → BGR uint8 배열)
  → 클립별 촬영 시각 등간격 보간(clip_start_capture_ms ~ clip_end_capture_ms) + 이벤트 구간 안 필터 + 장수 상한(16장) 균등 표본 추출
  → prepare_vlm_frames(face_policy: BLURRED | EXCLUDED, YOLO26x-pose) 얼굴 흐림 관문 (-270)
  → VlmClient.complete(response_format json_schema strict, 최대 2번: 첫 호출 + 형식/구간 오류 시 1회 재시도) (-22)
  → 근거 시각(evidence_timestamps_ms) 이벤트 구간([window_start_ms, window_end_ms]) 필터·오름차순 정렬
  → 결과 스키마(incident_summary.schema.json#/$defs/result) 확인 → job_callback SUCCEEDED
```

## 입력과 출력

### 입력 (`incident_summary.schema.json#/$defs/input`)

| 필드 | 필수 | 설명 |
|---|---|---|
| `event_id` | 예 | 요약 대상 이벤트 ID (`uuid_v7`) |
| `event_sequence` | 예 | 요약 대상 이벤트 시퀀스 (`integer >= 1`) |
| `window_start_ms` | 선택 | 이벤트 요약 구간 시작 시각(UTC ms). 생략 시 `clips` 의 최소 `clip_start_capture_ms` |
| `window_end_ms` | 선택 | 이벤트 요약 구간 끝 시각(UTC ms, `>= window_start_ms`). 생략 시 `clips` 의 최대 `clip_end_capture_ms` |
| `anchor_capture_ms` | 선택 | 클립을 자르게 한 전이의 기준 촬영 시각(`media_clip.anchor_capture_ms`) |
| `clips[]` | 예 (`minItems: 1`) | `clip_id`, `camera_id`, `object_key`, `clip_uri`(`s3://...`, 워커 객체 서명 조회용), `clip_start_capture_ms`, `clip_end_capture_ms`, 선택 `frame_count`·`container` |
| `face_policy` | 예 | `BLURRED` 또는 `EXCLUDED`. 이벤트 클립은 흐림 없이 보관되므로(11.5) 워커가 VLM 호출 직전에 처리한다 |
| `structured_state` | 예 | 이벤트 구조화 상태(규칙·구역·전이 요약·트랙 위치). 위치·좌표의 정본은 Tier 0 값이다 |

### 출력 (`incident_summary.schema.json#/$defs/result`)

| 필드 | 필수 | 설명 |
|---|---|---|
| `event_id` | 예 | 입력 `event_id` 그대로 |
| `event_sequence` | 예 | 입력 `event_sequence` 그대로 |
| `summary` | 예 | 한국어 구간 요약문(1~400자) |
| `evidence_timestamps_ms` | 예 | 요약 근거 촬영 시각 목록(UTC ms, 1개 이상, 중복 없음, 오름차순, 모두 `[window_start_ms, window_end_ms]` 안) |
| `window_start_ms` | 예 | 적용된 이벤트 요약 구간 시작 시각(UTC ms) |
| `window_end_ms` | 예 | 적용된 이벤트 요약 구간 끝 시각(UTC ms) |
| `frames_used` | 예 | 얼굴 흐림 관문을 통과해 VLM 에 실제로 넣은 프레임 장수(`>= 1`) |
| `model_version` | 예 | vLLM 응답 모델 이름 |
| `latency_ms` | 선택 | 처리기 전체 소요 시간(ms) |

결과 객체는 `additionalProperties: false` 로 닫혀 있어 `severity` 나 `proposed_severity` 를 절대 실을 수 없다(명세 5.4 용도 2: 사고 구간 요약은 등급·상향 의견을 내지 않는다).

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 클립 주소 전달 | `clips[]` 에 `object_key` 와 함께 `clip_uri`(`s3://<버킷>/<키>`)·`clip_start_capture_ms`·`clip_end_capture_ms` 필수 (`job_request` 3.4.0) | 워커 객체 서명 API(`JobObjectSignService`, -248)는 `input` 안에 똑같은 문자열로 적힌 `s3://` 주소만 GET 서명을 내준다. 또한 재인코딩 없는 H.264 Annex B 클립(`h264-annexb`)에는 UTC epoch 시각이 들어 있지 않으므로 `media_clip` 의 시작·끝 촬영 시각으로 프레임별 촬영 시각을 계산해야 한다(시각을 지어내지 않음, AGENTS 규칙 2) |
| 프레임 촬영 시각 보간 | 클립 안 $N$장의 $i$번째 프레임을 `round(start + (end - start) * i / (N - 1))` ($N=1$ 이면 `start`)로 보간 | Tier 0 링 버퍼가 고정 주기 카메라 스트림에서 잘라낸 구간이므로 클립 양 끝 촬영 시각 사이 등간격 보간으로 촬영 시각축을 유지한다 |
| VLM 입력 장수 상한 | `DEFAULT_MAX_FRAMES = 16` (이벤트 구간 안 프레임을 `(capture_time_ms, camera_id)` 순으로 정렬한 뒤 양 끝 포함 균등 표본 추출) | `vlm-face-blur.md` 에서 "표본 간격은 1초 1장 기본안, VLM 입력 장수 한도에 맞춰 271 에서 정한다"로 넘긴 항목. 20초 클립 기준 약 1.3초 간격으로 구간 전체를 덮으면서, YOLO pose 검출·얼굴 흐림 시간과 vLLM(`max_model_len=32768`, `SIMAI_VLM_TIMEOUT_S=30`) 이미지 토큰 수를 예산 안에 묶는다 |
| 요약문 길이와 출력 스키마 | `SUMMARY_MAX_CHARS = 400`, `MAX_EVIDENCE_TIMESTAMPS = 16`, `response_format` `json_schema` (`strict: true`) | 2~4문장 구간 요약과 근거 시각 배열이 기본 `max_tokens=256` 안에서 잘리지 않고 닫히도록 제한한다. `finish_reason == "length"` 면 파싱되더라도 버린다 |
| 구간 밖 근거 시각 처리 | VLM 이 낸 `evidence_timestamps_ms` 중 `[window_start_ms, window_end_ms]` 밖 시각은 버리고 구간 안 시각만 오름차순·중복 제거로 남긴다. 구간 안 시각이 하나도 남지 않으면 `ValueError` 로 거부해 1회 재시도(`RETRY_NOTE`) 후 `VLM_ANSWER_INVALID`(재시도 불가)로 끝낸다 | 지시서 완료 조건("근거 시각은 모두 이벤트 구간 안이어야 한다 — 밖이면 버리거나 오류 코드"). 일부 시각만 어긋난 경우 유효한 근거를 살리고, 전부 구간 밖이면 잘못된 응답으로 실패 처리한다 |
| 작업 시간 예산 | `JOB_BUDGET_MS = 120_000` (120초) | `incident_summary` 는 `REPORT` 우선순위 사후 요약 작업(`job_request.incident_summary.json` `timeout_ms: 120000`). 첫 호출 뒤 `elapsed_ms + r.latency_ms <= 120_000` 일 때만 형식 오류 재시도를 1회 허용한다 |
| 진입점 등록 | `vlm_rationale` 과 같은 조건(`store`, `VlmSettings`, `SIMAI_TIER0_WEIGHTS`)이 갖춰졌을 때 같은 `PoseDetectorProvider` 인스턴스를 공유해 `incident_summary` 를 함께 등록 | YOLO26x-pose 가중치를 중복 로드하지 않아 분석 워커 GPU 메모리를 아낀다 |

## 실패 코드 (`-25` 규칙 준수)

| 코드 | 언제 | `retryable` |
|---|---|---|
| `VLM_INPUT_INVALID` | 계약 스키마 불일치, `clip_end_capture_ms < clip_start_capture_ms`, `window_end_ms < window_start_ms` | `False` |
| `CLIP_FETCH_FAILED` | 클립 서명·내려받기 실패 (`ObjectStoreError`) | `ObjectStoreError.retryable` 그대로 |
| `NO_FACE_SAFE_FRAMES` | 모든 클립 디코딩 실패, 이벤트 구간 안 프레임 없음, 또는 모든 표본 프레임이 얼굴 검출 실패(`detect`)·이미지 형식 오류(`decode`)로 빠짐 | 검출 실패(`detect`)가 끼어 있으면 `True`, 디코드 실패뿐이면 `False` |
| `VLM_ANSWER_INVALID` | 두 번 모두 JSON 스키마 불일치, `finish_reason == "length"`, 또는 이벤트 구간 안 근거 시각이 0개 | `False` |
| `VLM_TIMEOUT` / `VLM_UNAVAILABLE` | vLLM 응답 시간 초과·연결 실패·429·5xx | `True` |
| `VLM_REQUEST_REJECTED` / `VLM_CONFIG` | vLLM 4xx 거부 또는 환경 변수 설정 오류 | `False` |
| `CANCELLED` | 대여 만료·실행 시간 상한으로 러너가 취소 | (러너가 처리) |

## 9차 GPU 세션에서 확인할 것

- 실제 RunPod Pod 에서 Tier 0 가 올린 H.264 Annex B 이벤트 클립(`media_clip`)을 내려받아 `decode_clip_frames` → `prepare_vlm_frames` → vLLM(`cosmos3-nano`)까지 종단 지연 시간(`latency_ms`)과 GPU 메모리 사용량이 `WorkerGpuSettings` 상한 안에 들어오는지 확인한다.
- 16장 표본(`DEFAULT_MAX_FRAMES = 16`)에서 한국어 구간 요약 품질과 `evidence_timestamps_ms` 정확도를 확인하고, 필요 시 카메라 대수별 표본 장수를 조정한다.

## EC2 연결 (`S15P31A101-369`)

- EC2(`S15P31A101-369`)는 이벤트 클립(`media_clip`)이 모이거나 운영자가 구간 요약을 요청할 때 `job_request` 3.4.0 형식으로 `incident_summary` 작업을 등록한다:
  - `clips[]` 각 원소에 `media_clip` 의 `clip_id`, `camera_id`, `object_key`, `clip_uri`(`s3://<버킷>/<object_key>`), `clip_start_capture_ms`, `clip_end_capture_ms`, `frame_count`, `container` 를 채운다.
  - `job_callback` 2.4.0 `SUCCEEDED` 의 `result`(`summary`, `evidence_timestamps_ms`, `window_start_ms`, `window_end_ms`, `frames_used`, `model_version`, `latency_ms`)를 받아 이벤트 요약 기록으로 저장하고 관제 화면에 제공한다.
