# sim — 결정적 가상 교실 시뮬레이터

장비 없이 Tier 0 하류(카메라 간 연결, 시간 보간·삼각측량, 접지, Risk Engine)를 개발하고 같은 입력으로 반복 검증하려고 만든다(명세 5.1 입력 추상화, 21.3 시나리오 재생).
시드를 고정한 가상 교실에서 사람 궤적(관절 3D)과 가상 카메라를 만들고, 2D keypoint 단계부터 넣을 수 있는 파일을 낸다. 영상은 만들지 않는다.

- 표준 라이브러리만 쓴다(기본 설치로 돈다). `simai_ai` 는 `sim` 을 import 하지 않는다(`tests/sim/test_import_direction.py`).
- 같은 시나리오·시드면 출력이 바이트 단위로 같다(`tests/sim/test_determinism.py`).
- 이 출력으로 얻은 지표는 **입력이 완벽할 때의 상한**이다(`input_provenance.is_upper_bound = true`). 제품 성능으로 쓰지 않는다.

## 실행

`ai/` 에서 실행한다.

```sh
uv run python -m sim.run --list                                   # 내장 시나리오: basic, crossing, podium
uv run python -m sim.run --scenario basic --out sim/runs/basic    # 출력 디렉터리는 없거나 비어 있어야 한다
uv run python -m sim.run --scenario path/to/custom.json --out <dir> --seed 7
```

`sim/runs/` 는 `.gitignore` 의 `runs/` 패턴으로 커밋되지 않는다. 시나리오 정의(`sim/scenarios/*.json`)만 커밋한다.

## 내장 시나리오

| 이름 | 내용 | 확인하는 것 |
|---|---|---|
| `basic` | 9 × 7.5 m 교실, 모서리 카메라 4대(높이 2.7 m). 한 명은 교실을 한 바퀴 걷고 한 명은 가운데 서 있다 | 기본 경로, 카메라별 시각 오프셋·지터, 등 뒤 카메라의 얼굴 가림 |
| `podium` | 앞쪽에 높이 0.2 m 단상. 교사는 단상 위를 오가고 학생은 바닥 → 단상 → 바닥 | 지지 표면 높이(접지점 Z = 0.2, Z=0 으로 채우지 않음) |
| `crossing` | 두 사람이 0.6 m 떨어진 평행선을 마주 걸어 가운데에서 엇갈린다 | 사람끼리의 가림(conf 0), 시각 오차가 있는 카메라 |

## 출력

| 경로 | 형식 | 내용 |
|---|---|---|
| `calibration.json` | `contracts/providers/calibration.schema.json` | 가상 카메라 K·왜곡·R·t(OpenCV, World → 카메라). `provider_source = SIM`, `state = PROVISIONAL` |
| `observations/<camera>.jsonl` | 시뮬레이터 형식(아래) | 카메라별 프레임마다 촬영 시각과 2D keypoint. 센서가 내는 값만 담는다 |
| `world_tracks.jsonl` | `contracts/providers/world_tracks.schema.json` 한 줄씩 | World tick(기본 10 Hz)마다 WorldTrackProvider SIM 형식 |
| `truth/world_truth.jsonl` | 시뮬레이터 형식 | tick 마다 사람별 정답 관절 3D 전부(가림과 무관), 지지 표면, 접지점, 머리 꼭대기 |
| `truth/camera_frames.jsonl` | 시뮬레이터 형식 | 프레임별 실제 촬영 시각과 보고한 촬영 시각 |
| `manifest.json` | 시뮬레이터 형식 | 시나리오 해시·시드, `input_provenance`(`contracts/common/defs.schema.json#/$defs/input_provenance`), 파일별 sha256 |

`truth/` 는 평가용 정답이다. 하류 모듈 입력으로는 `observations/` 와 `calibration.json` 만 쓴다.

### observations 한 줄

