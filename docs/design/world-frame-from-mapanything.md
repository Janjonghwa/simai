# MapAnything 초기 추정과 바닥·벽으로 World 좌표계 고정 설계

S15P31A101-102(열린 공간 벽 한 방향·바닥 두 점·카메라 높이 축척은 S15P31A101-277, 아래 '열린 공간' 절). 기획서 §4.1(World 좌표계), §4.3 1·2단계(MapAnything 초기 추정, 좌표계 고정, 스케일 우선순위), §4.4(PROVISIONAL), §11.5(영상), §20.4(출처·상한), 기획서 수정 6(실측 평면도 없이 첫 CALIBRATED 때 바닥·벽으로 World 를 정하고 바꾸지 않음) 기준이다.
코드는 `ai/src/simai_ai/calibration/world_frame/`, MapAnything 실행 쪽은 `ai/experiments/static_scene/run_mapanything.py`(`--mode prepared`)·`session3_jobs.py`, 시험은 `ai/tests/calibration/world_frame/`·`ai/tests/experiments/static_scene/` 에 있다.
외부 파라미터 도구(-214, `docs/design/extrinsics-from-recording.md`)의 `--world-alignment` 입력을 이 티켓이 확정했다.

## 흐름

| 단계 | 어디서 | 도구 | 출력 |
|---|---|---|---|
| 1 입력 준비 | 노트북 CPU | `world_frame.cli prepare --image CAM=사진 … --intrinsics <AnyCalib 기록> --segmenter yolo26x-seg.pt …` | 입력 폴더(`simai.mapanything_inputs`): 왜곡 편 영상, 펴진 K, 사람 마스크 PNG, `prepared.json` |
| 2 MapAnything | GPU 세션(MapAnything 이미지 Pod) | `gpu_session1.py run --exp-jobs <session3_jobs.py 출력>` → `run_mapanything.py --mode prepared` | `cameras.json`, `raw.npz`, `metrics.json`(기본·apache 둘) |
| 3 World 고정 | 노트북 CPU | `world_frame.cli fix --run-dir <결과> --intrinsics <같은 기록> --prepared <같은 폴더> [--scale-evidence] [--foot-points] [--floor-points] [--camera-heights]` | `simai.world_alignment` 결과(아래) |
| 4 정밀화 | 노트북 CPU | `extrinsics.cli adjust … --cameras-json <같은 결과>/cameras.json --world-alignment <3 결과>` (-214) | 번들 조정, 보정 버전 제안 |

**known-K.** AnyCalib 기록(`simai.intrinsics_estimate`)을 -214 와 같은 `bundle_init_from_record` 로 읽어 카메라별 K·[k1, k2] 로 사진을 편다. 펴진 K 는 `cv2.getOptimalNewCameraMatrix`(alpha 0, 주점 가운데, 같은 크기)다. 펴기 표가 원본 밖을 읽으면 오류다(검은 테두리를 장면으로 넣지 않는다). 펴기는 회전을 바꾸지 않으므로 MapAnything 이 낸 자세(cam2world)는 원래 카메라의 자세 그대로이고, 보정 문서의 K·왜곡은 원래(왜곡 있는) 기록 값이다. `run_mapanything.py --mode prepared` 는 펴진 영상과 펴진 K(원본 해상도)를 `intrinsics` 로 넣고 pose 는 넣지 않는다. MapAnything 전처리가 K 를 모델 해상도로 옮긴다.

**사람 마스크.** 펴진 영상에서 YOLO seg(person) 마스크의 합을 15 px 팽창한다(2304 폭의 0.65%, 경계 흐림·팔다리 끝). MapAnything 입력 영상은 지우지 않고(모델이 보는 장면을 바꾸지 않음) `fix` 가 마스크를 MapAnything 전처리 규칙(map-anything @3d10cf7 `utils/cropping.py` `crop_resize_if_necessary`: 가로·세로 비 중 큰 쪽으로 덮게 줄이고 가운데를 자름)으로 모델 해상도로 옮겨, 조금이라도 덮인 픽셀의 점을 뺀다. Pod 의 `fused.ply`·`pointcloud.glb` 에는 사람이 남는다(이 티켓은 쓰지 않음, 아래 넘긴 일).

## World 고정 규칙 (`frame.py`)

X_world = s · Ra · (X_mapanything − o). 길이 한도는 MapAnything 단위(모델 추정 미터)다.

