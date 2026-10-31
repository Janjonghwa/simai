# 분석 워커 `incident_summary` 처리기 (사고·아차사고 구간 요약)

S15P31A101-271, S15P31A101-387. 명세 5.4 용도 2(사고·아차사고 구간 요약), 12.5(I5 `incident_summary`), FR-88.
계약은 `contracts/messages/incident_summary.schema.json`(`job_request` 3.4.0, `job_callback` 2.4.0).
코드는 `ai/src/simai_ai/vlm/incident_summary.py`(클립 프레임 표본 계획·추출·프롬프트·응답 파싱 및 구간 검증),
`ai/services/worker/incident_summary.py`(`IncidentSummaryHandler`, `count_clip_frames`, `decode_clip_frames`), `ai/services/worker/__main__.py`(`build_handlers`).

## 흐름

```
job_request (incident_summary, priority REPORT)
  → 입력 스키마(incident_summary.schema.json#/$defs/input) 및 이벤트 구간(window_start_ms ≤ window_end_ms) 검사
  → 클립별 ObjectStore.get(clip_uri) 내려받기 (워커 전용 서명 API GET, -248, 남은 시간 예산·취소 확인)
  → count_clip_frames: BGR 변환·VideoFrame 보관 없이 클립별 디코딩 가능 프레임 수만 집계 (매 프레임 취소·예산 확인, -387)
  → plan_clip_frames: 클립별 촬영 시각 등간격 보간(clip_start_capture_ms ~ clip_end_capture_ms) + 이벤트 구간 안 필터 + 장수 상한(16장) 균등 표본 인덱스 확정 (-387)
  → decode_clip_frames(frame_indices=...): 선택된 표본 프레임(최대 16장)만 BGR uint8 배열로 변환·보관하고 마지막 필요 인덱스에서 즉시 중단 (-387)
  → prepare_vlm_frames(face_policy: BLURRED | EXCLUDED, YOLO26x-pose) 얼굴 흐림 관문 (-270, -384)
  → VlmClient.complete(response_format json_schema strict, 최대 2번: 첫 호출 + 형식/구간 오류 시 1회 재시도) (-22, -384)
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
| 선택적 BGR 디코딩(`-387`) | `count_clip_frames` → `plan_clip_frames` → `decode_clip_frames(..., frame_indices=...)` 2단계로 최종 선택된 최대 16장만 BGR `np.ndarray` 로 변환 | 2304×1296·25fps·20초 클립 전체를 BGR 배열로 쥐면 클립 1대에 4.17 GiB(4대에 16.69 GiB)가 필요하지만, 프레임 수 집계 후 표본 인덱스만 BGR 변환하면 전체 클립 수와 무관하게 BGR 보관 상한을 최대 16장(136.7 MiB)으로 묶으면서 표본 동등성을 100% 유지한다 |
| 요약문 길이와 출력 스키마 | `SUMMARY_MAX_CHARS = 400`, `MAX_EVIDENCE_TIMESTAMPS = 16`, `response_format` `json_schema` (`strict: true`) | 2~4문장 구간 요약과 근거 시각 배열이 기본 `max_tokens=256` 안에서 잘리지 않고 닫히도록 제한한다. `finish_reason == "length"` 면 파싱되더라도 버린다 |
| 구간 밖 근거 시각 처리 | VLM 이 낸 `evidence_timestamps_ms` 중 `[window_start_ms, window_end_ms]` 밖 시각은 버리고 구간 안 시각만 오름차순·중복 제거로 남긴다. 구간 안 시각이 하나도 남지 않으면 `ValueError` 로 거부해 1회 재시도(`RETRY_NOTE`) 후 `VLM_ANSWER_INVALID`(재시도 불가)로 끝낸다 | 지시서 완료 조건("근거 시각은 모두 이벤트 구간 안이어야 한다 — 밖이면 버리거나 오류 코드"). 일부 시각만 어긋난 경우 유효한 근거를 살리고, 전부 구간 밖이면 잘못된 응답으로 실패 처리한다 |
| 작업 시간 예산 | `JOB_BUDGET_MS = 120_000` (120초) | `incident_summary` 는 `REPORT` 우선순위 사후 요약 작업(`job_request.incident_summary.json` `timeout_ms: 120000`). 클립 내려받기·디코딩·얼굴 처리·VLM 호출·재질문이 모두 남은 예산(`ctx.remaining_s()`) 안에서만 수행된다 |
| 진입점 등록 | `vlm_rationale` 과 같은 조건(`store`, `VlmSettings`, `SIMAI_TIER0_WEIGHTS`)이 갖춰졌을 때 같은 `PoseDetectorProvider` 인스턴스를 공유해 `incident_summary` 를 함께 등록 | YOLO26x-pose 가중치를 중복 로드하지 않아 분석 워커 GPU 메모리를 아낀다 |

## 실패 코드 (`-25` 규칙 준수)

| 코드 | 언제 | `retryable` |
|---|---|---|
| `VLM_INPUT_INVALID` | 계약 스키마 불일치, `clip_end_capture_ms < clip_start_capture_ms`, `window_end_ms < window_start_ms` | `False` |
| `CLIP_FETCH_FAILED` | 클립 서명·내려받기 실패 (`ObjectStoreError`) 또는 내려받기 시작 전 시간 예산 소진 | `ObjectStoreError.retryable` 그대로(예산 소진은 `True`) |
| `NO_FACE_SAFE_FRAMES` | 모든 클립 디코딩 실패, 이벤트 구간 안 프레임 없음, 디코딩·검출 도중 시간 예산 소진(`budget`), 또는 모든 표본 프레임이 얼굴 검출 실패(`detect`)·이미지 형식 오류(`decode`)로 빠짐 | 검출 실패(`detect`)나 시간 예산 소진(`budget`)이 끼어 있으면 `True`, 디코드 실패뿐이면 `False` |
| `VLM_ANSWER_INVALID` | 두 번 모두 JSON 스키마 불일치, `finish_reason == "length"`, 또는 이벤트 구간 안 근거 시각이 0개 | `False` |
| `VLM_TIMEOUT` / `VLM_UNAVAILABLE` | vLLM 응답 시간 초과·연결 실패·429·5xx | `True` |
| `VLM_REQUEST_REJECTED` / `VLM_CONFIG` | vLLM 4xx 거부 또는 환경 변수 설정 오류 | `False` |
| `CANCELLED` | 대여 만료·실행 시간 상한으로 러너가 취소 | (러너가 처리) |

## S15P31A101-387 — 전체 BGR 프레임 보관 제거와 디코딩 중 취소·시간 예산 전파

관련 명세: 3.2(분석 워커 자원·시간 제한), 5.4 용도 2(사고·아차사고 구간 요약, 얼굴 처리 fail-closed), 12.5(I5 `incident_summary` `timeout_ms=120000`). 계약 변경 없음.

### 1. 배경과 최대 메모리 수치

기존 `decode_clip_frames` 와 `IncidentSummaryHandler` 는 클립마다 모든 비디오 프레임을 `frame.to_ndarray(format="bgr24")` 로 변환해 `list[np.ndarray]` 로 만들고, 모든 카메라 클립의 BGR 프레임 목록(`clips_with_images`)을 동시에 메모리에 쥔 뒤에야 `sample_clip_frames` 로 16장을 골랐다.

교실 고정 카메라 원본 해상도($2304 \times 1296$, BGR24 3바이트 = 프레임당 $8{,}957{,}952\text{ B} \approx 8.54\text{ MiB}$), 25 fps, 20초 이벤트 클립(클립당 500장) 기준 메모리 사용량은 다음과 같다.

| 구분 | 보관 대상 | 클립 1대 (500장) | 카메라 4대 (2,000장) |
|---|---|---|---|
| **개선 전** (`clips_with_images` 전체 BGR 보관) | 모든 디코딩 프레임 BGR `np.ndarray` | $4{,}478{,}976{,}000\text{ B}$ (**4.17 GiB** / 4,271.5 MiB) | $17{,}915{,}904{,}000\text{ B}$ (**16.69 GiB** / 17,086.0 MiB) |
| **개선 후** (`count_clip_frames` + `plan_clip_frames` + 선택적 `decode_clip_frames`) | 1차 프레임 수 집계 중 단일 YUV420p `VideoFrame` ($\approx 4.27\text{ MiB}$) + 2차 선택 표본 최대 `DEFAULT_MAX_FRAMES = 16`장 BGR `np.ndarray` | 최대 16장 BGR = $143{,}327{,}232\text{ B}$ (**0.133 GiB** / 136.7 MiB) | 최대 16장 BGR = $143{,}327{,}232\text{ B}$ (**0.133 GiB** / 136.7 MiB) |
| **절감률** | 원시 BGR 배열 피크 메모리 | **96.8% 감소** ($4.17\text{ GiB} \rightarrow 0.133\text{ GiB}$) | **99.2% 감소** ($16.69\text{ GiB} \rightarrow 0.133\text{ GiB}$) |

또한 `prepare_vlm_frames` 얼굴 흐림 관문을 통과해 읽기 전용 `VlmFrame` 이 만들어진 직후 원본 표본 참조(`sampled`)를 즉시 해제하므로, VLM HTTP 호출 동안에는 관문을 통과한 `VlmFrame`(최대 16장)만 메모리에 남는다.

### 2. 표본 동등성(`plan_clip_frames` ↔ `sample_clip_frames`)

1. `simai_ai.vlm.incident_summary.plan_clip_frames(clips_with_counts, *, window_start_ms, window_end_ms, max_frames)` 는 클립별 실제 디코딩 프레임 수 $N_c$ 만 받아:
   - `assign_clip_timestamps(clip_start_capture_ms, clip_end_capture_ms, N_c)` 로 프레임별 촬영 시각 보간,
   - `window_start_ms <= ts <= window_end_ms` 구간 필터,
   - `(capture_time_ms, camera_id, clip_id, frame_index, clip_order)` 정렬,
   - `uniform_indices(len(candidates), max_frames)` 등간격 인덱스 추출
   을 수행해 `list[PlannedFrame]` 을 돌려준다.
2. 기존 순수 함수 `sample_clip_frames(clips_with_images, ...)` 도 내부에서 똑같이 `plan_clip_frames` 를 호출해 인덱스를 고른 뒤 이미지만 매핑하도록 단일화했다.
3. 따라서 `count_clip_frames` 로 클립별 $N_c$ 를 세고 `plan_clip_frames` 가 지정한 `frame_indices` 만 `decode_clip_frames(raw, frame_indices=...)` 로 디코딩한 결과는, 단일 클립·다중 클립·구간 일부 겹침·구간 밖 클립 포함 등 모든 경우에서 기존 전체 디코딩 뒤 `sample_clip_frames` 를 호출한 결과와 `source_id`·`camera_id`·`frame_index`·`capture_time_ms`·BGR 픽셀 값이 **100% 동일(표본 동등성)**하다.
4. 이벤트 구간(`[window_start_ms, window_end_ms]`)과 겹치는 프레임이 하나도 없는 클립은 1차 집계 직후 압축 바이트(`raw`)를 즉시 버리고 2차 디코딩을 아예 실행하지 않는다. 선택된 프레임이 있는 클립도 마지막 선택 인덱스(`max(frame_indices)`)까지만 디코딩하고 즉시 루프를 종료(`break`)하며, 해당 클립의 2차 디코딩이 끝나는 즉시 압축 바이트(`raw`)도 해제한다.

### 3. 디코딩 중 취소·남은 시간 예산 확인

- `count_clip_frames` 와 `decode_clip_frames` 는 컨테이너를 열기 전과 `container.decode(video=0)` 의 **매 프레임 디코딩 전·BGR 변환 전**에 `_guard_decode_budget(cancelled, remaining_s)` 를 호출한다.
- 취소(`ctx.cancelled.is_set()`) 시 `ClipDecodeCancelled` 를 던져 포맷 폴백(`fmt=None`)으로 삼키지 않고 즉시 탈출하며, 처리기는 `JobFailed("CANCELLED")` 로 종료한다(얼굴 검출기·VLM 호출 0회).
- 남은 시간 예산이 0 이하(`ctx.remaining_s() <= 0`)가 되면 `ClipDecodeBudgetExceeded` 로 즉시 디코딩을 중단하고 `decode_drops` 에 `<clip_id>: budget` 을 기록해 재시도 가능한 `JobFailed("NO_FACE_SAFE_FRAMES", ..., retryable=True)` 로 종료한다(`-384` 의 관문 예산 소진 처리와 동일).

### 4. 버린 대안

- **단일 패스로 클립마다 독립적으로 먼저 `max_frames` 장을 추린 뒤 합치는 방식:** 클립 A(구간 안 100장)와 클립 B(구간 안 100장)가 이어질 때, 합쳐진 200장에서 `uniform_indices(200, 16)` 이 뽑는 클립 A 인덱스(`round(2 * 199 / 15) = 27` 등)와 클립 A 단독 `uniform_indices(100, 16)` 인덱스(`round(4 * 99 / 15) = 26` 등)가 반올림 경계에서 달라져 다중 클립 표본 동등성이 깨진다.
- **PyAV `container.demux(video=0)` 패킷 수나 입력 JSON 의 선택 필드 `frame_count` 로 1차 집계를 대체하는 방식:** H.264 Annex B 스트림에서 SPS/PPS 단독 패킷이나 끝부분 손상 패킷이 있으면 `demux` 패킷 수와 실제 `decode(video=0)` 프레임 수가 어긋날 수 있고, 계약상 `clips[].frame_count` 는 선택 필드다. `count_clip_frames` 는 `decode(video=0)` 를 돌되 `to_ndarray(format="bgr24")`(`sws_scale` 색공간 변환 + 프레임당 8.96 MB 할당, 전체 디코딩 시간의 약 80%)를 생략하고 `VideoFrame` 을 즉시 해제해, 정확도와 메모리 상한을 동시에 지킨다.
- **1차 패스에서 `av.VideoFrame` 객체를 리스트에 쥐고 있는 방식:** YUV420p `VideoFrame` 평면 버퍼도 $2304 \times 1296$ 500장이면 클립당 약 $2.09\text{ GiB}$(4대면 $8.35\text{ GiB}$)를 점유하므로 근본 해결이 되지 않는다.

### 5. 코드와 시험 위치

| 위치 | 내용 |
|---|---|
| `ai/src/simai_ai/vlm/incident_summary.py` | `PlannedFrame`, `plan_clip_frames` 추가 및 `sample_clip_frames` 단일 계획 경로로 통합 |
| `ai/services/worker/incident_summary.py` | `ClipDecodeCancelled`, `ClipDecodeBudgetExceeded`, `count_clip_frames`, `decode_clip_frames(..., frame_indices, cancelled, remaining_s)`, `IncidentSummaryHandler` 선택적 디코딩·예산 전파 |
| `ai/tests/services/worker/test_incident_summary.py` | 다중 클립 H.264 Annex B 선택적 BGR 변환 수 검증 및 `sample_clip_frames` 표본·픽셀 동등성 시험, 디코딩 도중 취소·시간 예산 소진 즉시 중단 시험 |

## 9차 GPU 세션에서 확인할 것

- 실제 RunPod Pod 에서 Tier 0 가 올린 H.264 Annex B 이벤트 클립(`media_clip`)을 내려받아 `count_clip_frames` → `decode_clip_frames(frame_indices=...)` → `prepare_vlm_frames` → vLLM(`cosmos3-nano`)까지 종단 지연 시간(`latency_ms`), 워커 프로세스 최대 RSS, GPU 메모리 사용량이 `WorkerGpuSettings` 상한 안에 들어오는지 확인한다.
- 16장 표본(`DEFAULT_MAX_FRAMES = 16`)에서 한국어 구간 요약 품질과 `evidence_timestamps_ms` 정확도를 확인하고, 필요 시 카메라 대수별 표본 장수를 조정한다.

## EC2 연결 (`S15P31A101-369`)

- EC2(`S15P31A101-369`)는 이벤트 클립(`media_clip`)이 모이거나 운영자가 구간 요약을 요청할 때 `job_request` 3.4.0 형식으로 `incident_summary` 작업을 등록한다:
  - `clips[]` 각 원소에 `media_clip` 의 `clip_id`, `camera_id`, `object_key`, `clip_uri`(`s3://<버킷>/<object_key>`), `clip_start_capture_ms`, `clip_end_capture_ms`, `frame_count`, `container` 를 채운다.
  ## EC2 서버 흐름·API (`S15P31A101-369`)

