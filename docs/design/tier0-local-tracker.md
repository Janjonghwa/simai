# Tier 0 카메라별 Local Tracker 설계

S15P31A101-23. 기획서 §5.1(추적, 출력 `observation_2d`), §5.2(추적 3계층의 Local Tracker, 수정 7), 요구사항 FR-57 기준이다.
코드는 `ai/src/simai_ai/tracking/` 의 `local_config.py`, `local_tracker.py`, `ultralytics_local.py` 이고, 시험은 `ai/tests/tracking/` 에 있다.

## 범위

YOLO26x-pose 묶음 추론 결과를 카메라별 독립 추적기에 넣어 카메라 내 트랙 ID 를 붙이고 `observation_2d` 를 낸다.
카메라 간 연결(S15P31A101-34)과 World Track(S15P31A101-37)은 범위 밖이며, 출력의 `world_id` 는 null 이다.

| 항목 | 형식 |
|---|---|
| 입력 | `CameraDetections(camera_id, stream_epoch, frame_sequence, estimated_capture_time_ms, timestamp_source, timestamp_uncertainty_ms, detections)`. 검출은 `PoseDetection(bbox, score, keypoints_2d, reid_feature)`. I0 헤더에서 `CameraDetections.from_header()`, YOLO `Results` 에서 `pose_detections_from_result()` 로 만든다 |
| 출력 | `Observation2DFrame` → `to_contract()` 가 `observation_batch` 의 `frames[]` 항목, `observation_batch_payload()` 가 payload |
| 상태 | `CameraTrackerState`(카메라별 처리 프레임 수, 마지막 촬영 시각·프레임 식별자, 추적 중·잃은 트랙 ID, 발급 ID 수, 재생성·버린 프레임·미연결 검출 수) |

## 추적기 설정

| 종류 | 근거 | 설정 |
|---|---|---|
| TrackTrack (기본) | §5.2 수정 7 | ultralytics 8.4.178 `tracktrack.yaml` 값, `gmc_method: none` |
| ByteTrack | 단순 기준 | `bytetrack.yaml` 값 |
| BoT-SORT | 가림·재등장 개선용, ReID 선택 | `botsort.yaml` 값, `gmc_method: none` |

- 모든 키를 명시한다(`DEFAULT_PARAMS`). 빠지거나 모르는 키는 오류다. Ultralytics 추적기가 빠진 키를 자기 기본값으로 채우는 것을 막으려는 것이고, 설치된 YAML 과 키 목록이 같은지 시험이 본다.
- 고정 카메라라 카메라 움직임 보정은 끈다. `gmc_method` 는 `none` 만 받는다.
- ReID 는 검출기 특징(`model: auto`, 검출마다 `reid_feature`)만 받는다. 외부 ReID 모델은 영상 crop 이 필요해 막았다(A2 에서 다시 본다). `with_reid` 인데 특징이 없는 검출이 있으면 오류다(조용히 IoU 만 쓰지 않는다).
- `track_buffer` 는 프레임 수다. 교실 값 조정은 G2·A0 에서 한다.

## 카메라별 분리

| 대상 | 분리 방식 |
|---|---|
| 추적 상태 | 카메라마다 추적기 인스턴스 하나. 추론 모델만 공유한다 |
| ID 공간 | 카메라마다 발급 카운터, `{camera_id}-t{n}`. 추적기를 새로 만들어도 이어서 센다(ID 를 다시 쓰지 않음) |
| 시간 기록 | 카메라마다 마지막 (stream_epoch, frame_sequence)와 촬영 시각 |

- Ultralytics 추적기는 `update(results, img, feats)` 계약으로 쓰고 img 는 늘 None 이다. GMC 를 끄고 외부 ReID 를 막았으므로 영상이 필요 없다. 8.4.178 의 세 추적기는 트랙 ID 카운터가 인스턴스마다 따로다.
- 이미 처리한 프레임보다 식별자나 촬영 시각이 오래된 프레임은 추적기에 넣지 않고 센다(§5.1 프레임 최신성). 새 `stream_epoch` 는 그대로 이어 추적한다.
- 같은 카메라의 촬영 시각 간격이 `max_frame_gap_ms`(초기값 2000 ms)를 넘으면 그 카메라의 추적기를 새로 만든다. 프레임 수 기준인 `track_buffer` 가 긴 공백 뒤 엉뚱하게 이어 붙이지 않게 하려는 것이다. 잘못 합친 ID 보다 갈라진 ID 가 덜 위험하다(§5.2).

## 출력 규칙