1. **상방 사전값**: 카메라 영상 위쪽(−y) 방향의 평균. 바로 선 설치 전제(`check_cameras_upright` 와 같음).
2. **바닥(Z = 0)·중력**: 사전값과 30° 안인 평면을 RANSAC 으로 찾고 법선을 상방 쪽으로 돌린다. 모든 카메라가 0.5 m 넘게 위이고, 평면 아래 0.10 m 넘게 떨어진 점이 3% 이하인 평면 중 가장 낮은 것. "가장 낮다"는 카메라 중심 평균에서 잰 카메라 높이가 가장 큰 것이다(MapAnything 원점 기준 offset 으로 비교하면 원점이 첫 카메라 = 방 가장자리라, 법선이 몇 도 다른 평면끼리 수평 팔 길이만큼 높낮이가 뒤바뀐다). 천장 높이 기록도 같은 기준점에서 잰다. RANSAC 다듬기는 인라이어가 줄면 이전 평면을 유지한다. 없으면 **바닥을 비운다**(`FLOOR_NOT_FOUND`, 변환·보정 문서 없음, Z = 0 을 지어내지 않음). 중력은 바닥 법선의 반대.
3. **발 접지점 확인**(입력이 있을 때): 접지점과 바닥의 **절대** 거리 중앙값 median(|d|) ≤ 0.05 m 이고 |d| 90 백분위 ≤ 0.15 m 가 아니면 실패(`FLOOR_FOOT_MISMATCH`). 부호 중앙값은 위·아래 오차가 섞이면 0 으로 상쇄되므로 편향 진단값(`signed_median_distance_ma`)으로만 기록한다. 10개 미만이면 확인하지 못함(`NOT_CHECKED`, 실패도 확인도 아님).
4. **경계 벽**: 바닥 위 0.3 m 보다 높은 점에서 상방과 수직(±10°)인 평면. 바닥 점이 거의 모두 한쪽(밖 0.10 m 넘는 점 5% 이하)이고 벽 방향 길이 1 m 이상인 것만 경계 벽이다. 법선은 방 안쪽.
5. **축**(축 출처 `WALLS`, 바닥 두 점이 없을 때): 경계 벽을 방향 묶음(평행 ±10°)으로 묶어 지지 점이 많은 순서로 서로 수직(90 ± 10°)인 두 묶음을 고른다. 묶음이 하나뿐이면(`ONE_WALL_DIRECTION`) 벽 한 방향 경로(`WALL_AND_FLOOR`, -277 절)로 가고, 없거나(`NO_WALL`) 수직 쌍이 없으면(`WALLS_NOT_ORTHOGONAL`) **축이 모호해 실패**(`AXES_AMBIGUOUS`, 사유에 `--floor-points` 안내)다. 세 방향 이상이면 경고.
6. **원점**: 두 묶음에서 각각 지지 점이 가장 많은 벽과 바닥, 세 평면의 교점(방 모서리 바닥). X·Y 는 두 벽의 방 안쪽 법선이고 X × Y = +Z(오른손)가 되게 어느 벽이 X 인지 정한다. 두 벽의 직교 오차는 Y 가 흡수한다(기록 `y_vs_wall_deg`). 직교화한 축에서 바닥 점이 0.10 m 넘게 −X·−Y 쪽인 비율이 5% 를 넘으면 실패(`AXES_AMBIGUOUS`, QUADRANT)다: 원점 모서리가 둔각(안쪽 법선 사이 < 90°)이면 직교 축으로는 방이 음의 쪽으로 넘어간다. 예각 모서리는 방이 사분면 안에 든다. 기준은 경계 벽의 밖 점 기준과 같다(같은 "방 경계" 뜻). 그래서 성공한 결과의 방은 +X·+Y 쪽에 있다.
7. **스케일**(`choose_scale`, 명세 4.3 우선순위): 실측 정물(정물별 평균 → 정물끼리 평균, 정물끼리 2% 넘게 어긋나면 그 근거를 버리고 다음으로) > 카메라 높이 실측(-277, 아래) > 키를 아는 사람 > 사람 키 통계(표본 최다) > 표준 사물 > 모델 추정(MapAnything 메트릭 = 1, 언제나 마지막 근거). 고른 값과 5%(CALIBRATED 교차 확인 허용치) 안인 다른 근거를 교차 확인으로, 나머지를 불일치로 기록한다. 지금 노트북 입력은 `--scale-evidence`(배율 = 실제 / MapAnything 길이)이고 근거를 만드는 쪽(-100 정물 검출, 사람 키)은 이 티켓 밖이다.

| 수치 | 값 | 근거 |
|---|---|---|
| 평면 허용 거리 | 0.03 m | MapAnything 미리보기 바닥 rms 0.015, 합성 0.002. 교실 바닥·벽 요철보다 크고 책상 높이(0.75)보다 훨씬 작게 |
| 바닥 기울기 한도 | 30° | 카메라 roll·pitch 가 상방 사전값을 흔드는 정도(미리보기 8.7°)보다 넉넉히, 벽(90°)과는 멀게 |
| 바닥 아래 점 3% / 0.10 m | — | 책상 윗면은 아래로 바닥·벽 점이 이어져 20% 넘게 걸린다(미리보기 22%·28%, 합성 반례) |
| 벽 수직 ±10°, 묶음 ±10°, 직교 ±10° | — | 교실 벽은 직각. 묶음·직교를 같은 폭으로 두어 45° 근처 방향이 두 묶음에 걸치지 않게 |
| 경계 벽 밖 5% / 0.10 m, 길이 1 m | — | 방 가운데 수직면·사람은 바닥 점이 양쪽에 있다. 벽에 붙은 사물함·칠판은 거의 같은 평면이라 남는다(잔여 위험) |
| 발 접지점 0.05 / 0.15 m | — | 접지 분해능 약 8 cm(명세 5.2)보다 작게 절대 거리 중앙값을, 개별 오차는 넉넉히 |
| 카메라 바닥 위 0.5 m 초과 | — | 교실 카메라는 2.5 m 안팎(미리보기 3.1~3.3 모델 단위). 바닥 아래 반사·잡음 평면이나 카메라 높이의 책상 윗면을 거르는 최소값이고, 책상(0.75)보다 낮은 카메라 설치는 없다 |
| 스케일 교차 5%·정물 일치 2% | — | 새로 정하지 않고 CALIBRATED 판정 기본값(`CalibratedCriteria.cross_check_tolerance`·`measured_object_agreement`)을 그대로 써서 초기 추정과 판정이 같은 근거를 같은 뜻으로 본다. 2% 는 정물별 값의 평균으로부터의 편차다 |
| 사분면 밖 바닥 점 5% / 0.10 m | — | 경계 벽 기준과 같음. 합성 평행사변형 방(±9°)에서 둔각 모서리는 걸리고 예각·직각은 통과 |
| 신뢰도 하위 10% 제외 | — | `run_mapanything.py` 점구름(`--conf-percentile 10`)과 같게 |
| RANSAC 600회, 평가 점 4만, 시드 0 | — | 합성 교실 0.2 s(노트북). 같은 입력이면 같은 결과 |

## 결과 파일 (`simai.world_alignment` 0.2.0)

-214 `load_world_alignment` 가 읽는 형식에 근거를 더했다(계약이 아닌 도구 형식). 0.2.0(-277): `axes_source`(OK 일 때 `WALLS`·`WALL_AND_FLOOR`·`MANUAL_FLOOR_POINTS`, 실패면 null)·`origin_source`(`WALL_AND_FLOOR` 이면 `CAMERA_LAYOUT_ON_WALL`), `inputs.floor_points`·`inputs.camera_heights`(경로·sha256·값), 보정 문서는 계약 calibration 2.1.0(`world_axes_source`).