명세 5.2(비용 제한·클립 생성 뒤 사후 요청), 5.6(사고 구간 사후 요약), 12.5(I5 `incident_summary`), 12.6(`POST /api/events/{eventId}/summary`, `GET /api/events/{eventId}`).
코드는 `server/src/main/resources/db/migration/V20__incident_summary.sql`, `IncidentSummaryService.java`, `EventController.java`, `EventQueryRepository.java`, `JobLeaseService.java`(`JobStateChanged`).

### 1. 서버 흐름

```
POST /api/events/{eventId}/summary (ROLE_ADMIN, 본문 없음)
  → risk_event 행 잠금(FOR UPDATE): 없으면 404 EVENT_NOT_FOUND, source != 'LIVE' 면 409 EVENT_NOT_LIVE
  → 진행 중인 요약(incident_summary.status IN ('QUEUED', 'RUNNING')) 확인: 있으면 새 작업을 만들지 않고 200 OK 로 기존 항목 반환
  → 삭제되지 않은 클립(media_clip.deleted_at IS NULL) 조회: 0개면 409 CLIP_NOT_READY
  → 클립 구간(window_start_ms = min(clip_start_capture_at), window_end_ms = max(clip_end_capture_at), anchor_capture_ms) + face_policy = "BLURRED" + structured_state(rule_id, severity, zone_id, metrics, transitions, tracks, zone) 조립
  → AnalysisJobService.enqueue(jobType = "incident_summary", priority = REPORT, maxAttempts = 2, timeoutMs = 120_000) → job_request 3.4.0 스키마 검사 통과
  → incident_summary 정본 행 INSERT (status = 'QUEUED') + RiskEventsDelivered 발행(STOMP /topic/events) → 202 Accepted

워커 대여·반납·재시도 (JobStateChanged)
  → tryLease(LEASED): incident_summary.status 를 RUNNING 으로 바꾸고 RiskEventsDelivered 발행
  → unlease / consume_attempt=false 반납 / retryable 실패 재큐잉(QUEUED): incident_summary.status 를 QUEUED 로 되돌리고 RiskEventsDelivered 발행

워커 콜백·만료 (JobFinished)
  → SUCCEEDED: input 과 result 의 event_id·event_sequence·window_start_ms·window_end_ms 일치, evidence_timestamps_ms 모두 구간 안, severity·proposed_severity 미포함, LIVE 이벤트 확인(위반 시 400 INVALID_WORKER_MESSAGE 로 트랜잭션 롤백)
    → incident_summary 행을 SUCCEEDED 로 갱신(summary, evidence_timestamps_ms, frames_used, model_version, latency_ms, finished_at) + RiskEventsDelivered 발행
    → risk_event 의 severity·proposed_severity·rationale 은 절대 바꾸지 않는다(AGENTS 규칙 4)
  → FAILED / TIMED_OUT / CANCELLED: incident_summary 행을 FAILED 또는 TIMED_OUT 으로 갱신(error_code, finished_at) + RiskEventsDelivered 발행
  → 완료(SUCCEEDED)·실패(FAILED)·타임아웃(TIMED_OUT) 뒤 재요청하면 기존 행을 덮어쓰지 않고 새 job_id·summary_id 행을 추가한다(AGENTS 규칙 6 추가 전용)
```