- bbox·score·keypoint 는 검출 원본이다. 추적기 칼만 상태 박스는 내지 않는다(관측과 추정 구분).
- 이번 프레임에 트랙이 붙지 않은 검출(확정 전 새 트랙, 저신뢰 미연결)은 `observation_2d` 에 넣지 않고 수만 센다. 계약상 `local_track_id` 가 필수라 지어내지 않는다.
- `timestamp_uncertainty_ms` 를 모르면 계약을 채울 수 없어 오류다(0 으로 채우지 않는다). 관절 배열 길이는 `joint_layout`(COCO17 17개 등)과 맞아야 한다.

## 시뮬레이터 측정 (SIM, 상한)

`cd ai && uv run --extra detection python tests/tracking/sim_tracking.py` 로 잰다. 시뮬레이터 2D keypoint 로 박스를 만들어 넣고, 시뮬레이터가 붙인 카메라 내 ID 는 정답 연결로만 쓴다. 네 카메라 프레임을 촬영 시각 순서로 합쳐 카메라마다 한 장씩 묶어 넣는다.
박스는 두 변형으로 만든다. amodal 은 가려진 관절(conf 0)까지 감싸는 박스, visible 은 보인 관절만 감싸 가림이 생기면 박스가 갑자기 줄어드는 박스다. 신뢰도는 보인 관절 신뢰도 평균이다.

**아래 값은 SIM 입력의 상한 성능이다(`input_provenance.is_upper_bound = true`). 제품 성능이나 추적기 선택 근거로 쓰지 않는다.** 교실 ID 전환·IDF1 은 G2·A0 녹화로 잰다(§5.2 수정 7).

| 시나리오 | 박스 | 추적기 | 검출 | 트랙 붙음 | ID 전환 | 뒤섞인 ID | 조각 |
|---|---|---|---|---|---|---|---|
| basic | amodal | tracktrack | 3000 | 3000 | 0 | 0 | 0 |
| basic | amodal | bytetrack | 3000 | 3000 | 0 | 0 | 0 |
| basic | amodal | botsort | 3000 | 3000 | 0 | 0 | 0 |
| basic | visible | tracktrack | 3000 | 2976 | 16 | 0 | 10 |
| basic | visible | bytetrack | 3000 | 2996 | 7 | 0 | 4 |
| basic | visible | botsort | 3000 | 2996 | 7 | 0 | 4 |
| podium | amodal | tracktrack | 2394 | 2394 | 0 | 0 | 0 |
| podium | amodal | bytetrack | 2394 | 2394 | 0 | 0 | 0 |
| podium | amodal | botsort | 2394 | 2394 | 0 | 0 | 0 |
| podium | visible | tracktrack | 2394 | 2367 | 16 | 0 | 10 |
| podium | visible | bytetrack | 2394 | 2386 | 7 | 0 | 7 |
| podium | visible | botsort | 2394 | 2389 | 4 | 0 | 4 |
| crossing | amodal | tracktrack | 1395 | 1395 | 0 | 0 | 0 |
| crossing | amodal | bytetrack | 1395 | 1395 | 0 | 0 | 0 |
| crossing | amodal | botsort | 1395 | 1395 | 0 | 0 | 0 |
| crossing | visible | tracktrack | 1395 | 1386 | 5 | 0 | 3 |
| crossing | visible | bytetrack | 1395 | 1392 | 5 | 0 | 3 |
| crossing | visible | botsort | 1395 | 1392 | 5 | 0 | 3 |

- ID 전환: 같은 정답 사람에게 붙은 트랙 ID 가 직전과 다른 횟수(MOT IDSW). 뒤섞인 ID: 서로 다른 정답 사람 둘 이상에게 붙은 트랙 ID 수. 조각: 정답 사람마다 받은 트랙 ID 수 − 1 의 합. 카메라 4대 합계다.
- visible 박스의 ID 전환은 가림으로 박스가 갑자기 줄어 IoU 가 떨어질 때 새 트랙이 생겼다가 원래 트랙으로 돌아오는 경우다(2~3 프레임 공백). 어느 경우에도 정답 사람 둘이 한 트랙 ID 로 섞이지 않았다.
- 시험(`test_local_tracker_sim.py`)은 세 시나리오 × 두 박스 × 세 추적기에서 네 카메라를 섞어 넣은 결과가 카메라 하나씩 따로 넣은 결과와 같은지(ID·박스·상태), ID 접두어·카메라 간 겹침, 카메라별 시간 기록, 뒤섞인 ID 0, ID 전환 허용치(amodal 0, visible 20 이하, 회귀용)를 본다.