```json
{"camera_id": "cam1", "stream_epoch": 0, "frame_sequence": 12, "estimated_capture_time_ms": 1760342400483,
 "timestamp_source": "SIM", "timestamp_uncertainty_ms": 0,
 "detections": [{"local_track_id": "cam1-1", "bbox": [x1, y1, x2, y2], "joint_layout": "COCO17",
                 "keypoints_2d": [[u, v, conf], ...17개], "foot_tips_2d": [[u, v, conf], [u, v, conf]]}]}
```

- 프레임 식별·시각 필드는 I0 프레임 헤더(명세 12.7)와 같은 이름이다. 나머지 메시지 스키마(`observation_2d`)가 정해지면 맞춘다.
- `keypoints_2d` 는 COCO17 순서, `foot_tips_2d` 는 [l_toe, r_toe].
- 보인 관절: 픽셀 잡음(`pixel_noise_sigma_px`)을 더한 좌표와 `confidence_range` 안의 신뢰도.
- 가린 관절(`occluded`): `conf_zero` 면 잡음 없는 투영 좌표에 conf 0, `null` 이면 null.
- 화면 밖 관절(`out_of_frame`): `null`(기본) 또는 `conf_zero`. 카메라 뒤 관절은 늘 null.
- 보인 COCO17 관절이 `min_visible_joints` 보다 적으면 그 카메라에서 검출하지 않는다(누락).
- `local_track_id` 는 카메라마다 따로 붙인 추적 ID다. 사람 정체성의 정본이 아니다. 정답 대응은 `world_tracks.jsonl` 의 `per_camera` 와 `manifest.json` 의 `people` 에 있다.

### 촬영 시각

카메라 k 번째 프레임의 실제 촬영 시각 = `start_time_ms + time_offset_ms + round(k · 1000 / fps) + 지터`, 지터는 [−`capture_jitter_ms`, +`capture_jitter_ms`] 정수 균등분포.
보고 시각 `estimated_capture_time_ms` = 실제 촬영 시각 + [−`timestamp_error_ms`, +`timestamp_error_ms`] 정수 균등분포, `timestamp_uncertainty_ms` = `timestamp_error_ms`.
정답 관절은 실제 촬영 시각 기준으로 투영한다. 모든 시각은 UTC int64 ms 다.

### world_tracks 의 의미

정답을 그대로 채우지 않고 Tier 0 출력과 같은 의미로 가린다(명세 20.4 규칙 1).

- 카메라마다 보고 시각이 tick 에 가장 가까운 프레임을 쓴다(`per_camera`).
- 2대 이상에서 보인(conf > 0) 관절만 채우고 나머지는 null(`visible = false`, `validity = UNAVAILABLE`). `joint_source` 는 모두 `observed`.
- 골반은 양쪽 엉덩이, 접지점은 발목 하나 이상이 2대 이상에서 보여야 채운다. 접지를 모르면 `ground_anchor_xyz = null`, `support_surface_validity = UNKNOWN` 이다(Z=0 대체 금지).
- 골반도 접지점도 없는 사람은 그 tick 의 `tracks` 에 넣지 않는다.
- 관절 값 자체는 정답(tick 시각의 3D)이다. 삼각측량 오차는 없다.

## 모델