### 2. API·투영 형식 (`-368` 관제 화면과 공유)

- `POST /api/events/{eventId}/summary` (관리자 전용, 본문 없음)
  - 신규 생성 시 `202 Accepted`, 진행 중(`QUEUED`·`RUNNING`) 중복 요청 시 `200 OK` 로 아래 요약 항목 JSON 을 반환한다.
  - 오류: `404 EVENT_NOT_FOUND`, `409 EVENT_NOT_LIVE`, `409 CLIP_NOT_READY`, `401`/`403`.
- `GET /api/events/{eventId}`, `GET /api/events`, STOMP `/topic/events` 이벤트 객체 안 `summaries` 배열(최신순 `requested_at DESC, summary_id DESC`):

```json
"summaries": [
  {
    "summary_id": 1,
    "job_id": "job-0199df4d-0002-7000-8000-000000000002",
    "status": "QUEUED | RUNNING | SUCCEEDED | FAILED | TIMED_OUT",
    "requested_at": 1760342400400,
    "finished_at": 1760342412050,
    "summary": "비계 2단 작업 중 안전대 미체결 상태로 몸을 내밀어 추락 위험이 발생한 뒤 복귀했습니다.",
    "evidence_timestamps_ms": [1760342395000, 1760342400100],
    "window_start_ms": 1760342380000,
    "window_end_ms": 1760342420000,
    "error_code": null
  }
]
```