- `status` OK/FAILED, `reason`·`detail`. OK 일 때만 `mapanything_to_world`(4×4 [sR | T])와 `calibration`(계약 calibration 2.1.0 문서)을 싣는다. FAILED 결과는 -214 가 거절한다(`load_world_alignment`, 임시 정렬로 바꾸지 않음).
- `cameras_json_sha256`: 이 변환을 만든 cameras.json. -214 `adjust` 는 `--cameras-json` 과 다르면 거절한다(다른 실행의 MapAnything world 는 원점·축이 다르다).
- `calibration`: state **PROVISIONAL**(독립 검증점이 없다, 4.4), provider_source G1_AUTO(입력 영상·근거 중 SIM 이 있으면 SIM, DATASET_GT 면 DATASET_GT → CALIBRATED 불가, -214 와 같은 규칙), scale_basis·scale_cross_check_basis 는 위 스케일 선택, K·왜곡은 내부 파라미터 기록, R·t 는 MapAnything 자세 × World 변환, validation 없음, `check_calibration`(스키마·기하) 통과가 조건(못 하면 FAILED `CALIBRATION_CHECK`).
- `input_provenance`: 입력 폴더(영상)·내부 파라미터 기록·스케일 근거·발 접지점의 출처를 -214 `combine_input_provenance` 로 합친다. frames 는 입력 폴더 출처(교실 사진 FILE `classroom-stills-…`), calibration 은 MODEL_ESTIMATE `mapanything-<run_id>`(-214 와 같은 값)이고 입력 중 하나라도 SIM·DATASET_GT 면 calibration 이 그 출처(SIM 우선)가 되어 `is_upper_bound` 가 출처와 함께 true 다(`check_input_provenance` 통과, -214 `load_world_alignment` 가 그대로 읽고 이어 쓴다). 어느 입력이 상한인지는 `upper_bound_inputs`. provider_source 도 그 출처라 G1_AUTO 로 바뀌지 않는다.
- 연결 확인(종료 2, 같은 카메라 이름의 다른 입력을 섞지 않게): 입력 폴더의 `intrinsics.sha256` 이 `--intrinsics` 와 다름, MapAnything `metrics.json` 의 `prepared_sha256` 이 `--prepared` 의 prepared.json 과 다름, prepared 모드 실행(`prepared_sha256` 또는 `args.mode` prepared)인데 `--prepared` 없음(마스크·출처·내부 파라미터 확인이 빠져 SIM 입력이 G1_AUTO 로 바뀜), `--prepared` 를 줬는데 그 폴더로 돈 기록이 없음(images 모드·metrics.json 없음: 왜곡 안 편 깊이에 펴진 영상 마스크가 어긋남). -214 `adjust` 도 결과의 `inputs.intrinsics.sha256` 이 `--intrinsics` 와 다르면 거절한다.
- `evidence`: 바닥(법선·지지·rms·사전값과의 각도·중력·발 확인)과 바닥 후보 목록, 벽 목록(경계 여부·밖 비율·길이·기울기), 축(묶음, 직교 오차, 규칙), 원점(MapAnything world 좌표·규칙), 스케일(후보·교차 확인·불일치·우선순위), 방(바닥 XY 2·98 백분위, 사분면 밖 비율, 카메라 높이, 천장 높이(모든 카메라보다 위인 수평면이 있을 때)).
- `reprojection`: **카메라 간 왕복 재투영**(카메라 i 의 점 → j 에 투영 → j 깊이와 5% 안이면 그 j 점을 다시 i 로, 처음 픽셀과의 거리). 모델 해상도 px 와 원본 px(원본 폭 비). 2D 관측 재투영 오차가 아니라 MapAnything 깊이·자세의 일관성이라 계약 `reprojection_error_px` 에는 넣지 않는다(관측 재투영은 -214 번들 조정이 보정 문서에 넣는다).

## 열린 공간: 벽 한 방향·바닥 두 점·카메라 높이 축척 (S15P31A101-277)

3차 GPU 세션(2026-10-26) 새 교실 배치에서 `fix` 결과가 w1 `AXES_AMBIGUOUS(ONE_WALL_DIRECTION)`(창문 벽 한 방향만), w2 `FLOOR_NOT_FOUND` 였다. 교실이 넓은 열린 공간이라 "서로 수직인 경계 벽 두 묶음 + 모서리" 전제가 맞지 않는다. 바닥·중력은 찾으므로 모르는 것은 바닥 위 방향(X·Y)·원점·축척이다. Lead 결정(2026-10-26, 사용자: 높이 실측·바닥 기준점 설치는 가능하면 피하고 최대한 추정): 축 우선순위는 **선택 입력 바닥 두 점(`MANUAL_FLOOR_POINTS`) > 수직 경계 벽 두 방향(`WALLS`, 기존 경로 그대로) > 경계 벽 한 방향 + 바닥(`WALL_AND_FLOOR`) > 실패**(경계 벽이 없으면 지금처럼 비움). 축척은 필수로 만들지 않고 근거가 없으면 기존 체계(MODEL_ESTIMATE 등), 카메라 높이 실측은 선택 근거다.

**벽 한 방향(`WALL_AND_FLOOR`, 입력 없음).** 경계 벽 묶음이 한 방향뿐(`ONE_WALL_DIRECTION`)이고 두 점이 없으면 지지 점이 가장 많은 경계 벽 하나의 방 안쪽 수평 법선 = X, Y = Z × X, Z = 바닥 법선. 원점은 그 벽과 바닥의 교선 위에서 카메라 중심 평균을 교선에 수직으로 내린 점이다(`evidence.origin.source` = `CAMERA_LAYOUT_ON_WALL`, 결과 파일 `origin_source`). 벽은 영구 구조라 다시 보정해도 축은 같고(같은 벽이 뽑히는 한), 원점은 카메라가 벽을 따라 움직이면 Y 로 그만큼 움직인다(잔여 위험, 결과 경고에도 적음). 사분면 판정은 X 쪽(벽 안쪽)만 한다: 바닥 점의 5% 넘게 −X 로 0.10 m 넘게 벗어나면 `AXES_AMBIGUOUS(QUADRANT_X)`(경계 벽 기준과 같아 보통 통과). Y 쪽은 원점이 방 가운데라 양쪽에 바닥이 있으므로 사분면 비율만 기록한다. 바닥 두 점을 주면 이 벽의 축과의 각도 차만 기록한다(`walls_comparison.walls` = `ONE_WALL`). 원점 출처는 벽 두 방향이면 `WALL_CORNER`, 두 점이면 `FLOOR_POINT` 다.

**바닥 두 점(선택 입력, `frame.manual_floor_axes`, `inputs.floor_point_rays`).**

