# VLM 입력 얼굴 처리 (S15P31A101-270, FR-89)

명세 5.4 "VLM 입력 프레임": VLM 에 넣는 프레임·클립은 얼굴 영역을 처리한 뒤 넣는다. 분석 워커가 VLM 작업 안에서 VLM 호출 직전에 하고,
Tier 0 실시간 경로에는 아무것도 더하지 않는다. 얼굴은 pose keypoint(눈·귀·코)로 잡고 별도 얼굴 모델은 쓰지 않는다. 안전모가 보이도록 머리 윗부분은 남긴다.

## 코드

| 위치 | 내용 |
|---|---|
| `ai/src/simai_ai/vlm/face_blur.py` | 얼굴 영역 계산, BLURRED·EXCLUDED 처리, 관문 `prepare_vlm_frames`, VLM 입력 타입 `VlmFrame`, `KeypointProvider`·`PoseDetectorProvider` (numpy 만) |
| `ai/services/worker/vlm_frames.py` | vlm_rationale `input.frames[]` 를 객체 저장소에서 받아 관문에 넣고, 남는 프레임이 없으면 `JobFailed` |
| `ai/tests/vlm/test_face_blur.py`, `ai/tests/services/worker/test_vlm_frames.py` | 합성 이미지 + 가짜 keypoint 시험 |

## 결정

- **keypoint 출처:** 워커가 받은 프레임에 pose 검출을 돌린다(`KeypointProvider`, 운영은 `PoseDetectorProvider` 로 YOLO26x-pose 검출기를 감싼다).
  검출기 출력 형식은 `calibration.extrinsics.extract.PoseDetector`·`services.tier0.yolo_detector` 와 같은 dict 다. 워커 프로세스 안에서 돌아
  GPU 메모리는 워커 상한(`gpu.reserve.cap_process_memory`)과 Tier 0 우선(작업 받기 전 `AdmissionGate`)을 그대로 따른다.
  EC2·Tier 0 가 keypoint 를 같이 넘기는 안은 계약 변경이 필요하고, 프레임마다 keypoint 를 맞출 근거(같은 시각의 2D 검출)가 계약에 없어 버렸다.
- **얼굴 영역**(머리 폭 추정 s = max(얼굴 점 범위, 사람 상자 폭 / 3, 12 px)):
  - 두 눈이 보이면 얼굴 점 중심 ± 0.75 s, 세로는 눈높이 − 0.30 s ~ 눈높이 + 0.95 s. 눈높이 0.30 s 위부터 남겨 이마 위·안전모가 보인다.
  - 눈이 하나라도 없으면 보이는 점 양 끝에서 ± 1.0 s(얼굴이 어느 쪽에 있는지 모름). 눈이 하나도 없으면 가장 위 점을 코로 보고 0.35 s 더 올린다.
  - 얼굴 점이 하나도 없으면(뒤돌아섬·점 놓침) 사람 상자 높이 5%~30% 띠(상자 폭 전체). 얼굴이 안 보이는지 못 잡았는지 구별할 수 없어 처리하는 쪽을 골랐다.
  - keypoint 신뢰도 0.2 미만은 버린다(버리면 더 넓은 기하 처리로 갈 뿐이라 낮게 둔다).
- **BLURRED 강도:** 영역 긴 변을 3칸으로 나눈 모자이크(칸마다 평균색). 30~50 px 얼굴에서 칸이 10~17 px 라 이목구비가 한 칸에 섞여 복원할 수 없다.
  모자이크 뒤 블러는 더하지 않았다(칸 경계만 부드럽게 할 뿐 정보량이 줄지 않는다).
- **EXCLUDED:** 같은 영역을 회색(128)으로 칠한다. `avatar_asset.face_policy` 의 EXCLUDED("얼굴 영역을 넣지 않는다")와 같은 뜻으로, 프레임 전체를 빼지 않는다
  (근거 설명에는 사람의 자세·위치·안전모가 필요하다).