### 3. 9차 GPU 세션 검증 방법 (EC2 ↔ 워커 종단 확인)

- 관리자 토큰으로 클립이 올라온 `LIVE` 이벤트에 `POST /api/events/{eventId}/summary` 를 호출해 `202 Accepted`(`status: "QUEUED"`)를 확인한다.
- GPU 워커가 대여(`/api/worker/jobs/lease`) → 클립 GET 서명(`/api/worker/jobs/{jobId}/objects/sign`) → VLM 요약 → 콜백(`/api/worker/jobs/{jobId}/result`)을 마친 뒤 `GET /api/events/{eventId}` 와 PostgreSQL 정본을 대조한다:
  ```sql
  SELECT summary_id, event_id, event_sequence, job_id, status, requested_at, finished_at,
         window_start_ms, window_end_ms, frames_used, model_version, latency_ms, error_code,
         evidence_timestamps_ms, summary
  FROM incident_summary
  WHERE event_id = '<event_id>'
  ORDER BY requested_at DESC, summary_id DESC;
  ```
- STOMP `/topic/events` 구독 화면에서 `QUEUED` → `RUNNING` → `SUCCEEDED` 상태 변화와 근거 시각(`evidence_timestamps_ms`) 표시, 이벤트 `severity` 불변을 함께 확인한다.