1. 입력: 카메라 하나의 그림에서 찍은 두 점(원점, +X 방향 점). 기본은 원본(왜곡된) 그림 픽셀(`pixel_space: ORIGINAL`, 설치자가 원본 스틸에서 찍음), 펴진 그림 픽셀(`PREPARED`)도 받는다. 픽셀은 OpenCV 규약(왼쪽 위 픽셀 중심 (0, 0)). `--prepared` 실행에서만 받는다(픽셀 → 광선 규약이 입력 폴더에 묶임).
2. 원본 픽셀 → `cv2.undistortPoints(K, 왜곡, P = K')`(내부 파라미터 기록의 그 카메라 K·[k1, k2], 입력 폴더의 펴진 K'. `prepare` 의 `initUndistortRectifyMap(K, 왜곡, None, K')` 과 같은 규약) → 펴진 픽셀 → 사람 마스크와 같은 crop·resize(픽셀 중심 규약)로 모델 해상도 픽셀 → MapAnything 출력 K(raw.npz `intrinsics`, 점구름을 만든 K) → 그 카메라 cam2world 회전 → MapAnything world 광선. 펴진 K' 로 만든 광선과의 각도를 `model_k_vs_prepared_k_deg` 로 기록한다(w1 0.05°·0.12°: 두 규약이 맞음). 입력 폴더의 `source_camera` 가 기록과 다르거나 픽셀이 원본 그림·펴진 그림(alpha 0 으로 잘린 테두리)·모델 입력(가운데 자름) 밖이면 입력 오류(종료 2)다.
3. 광선과 바닥 평면의 교점: t = h / (−n·d)(h = 그 카메라의 바닥 위 높이). 원점 = 첫 교점, X = (둘째 − 첫)을 바닥에 투영해 정규화, Z = 바닥 법선(상방), Y = Z × X(오른손). 원점은 바닥 위에 있다(교점).
4. 비우는 경우(`FLOOR_POINTS_INVALID`, 사유 코드를 detail 에): `BEHIND_CAMERA`(n·d ≥ 0, 광선이 수평 위라 교점이 카메라 뒤), `RAY_GRAZING`(광선과 바닥이 이루는 각 < 10°), `POINT_NOT_ON_FLOOR`(그 모델 픽셀 ±2 이웃의 MapAnything 점 중앙값이 바닥에서 0.10 넘게 떨어짐: 책상·사람을 찍음. 이웃 점이 3개 미만이면 `NOT_CHECKED` 로 기록하고 진행), `POINTS_TOO_CLOSE`(두 점의 바닥 위 거리 × 스케일 < 1.0 m). 바닥을 못 찾으면 지금처럼 `FLOOR_NOT_FOUND` 다(두 점이 있어도).
5. 사람 지정이 이긴다: 두 점을 주면 벽 두 방향이 있어도 두 점으로 정한다(같은 점 → 같은 변환, 재현성). 벽 축이 있으면 `walls_comparison.angle_to_wall_axes_deg`(벽 X·Y 중 가까운 축과의 각도, ±45°)만 기록하고 10° 를 넘으면 경고한다. 벽이 없으면 `walls_status` 에 벽 경로 사유를 남긴다.
6. 사분면(방이 +X·+Y) 판정은 이 경로에 적용하지 않는다(사람이 정한 기준). 바닥 점의 사분면별 비율(`floor_quadrant_fractions`)만 기록한다.
7. 스케일은 축보다 먼저 정한다(바닥만 있으면 정해짐, 최소 거리를 World m 로 보려고). 축을 못 정해 실패해도 스케일 근거(카메라 높이 비 포함)는 결과에 남는다.

**카메라 높이 축척(선택 입력, `frame.camera_height_scale`, 스케일 근거 `MEASURED_CAMERA_HEIGHT`).** 입력은 카메라별 바닥 → 렌즈 중심 높이(m). 바닥 평면에서 잰 카메라 중심 높이(MapAnything 단위)와의 비를 카메라마다 내고 중앙값을 근거 값으로 쓴다. 결과에 없는 카메라의 높이는 실행 도구가 입력 오류로 거절한다(카메라 이름 오타). 쓸 수 있는 카메라가 2대 미만이거나 비가 중앙값에서 3% 넘게 벗어나는 카메라가 있으면 쓰지 않고 다음 근거로 간다(실측 정물 불일치와 같은 처리). 우선순위는 실측 정물 다음이다: 줄자 실측이라 사람 키(1~2%)보다 정확하지만, 카메라 4대가 모두 한 평면(바닥)에서 잰 높이라 방 안 여러 곳의 길이를 직접 재는 정물보다는 모델 깊이 오차가 덜 평균된다. CALIBRATED 주 근거(실측 정물 2개 일치·사람 키 통계 20명)는 바꾸지 않았다(`PRIMARY_SCALE_BASES` 그대로, 교차 확인 근거 `CROSS_CHECK_SCALE_BASES` 에만 추가). `--scale-evidence` 로는 받지 않는다(찾은 바닥과 비교해야 하는 값).

| 수치 | 값 | 근거 |
|---|---|---|
| 광선과 바닥 각 하한 | 10° | 바닥 위 위치 오차는 평면 높이 오차 δh 에 대해 δh / tanθ, 픽셀 각 오차 δα 에 대해 약 h·δα / sin²θ 다. h = 2.7 m, δα = 3 px / 1259 px 이면 10° 에서 21 cm, 20° 에서 5.5 cm, 45° 에서 1.3 cm. 10° 는 h = 2.7 m 에서 수평 15 m(교실보다 멂)라 지평선 근처의 무의미한 점만 막고, 안내는 20° 이상(수평 7.4 m 안)을 권한다. w1 임시 점 41.9°·44.4° |
| 찍은 곳 바닥 확인 | 0.10 (MapAnything 단위), 이웃 ±2 모델 픽셀 중앙값 | 바닥 아래 점 기준(0.10)과 같은 값. 바닥 rms 1.15 cm(w1)보다 넉넉하고 의자 좌판(0.45 m)·책상(0.75 m)은 확실히 걸린다. 가방처럼 낮은 물체는 통과할 수 있다(잔여 위험) |
| 두 점 최소 거리 | 1.0 m (스케일 반영 World m) | X 방향 오차 ≈ atan(√2·σ / L). 바닥 점 오차 σ = 2 cm(각 30° 이상)이면 L = 0.5 m 에서 3.2°, 1 m 에서 1.6°, 3 m 에서 0.5°. Lead 예시 0.5 m 는 3° 넘게 흔들려 1 m 로 정했고 안내는 3 m 이상을 권한다. w1 임시 점 3.46 m |
| 카메라 높이 퍼짐 한도 | 중앙값에서 3% | 카메라 한 대 오차: 줄자 ±2 cm(2.7 m 의 0.74%), 렌즈 중심 위치 ±1~2 cm(약 0.5%), 바닥 평면 rms 1.15 cm / 높이 2.05(모델 단위, 0.56%)와 평면 기울기 오차 × 카메라 수평 거리(0.1° × 5 m ≈ 0.9 cm) → 약 1.1%(1σ). 4대 중 중앙값에서 가장 먼 값의 2~3σ 가 약 3%. 실측 정물 일치 2%·교차 확인 5% 사이 |
| 카메라 높이 최소 대수 | 2 | Lead 결정. 1대는 퍼짐을 볼 수 없다 |

**계약.** `contracts/providers/calibration.schema.json` 2.1.0(필드 추가, 마이너): `world_axes_source`(선택) enum `WALLS`·`WALL_AND_FLOOR`·`MANUAL_FLOOR_POINTS`·`CAMERA_LAYOUT`(예약, 내지 않음), scale_basis 값 `MEASURED_CAMERA_HEIGHT`. 생략은 '기록 없음'이다(2.0.0 문서·DATASET_GT·SIM 대체 구현). 원점 출처는 계약에 넣지 않고 결과 파일(`origin_source`, `evidence.origin.source`)에만 둔다(축 출처가 원점 규칙을 정함). 예시 `calibration.world_frame_wall_and_floor.json`·`calibration.world_frame_manual.json`, 거절 예시 `invalid/calibration.unknown_world_axes_source.json`. `world_frame.cli fix` 와 -214 `adjust` 의 버전 제안(`HumanBundleResult.propose_to` → `CalibrationVersionStore.propose`, 결과의 `axes_source` 를 옮김)이 문서에 싣는다.

**합성 확인(SIM, 상한, 제품 성능 아님, `tests/calibration/world_frame/test_open_space.py`).** 벽 한 방향만(x=0·x=L 벽) 입력 없이 → `WALL_AND_FLOOR` OK, X = 정답 ±x, 원점 = 벽·바닥 교선 위 y = 3.25(카메라 y 평균) 3 cm 안, +X 밖 바닥 점 ≤ 5%, ±Y 양쪽 바닥 30% 넘게(기록만). 카메라 중심을 정답 +y 1 m 옮겨 넣으면 회전은 같고 원점만 Y 로 1.04(모델 단위) 이동. 두 벽 방은 그대로 `WALLS`(원점 `WALL_CORNER`). 벽 한 방향만·벽 없음 + 두 점(cam_A 에서 보이는 바닥 (3, 2)·(6, 1.5)) → OK, 원점·+X 점 3 cm 안(평면 허용 거리), 오른손·+Z 상방, 길이 = 정답 × 1.04(모델 스케일) 또는 정답(카메라 높이). 두 점 없이 벽 없음 → `AXES_AMBIGUOUS(NO_WALL)` + 안내. 같은 점·광선 길이만 다른 입력 → 같은 변환. 두 벽 방 + 두 점 → 사람 지정이 이기고 벽과 나란하면 각도 0°, 30° 돌린 X 면 각도 30°·경고. 각 9.9° 실패·10.1° 통과, 수평 위 광선 `BEHIND_CAMERA`, 정답 0.98 m 두 점은 모델 스케일(1.019 m)이면 통과·카메라 높이 스케일이면 `POINTS_TOO_CLOSE`, 책상 윗면을 찍은 픽셀(원본 그림 → 광선 경로) `POINT_NOT_ON_FLOOR`. 원본(왜곡 [−0.05, 0.01]) 픽셀 → 광선이 정답 광선과 0.05° 안, 펴진 픽셀로 줘도 같은 광선. 카메라 높이 정답 → 스케일 1/1.04(0.5% 안)·카메라 높이 ±1 cm·모델 추정이 교차 확인(4%), 벽 경로의 축·원점은 스케일 근거와 관계없이 그대로, 실측 정물이 있으면 정물이 이기고 카메라 높이가 교차 확인. 한 대 6% 어긋남·1대만 → 근거 버림(MODEL_ESTIMATE), 퍼짐 2.9% 통과·3.1% 거부.

**입력 형식(사용자가 줄 값).**

```json
{"format": "simai.floor_points", "camera_id": "cam_C", "pixel_space": "ORIGINAL",
 "origin_px": [430, 950], "x_axis_px": [1600, 1090], "image": "cam_C_03.jpg", "note": "교실 앞 기준점 A → B"}