- World: 미터, 오른손, +Z 상방, 바닥 Z = 0. 교실 바닥은 `[0, size_x] × [0, size_y]`.
- 카메라: 핀홀(`fx = fy = (width / 2) / tan(hfov_rad / 2)`, 주점은 영상 중앙). 자세는 `position` 에서 `look_at` 을 보고 `roll_rad` 만큼 돈다. 왜곡은 `NONE`, `OPENCV_PINHOLE`(k1 k2 p1 p2 k3), `OPENCV_FISHEYE`(k1 k2 k3 k4) 를 OpenCV 식 그대로 적용한다.
- 사람: 시각이 붙은 경유점을 등속 직선으로 잇는다. 키 1.70 m 기준 골격 비율을 키에 맞춰 늘리고, 걸을 때 다리·팔을 진자처럼 흔든다. 발목은 늘 지지 표면 위 같은 높이다. 멈추면 직전 진행 방향(없으면 `initial_heading_rad`)을 본다.
- 자세(선택, R4 낙상·오경보 대조용): `postures` 키가 있으면 그 시각에 그 자세이고, 앞 키에서 다음 키까지 관절 위치를 보간한다(`ease`: `linear` 등속, `in` 진행률²로 점점 빨라짐, 넘어짐용). 자세는 `stand`(걷기 골격), `sit`(의자, 머리 꼭대기 약 1.26 m), `squat`(쪼그림, 약 1.12 m), `stoop`(허리 굽힘, 약 1.02 m), `sit_floor`(바닥 앉기, 약 0.90 m), `lie`(누움, 약 0.10 m, 발은 경유점·머리는 진행 방향 앞쪽 1.75 m) 이다(키 1.70 m 기준, 지지 표면 위). 고정 자세는 대략적인 비율이고, 서기와 섞는 전환 중에는 걷기 흔들림이 서기 비중만큼 남는다. 자세 키가 있는 사람의 가림 원기둥 위 끝은 가장 높은 점 + 5 cm 다(누운 사람의 몸 방향 길이는 모델링하지 않는다). 자세 키가 없으면 출력은 이전과 바이트 단위로 같다.
- 지지 표면: `surfaces` 의 상자(바닥에 놓인 축 정렬 상자). 사람 위치가 상자 위면 그 윗면, 아니면 바닥이다. 상자는 가림 물체이기도 하다.
- 가림: 카메라 중심 → 관절 선분이 다른 사람 원기둥(지지 표면 ~ 키, 반지름 `radius_m`)이나 상자를 지나면 가린다. 얼굴(코·눈)은 카메라가 머리 뒤쪽이면 가린다. 몸통·팔다리 자기 가림은 모델링하지 않는다.

## 시나리오 필드

길이는 미터, 각도는 라디안, 시각은 ms 정수다. 밑줄로 시작하는 키(`_note`)는 메모로 무시하고, 그 밖의 모르는 키는 오류다.

| 키 | 필수 | 기본값 | 뜻 |
|---|---|---|---|
| `name`, `seed`, `start_time_ms`, `duration_ms` | 예 | | 이름, 난수 시드, 시작 시각(UTC ms), 길이 |
| `description`, `site_id`, `world_rate_hz` | | `""`, `sim-classroom`, 10 | |
| `room.size_m` | 예 | | 교실 바닥 [x, y] |
| `room.ceiling_height_m` | | 3.0 | 천장 높이. 카메라는 교실 안·천장 아래에 둔다 |
| `surfaces[]` | | `[]` | `{id, box_min, box_max}`. `box_min` 의 z 는 0 |
| `camera_defaults` | | Tapo C110 | 아래 카메라 키의 기본값을 바꾼다 |
| `cameras[]` | 예 | | `{id, position, look_at}` + 카메라 키 |
| `people[]` | 예 | | `{id, waypoints: [{t_ms, xy}], height_m = 1.70, radius_m = 0.2, initial_heading_rad = 0, postures = []}` |
| `people[].postures[]` | | `[]` | `{t_ms, posture, ease = "linear"}`. 위 '모델'의 자세 |
| `keypoints` | | 아래 | `pixel_noise_sigma_px = 1.0`, `confidence_range = [0.6, 0.98]`, `min_visible_joints = 3`, `occluded = conf_zero`, `out_of_frame = null` |

카메라 키(기본값은 Tapo C110 stream1, 명세 3.3): `image_size = [2304, 1296]`, `fps = 25`, `hfov_rad = 1.832596`(가로 105°, 판매처 정보·미확인), `distortion = {model: NONE, coeffs: []}`, `roll_rad = 0`, `time_offset_ms = 0`, `capture_jitter_ms = 0`, `timestamp_error_ms = 0`.
`capture_jitter_ms + timestamp_error_ms` 는 프레임 간격의 절반보다 작아야 한다.

## 남은 일

- S15P31A101-39 의 FrameSource·CalibrationProvider 인터페이스가 머지되면 이 출력을 읽는 SIM Provider 어댑터로 연결한다.
- 나머지 메시지 스키마(S15P31A101-20)의 `observation_2d` 가 정해지면 `observations/` 형식을 맞춘다.