- **fail-closed:** 디코드 실패·형식 오류, 검출 예외인 프레임은 VLM 에 넘기지 않는다(흐리지 않은 프레임으로 대신하지 않는다). 남는 프레임이 없으면
  `NO_FACE_SAFE_FRAMES` 로 작업 실패(검출 실패가 있으면 재시도 가능, 디코드 실패뿐이면 불가). 프레임 받기 실패는 `FRAME_FETCH_FAILED`(저장소 오류의 재시도 여부).
- **병렬 프레임 다운로드와 남은 시간 예산 전파(S15P31A101-384):**
  - `gate_input_frames` 는 `input.frames[]` 를 최대 `DEFAULT_FETCH_WORKERS = 4` 스레드로 병렬 다운로드(`store.get`)해 입력 순서를 그대로 유지한다. 다운로드 도중 취소(`ctx.cancelled`), 시간 예산 소진(`ctx.is_expired()`), 또는 프레임 오류가 나면 아직 시작하지 않은 다운로드를 즉시 건너뛴다.
  - `prepare_vlm_frames` 는 `cancelled` 와 `remaining_s` 콜백을 받아 각 프레임의 이미지 디코드 전과 `provider.people(image)` 호출 전에 확인한다. 취소되었거나 남은 시간이 `<= 0` 이면 그 이후 프레임을 `DroppedFrame(reason="cancelled" | "budget")` 으로 버리고 더 이상 검출기를 돌리지 않는다(얼굴 처리 전 원본 프레임이 VLM 에 넘어가는 일은 없다).
  - 모든 프레임이 `"budget"` 사유로 버려져 안전 프레임이 0장일 때는 재시도 가능한 `JobFailed("NO_FACE_SAFE_FRAMES", retryable=True)` 로 실패한다.
- **관문:** `VlmFrame` 은 `prepare_vlm_frames` 만 만들 수 있고(내부 토큰), 픽셀은 읽기 전용이다. VLM 클라이언트(S15P31A101-22·25)는 `VlmFrame` 만 받는다.
  처리한 이미지는 메모리에서만 쓰고 객체 저장소에 올리지 않는다. 저장된 원본은 그대로다(11.5).
- **face_policy:** 계약상 프레임마다 있으므로 프레임마다 그 값으로 처리한다.
- **클립(incident_summary):** 271 이 클립에서 프레임을 뽑아 BGR 배열로 같은 관문에 넣는다(배열 입력 시험함). 표본 간격은 1초 1장(20초 클립 × 카메라 4대 ≈ 80장)을
  기본안으로 두고, VLM 입력 장수 한도에 맞춰 271 에서 정한다.
- **Tier 0:** import-linter `tier0-no-worker-stack` 이 이미 Tier 0 경로의 `simai_ai.vlm` import 를 막고 있어 계약을 더하지 않았다.

## 잔여 위험

- pose 검출이 사람을 놓치면 그 사람의 얼굴은 처리되지 않는다(얼굴 모델을 쓰지 않는 명세 결정의 한계). 운영 검출기의 사람 신뢰도 기준을 낮게 두는 것이 대책이다.
- 비율은 정면·약간 내려다본 사람 기준 초기값이다. 실제 카메라(3 m 높이) 표본으로 얼굴이 영역 밖으로 나가는지 확인해 고친다.
- 얼굴 점이 없을 때의 상자 띠는 안전모 대부분을 함께 가릴 수 있다.

## 넘긴 일

- VLM 서빙(S15P31A101-22)·근거 설명 처리기(S15P31A101-25)가 `gate_input_frames` → `VlmFrame` 만 VLM 에 넣는다.
- incident_summary 처리기·클립 프레임 추출(S15P31A101-271).
- 실제 pose 가중치로 표본 프레임 확인(로컬에 가중치·시험용 이미지가 없어 하지 않음).