```

```json
{"format": "simai.camera_heights", "heights_m": {"cam_A": 2.71, "cam_B": 2.69, "cam_C": 2.70, "cam_D": 2.72},
 "method": "줄자, 바닥 → 렌즈 중심", "measured_at": "2026-10-27"}
```

카메라 높이는 맨 대응 `{"cam_A": 2.71, …}` 도 받는다. 둘 다 선택 `input_provenance` 를 받고 SIM·DATASET_GT 면 결과 전체가 상한이다(다른 입력과 같은 규칙).

**3차 GPU 세션 결과(노트북 `fix`, 결과는 scratchpad, 커밋 안 함).**

| Run | 결과 |
|---|---|
| w1 (map-anything, known-K·마스크) -102 규칙(이 티켓 전) | `AXES_AMBIGUOUS(ONE_WALL_DIRECTION)`. 바닥 지지 48,858점, rms 0.0115, 상방 사전값과 1.76°. 경계 벽 2개가 모두 창문 벽 한 방향(나머지 수직면 후보 6개는 바닥 점 11~44% 가 밖이라 경계 벽이 아님). 카메라 간 재투영 중앙값 2.0~4.1 모델 px(원본 9~18 px), p95 17.6~23.7 모델 px |
| **w1 입력 없이(이 티켓)** | **OK, `WALL_AND_FLOOR`**, 원점 `CAMERA_LAYOUT_ON_WALL`. X 벽 = 경계 벽 #3(지지 8,426점, 길이 4.8, 기울기 1.8°, 밖 0%; 같은 묶음의 다른 경계 벽 #0 은 지지 6,599점·법선 6.4° 차). 원점 MapAnything (3.089, 0.528, 3.187)(카메라 중심 평균 (0.471, −0.887, 1.895) 의 수선 발). 카메라 높이(모델 단위) A 2.077·B 2.086·C 2.022·D 2.007. 스케일 MODEL_ESTIMATE 1.0. +X 밖 바닥 점 0%, 사분면 +X+Y 34%·+X−Y 66%. 바닥 XY 2~98 백분위 (0.60, −3.80)~(6.31, 4.96) m: 바닥이 벽 평면에서 0.6 m 안쪽부터 보인다(벽 앞 난방기·책상) |
| w2 (map-anything-apache) | `FLOOR_NOT_FOUND`: 가장 나은 바닥 후보(지지 36,484점)의 아래 점 비율 3.08% 가 한도 3% 를 조금 넘음. 카메라 간 재투영 중앙값 4.8~7.4 모델 px(원본 21~33 px). 바닥 찾기 개선은 이 티켓 밖 |
| w1 + 임시 값(선택 경로 확인용: 두 점 cam_C 원본 (430, 950)·(1600, 1090) 바닥 카펫, 높이 2.70 m × 4) | OK, `MANUAL_FLOOR_POINTS`(벽 한 방향보다 우선, 벽 축과 −35.6° 라 경고 — 임시 점이 벽과 나란하지 않음). 카메라 높이(모델 단위) A 2.077·B 2.086·C 2.022·D 2.007 → 비 1.300·1.294·1.335·1.346, 퍼짐 2.1%(통과), 스케일 1.3174(`MEASURED_CAMERA_HEIGHT`, 모델 추정 1.0 과 32% 불일치로 기록). 두 점 MapAnything (2.311, 0.523, 3.359)·(−0.106, 0.298, 4.352), 각 41.9°·44.4°, 이웃 점 바닥 위 +0.011·−0.011, 거리 3.455 m. 바닥 사분면 비율 +X+Y 62%·+X−Y 27%·−X+Y 10%·−X−Y 1%. 보정 문서 카메라 높이 2.737·2.748·2.664·2.644 m(같은 높이를 넣어도 카메라별로 다른 것은 모델 카메라 높이의 퍼짐), 바닥 XY 2~98 백분위 (−1.6, −2.3)~(7.2, 8.4) m. 임시 값이라 정확도 판단은 하지 않는다 |

모델 추정 스케일이 임시 높이 대비 약 24% 작다(카메라 2.0~2.1 모델 단위). 실측 높이를 받으면 이 차이를 다시 본다.

**버린 대안.**

- **카메라 배치 기준 축(`CAMERA_LAYOUT`)**: 카메라 중심을 바닥에 내려 축까지 정하면 사람 입력이 없지만, 카메라를 옮기거나 하나를 바꾸면 축이 돌아 저장된 구역이 어긋난다(4.1 "좌표계는 바꾸지 않는다"). 계약 값만 예약했다. `WALL_AND_FLOOR` 는 축을 벽(영구 구조)에서 얻고 원점 한 성분(벽을 따른 Y)만 카메라 배치에 맡겨 그 위험을 줄였다.
- **벽 한 방향에서 원점을 벽 끝(모서리 근처)으로**: 벽의 끝은 책상·커튼·가려짐에 따라 RANSAC 지지 점 범위가 달라져 재현되지 않는다. 카메라 중심 평균은 설치가 그대로면 같은 값이다.
- **벽 한 방향이면 늘 두 점을 요구**: 사용자 결정(실측·기준점 설치는 가능하면 피함)에 따라 자동 경로를 두고 두 점은 선택으로 남겼다.
- **MANHATTAN(주된 방향 자동 추정) 단독**: 점구름 법선·직선의 주된 방향으로 X·Y 를 정하면 90° 모호성(어느 쪽이 X 인지, 부호)이 남고, 비스듬히 놓인 책상 배치가 주된 방향을 끌어간다. 원점도 정하지 못한다. 이 티켓에서는 구현하지 않았다(Lead: 선택, 하더라도 기록만).
- **두 점이 무효하면 벽 경로로 대신하기**: 사람이 준 기준과 다른 축이 조용히 나와 재현성이 깨진다. 비우고 사유를 낸다.
- **펴진 K' 로 광선 만들기**: MapAnything 점구름(바닥 평면)은 모델 출력 K 로 만들어졌다. 같은 K 를 써야 찍은 픽셀의 광선이 그 픽셀의 점과 같은 규약이다. K' 광선은 확인값으로만 기록한다.
- **두 점 최소 거리 0.5 m(Lead 예시)**: 위 표대로 축이 3° 넘게 흔들린다.

**잔여 위험.**

- `WALL_AND_FLOOR` 원점은 카메라 배치에 따른다: 카메라를 벽을 따라 옮기거나 한 대를 바꾸면 원점이 Y 로 움직인다(축은 같다). 그때는 좌표계 이전(4.1)으로 기록하고 구역을 변환하거나 두 점(선택 입력)으로 고정한다.
- `WALL_AND_FLOOR` 축은 '지지 점이 가장 많은 경계 벽'이다. w1 처럼 나란한 경계 평면이 둘(창문 벽과 그 앞 0.2 면)이면 다시 돌렸을 때 다른 평면이 뽑힐 수 있다. 방향은 같은 묶음(±10°)이라 축 차이는 그 각도(w1 두 평면 사이 약 6.4°, 묶음 허용 10° 안)이고, 원점 X 는 두 평면 간격만큼 달라진다.
- 찍은 점의 바닥 확인은 MapAnything 깊이에 기대므로, 바닥에 놓인 낮은 물체(가방·전선)나 깊이가 바닥처럼 잘못 나온 곳은 통과할 수 있다. 원점 위치가 그만큼 틀린다(축 방향은 거의 같다).
- 사람이 고른 두 점이 실제 교실 기준(예: 앞 벽과 나란한 줄)과 맞는지는 이 도구가 확인하지 않는다. 같은 점을 다시 찍을 수 있게 기준점을 바닥에 표시해 두는 것을 권한다(재보정 때 같은 World).
- 카메라 높이 축척은 모든 카메라의 MapAnything 높이가 같은 방향으로 틀리면(바닥 평면 자체가 틀림) 그 오차를 그대로 옮긴다. 실측 정물이 있으면 정물이 이기고 카메라 높이는 교차 확인이 된다.
- 렌즈 중심 위치는 하우징 밖에서 정확히 알기 어렵다(±1~2 cm). 퍼짐 한도 3% 에 포함했다.
- w2 처럼 바닥을 못 찾으면 두 점·높이도 쓸 수 없다(바닥 찾기 개선은 범위 밖).

**넘긴 일.**

- 노트북 윗판(35.5 × 25.5 cm, 교실에 같은 노트북 여러 대)을 실측 물체로 쓰는 축척은 -100 이 `world_frame.cli measure-objects`(`--scale-evidence` 입력을 만든다)로 했다(`docs/design/measured-object-scale.md`). w1(518)에서는 원본 윤곽 방법으로 노트북 4대가 1% 안에 모여 MEASURED_OBJECT 1.028(MODEL_ESTIMATE 와 5% 안, 카메라 높이 2.06~2.15 m)이지만, 한 대 값이라 공통 깊이 치우침을 배제하지 못해 카메라 높이 실측으로 교차 확인하기 전까지 잠정 근거다.
- (선택) 카메라 높이를 잴 수 있으면 w1 로 다시 `fix` 해 모델 축척 차(임시 2.70 m 기준 약 24%)를 확인.
- -214 번들 조정 보정 문서의 scale_basis 는 아직 번들 쪽 근거(없으면 MODEL_ESTIMATE)다. World 고정의 스케일 근거(카메라 높이·정물)를 번들 게이지·문서로 넘기는 일.
- MANHATTAN 주된 방향을 기록용으로 더하기(판정 미사용), 원본 스틸 위에 두 점을 찍는 작은 도구(지금은 그림 뷰어로 픽셀을 읽어 JSON 을 쓴다).
- w2 바닥 아래 점 비율 3.08% 사례로 바닥 한도를 다시 볼지(바닥 찾기 개선 티켓).

## 3차 GPU 세션 큐 항목

`session3_jobs.py` 가 gpu_queue Job 파일을 만든다(`gpu_queue.py check` 통과). `gpu_session1.py run --exp-jobs` 로 MapAnything 이미지 Pod 에서 m 항목 뒤에 LIVE 보호를 켠 큐로 돈다.

| Job | 가중치 | 명령 |
|---|---|---|
| w1_mapanything_known_k_default | map-anything (CC-BY-NC-4.0, 비상업: 교육 시연 가능, 공개 산출물에 표기) | `exp_job.py --out <root>/runs/exp01/CAL102_w1_… -- run_mapanything.py --mode prepared --prepared <입력 폴더> --model <root>/weights/map-anything` |
| w2_mapanything_known_k_apache | map-anything-apache (Apache-2.0) | 같은 입력, `--model <root>/weights/map-anything-apache` |

Run 기록은 운영 경로 검증 `VAL-01`(e_gate 없음), jira S15P31A101-102. 세션 전 노트북에서: (1) `prepare` 로 입력 폴더를 만들고(`ai/experiments/recordings/mapanything-inputs/…`, 커밋 안 함) Pod `datasets/` 에 올린다, (2) `session3_jobs.py --repo <Pod 저장소 사본> --prepared <Pod 경로> --session <이름> -o …`, (3) `gpu_queue.py check`. 세션 뒤: 결과 묶음을 노트북으로 가져와 두 Run 각각 `fix` 를 돌려 바닥·벽·축 근거와 카메라 간 재투영을 비교하고 하나를 고른다(공식 벤치마크로 되는 비교가 아니라 우리 교실 입력 비교).

## 개발 확인

**합성 교실(SIM, 상한, 제품 성능 아님).** 8 × 6.5 × 2.9 m 상자 방, 모서리 카메라 4대(높이 2.55~2.65 m, 가로 85°, 모델 해상도 224×126), 책상(0.75 m)·사람 상자, MapAnything world = cam_A 축 × 1.04.

| 경우 | 결과 |
|---|---|
| 기본(스케일 근거 없음) | 바닥 Z 오차 < 1 cm, 회전 직교·오른손, 원점 = 방 모서리 하나(3 cm 안), 방 +X·+Y, 카메라 높이 = 정답 × 1.04(MODEL_ESTIMATE 그대로), 0.2 s |
| 실측 정물 2개(배율 1/1.04) | 카메라 높이 정답 ±2 cm, MODEL_ESTIMATE 가 교차 확인(4% < 5%) |
| 카메라 간 재투영(정확한 장면) | 중앙값 0.67~0.71 모델 px(픽셀 반올림), p95 3.5~4.4 px(스치는 각도) |
| 바닥 안 보임(책상 보임) | FLOOR_NOT_FOUND, 변환·문서 없음. 책상 윗면은 아래 점 비율로 거름 |
| 한 방향 벽만 | AXES_AMBIGUOUS(ONE_WALL_DIRECTION) → -277 부터 `WALL_AND_FLOOR` OK |
| 60° 벽 / 직교 오차 10° / 10.5° | 실패 / 받음 / 실패 |
| 발 접지점 +1 cm / +20 cm / 5개 / 절반 −10·절반 +10 cm | CONSISTENT / FLOOR_FOOT_MISMATCH / NOT_CHECKED / FLOOR_FOOT_MISMATCH(부호 중앙값 0) |
| 바닥 후보 둘(진짜 바닥 + 방 가운데에서 0.15 m 높고 원점 쪽으로 4° 기운 면) | 진짜 바닥(원점 offset 으로 고르면 기운 면이 뽑히는 반례) |
| 천장 4° 기울기 | 천장 높이 2.9 m(카메라 기준) |
| 평행사변형 방 ±9°·0°, 벽 지지 순서 3가지 | 둔각 모서리 → AXES_AMBIGUOUS(QUADRANT), 예각·직각 → OK 이고 바닥 점 95% 이상 +X·+Y |
| 내부 파라미터만 SIM(영상 FILE) / 스케일·발만 SIM | 결과 calibration·provider_source SIM, is_upper_bound true, -214 가 읽음 |
| 사람 마스크 | 사람 상자 안 점 0개(마스크 없으면 100개 넘게 들어옴) |

**2차 GPU 세션 미리보기(경로 점검, 정확도 판단 안 함).** `CLS_classroom4_mapanything_20261022`(왜곡 안 편 사진 4장, images 모드, 사람 마스크 없음)로 `fix` 를 돌렸다(결과는 scratchpad, 커밋 안 함). 결과 FAILED `AXES_AMBIGUOUS`(WALLS_NOT_ORTHOGONAL): 바닥은 찾았고(지지 10,070점, rms 0.015, 상방 사전값과 8.7°, 그 위 0.11 높이에 두 번째 바닥 후보 — 휜 바닥), 경계 벽 6개가 모두 한 방향 계열인데 서로 11° 어긋나 두 묶음으로 갈렸고, 그와 수직인 벽 후보 2개는 바닥 점이 16%·27% 밖이라 경계 벽이 아니었다. 왜곡을 펴지 않은 입력에서 평면이 휘는 것과 맞는 결과다. 시험 삼아 경계 조건을 30% 로 풀면 고정은 되지만 바닥 점 13% 가 사분면 밖이고 카메라 높이 3.1~3.3(모델 단위)이라 기본값을 바꾸지 않았다. 카메라 간 재투영은 중앙값 2.3~9.8 모델 px(원본 10~44 px). 정식 수치는 3차 세션(known-K·마스크) 결과로 낸다.

**교실 사진 준비(노트북 CPU).** classroom-stills-20261021 의 `cam_*_01` 4장 → `prepare` 33 s. 펴진 K fx 1161(원래 1259, 같은 렌즈 공통값이라 4대 같음), 사람 5·19·23·0명, 마스크 2.5·10·12·0%. 결과 폴더는 확인 뒤 지웠다(사람이 찍힘).

## 버린 대안

- **벽 소실점으로 축 정하기**: 펴진 영상의 직선(`intrinsics.lines`)으로 소실점을 잡을 수 있지만 그 방향을 3D 로 올리려면 카메라 자세가 필요하고, 점구름 벽 평면이 같은 정보를 3D 로 바로 준다. 영상 쪽 교차 확인은 넘긴 일.
- **바닥을 발 접지점으로 정하기**(명세 4.3 2단계 문구): 지금 교실 녹화(dev_test)는 여러 명이 함께 보여 -214 한 명 걷기 대응이 안 되고, 접지점을 MapAnything world 로 올리는 단계도 없다. MapAnything 바닥을 쓰고 발 접지점은 있으면 확인(어긋나면 고정 안 함)으로 두었다. 3단계 번들 조정이 발 접지점으로 바닥을 다시 확인한다(4.3).
- **사람을 MapAnything 입력 영상에서 지우기(칠하기·인페인팅)**: 모델이 보는 장면이 바뀌어 깊이·자세가 달라질 수 있다. 결과 점에서 뺐다. 5장 시간 중앙값 배경도 같은 이유와 앉은 사람이 남는 문제로 쓰지 않았다.
- **원점 모서리 고정 규칙(카메라 번호 기준 등)**: 모서리 4대 배치에서 카메라 가까운 모서리는 그 카메라에 거의 안 보인다. 가장 잘 보인(지지 점이 많은) 벽 쌍을 골랐다.
- **카메라 간 재투영을 계약 `reprojection_error_px` 에 넣기**: 판정·버전 비교(`_worst_median`)가 관측 재투영과 섞을 수 있어 근거 파일에만 둔다.
- **Pod 에서 마스크를 적용**: MapAnything 이미지에는 simai_ai 가 없고 시험도 노트북에서 해야 해서 노트북 `fix` 한 곳에서만 적용한다.

## 잔여 위험

- 벽에 붙은 사물함·칠판·커튼은 벽과 거의 같은 평면이라 경계 벽으로 남는다. 그 면이 지지 점이 가장 많으면 원점이 실제 벽에서 그 두께만큼(수 cm~수십 cm) 들어온다. 축 방향은 같다.
- 원점 모서리 선택(벽 경로)은 데이터(지지 점 수)에 따른다. World 는 첫 CALIBRATED 때 한 번 정하므로 그 결과의 모서리를 기록으로 고정하고 다시 돌리지 않는다(4.1). 사람이 원점을 정하려면 바닥 두 점(-277)을 준다.
- 길이 한도가 MapAnything 단위 기준이라, 모델 스케일이 크게(예: 30% 이상) 틀리면 한도 의미가 달라진다(미리보기 metric_scaling_factor 3.3 은 내부 값이며 출력은 미터 추정).
- 카메라 id 는 사진 이름의 `cam_A`~`cam_D` 다. 서버 시드(`cam1`~`cam4`)와의 대응은 이 도구 밖이다(-214 와 같음).
- 마스크 경계: 팽창 15 px 로 덮지만 앉은 사람과 책상이 붙은 곳은 책상 점도 일부 빠진다(바닥·벽 추정에는 영향 작음).
- 미리보기에서 바닥 후보가 두 층(0.11 차)으로 나왔다. 펴진 입력에서도 남으면 바닥 허용 거리·가장 낮은 평면 규칙을 다시 본다.

## 넘긴 일

- 3차 GPU 세션: 위 두 Job, 결과로 `fix` 정식 수치(바닥·벽·축 근거, 카메라 간 재투영), 기본·apache 비교·선택.
- 명세 반영(-172): 4.3 2단계 바닥의 근거(MapAnything 바닥 + 발 접지점 확인), 19.1 "World 원점·축 자동 선택 규칙"을 위 6번 규칙으로 닫을지, 4.1 "벽 소실점" 문구.
- 발 접지점을 MapAnything world 로 만드는 단계(-214 keypoint 삼각측량 또는 -215 재생 출력), 스케일 근거 생산(-100 정물, 키 아는 사람).
- Pod 산출물(`fused.ply`·`pointcloud.glb`)에서 사람 빼기, 정적 3D(명세 5.3 Scene)는 CALIBRATED 뒤 별도.
- 펴진 영상 직선으로 벽 방향 교차 확인(소실점).
- 사람 마스크 좌표를 실제 MapAnything 전처리(`preprocess_inputs`)와 나란히 돌려 비교하는 시험(지금은 같은 규칙을 옮긴 자체 공식과 비교한다. map-anything 이 있는 환경, 3차 세션에서 확인).
