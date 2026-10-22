# MapAnything 초기 추정과 바닥·벽으로 World 좌표계 고정 설계

S15P31A101-102. 기획서 §4.1(World 좌표계), §4.3 1·2단계(MapAnything 초기 추정, 좌표계 고정, 스케일 우선순위), §4.4(PROVISIONAL), §11.5(영상), §20.4(출처·상한), 기획서 수정 6(실측 평면도 없이 첫 CALIBRATED 때 바닥·벽으로 World 를 정하고 바꾸지 않음) 기준이다.
코드는 `ai/src/simai_ai/calibration/world_frame/`, MapAnything 실행 쪽은 `ai/experiments/static_scene/run_mapanything.py`(`--mode prepared`)·`session3_jobs.py`, 시험은 `ai/tests/calibration/world_frame/`·`ai/tests/experiments/static_scene/` 에 있다.
외부 파라미터 도구(-214, `docs/design/extrinsics-from-recording.md`)의 `--world-alignment` 입력을 이 티켓이 확정했다.

## 흐름

| 단계 | 어디서 | 도구 | 출력 |
|---|---|---|---|
| 1 입력 준비 | 노트북 CPU | `world_frame.cli prepare --image CAM=사진 … --intrinsics <AnyCalib 기록> --segmenter yolo26x-seg.pt …` | 입력 폴더(`simai.mapanything_inputs`): 왜곡 편 영상, 펴진 K, 사람 마스크 PNG, `prepared.json` |
| 2 MapAnything | GPU 세션(MapAnything 이미지 Pod) | `gpu_session1.py run --exp-jobs <session3_jobs.py 출력>` → `run_mapanything.py --mode prepared` | `cameras.json`, `raw.npz`, `metrics.json`(기본·apache 둘) |
| 3 World 고정 | 노트북 CPU | `world_frame.cli fix --run-dir <결과> --intrinsics <같은 기록> --prepared <같은 폴더> [--scale-evidence] [--foot-points]` | `simai.world_alignment` 결과(아래) |
| 4 정밀화 | 노트북 CPU | `extrinsics.cli adjust … --cameras-json <같은 결과>/cameras.json --world-alignment <3 결과>` (-214) | 번들 조정, 보정 버전 제안 |

**known-K.** AnyCalib 기록(`simai.intrinsics_estimate`)을 -214 와 같은 `bundle_init_from_record` 로 읽어 카메라별 K·[k1, k2] 로 사진을 편다. 펴진 K 는 `cv2.getOptimalNewCameraMatrix`(alpha 0, 주점 가운데, 같은 크기)다. 펴기 표가 원본 밖을 읽으면 오류다(검은 테두리를 장면으로 넣지 않는다). 펴기는 회전을 바꾸지 않으므로 MapAnything 이 낸 자세(cam2world)는 원래 카메라의 자세 그대로이고, 보정 문서의 K·왜곡은 원래(왜곡 있는) 기록 값이다. `run_mapanything.py --mode prepared` 는 펴진 영상과 펴진 K(원본 해상도)를 `intrinsics` 로 넣고 pose 는 넣지 않는다. MapAnything 전처리가 K 를 모델 해상도로 옮긴다.

**사람 마스크.** 펴진 영상에서 YOLO seg(person) 마스크의 합을 15 px 팽창한다(2304 폭의 0.65%, 경계 흐림·팔다리 끝). MapAnything 입력 영상은 지우지 않고(모델이 보는 장면을 바꾸지 않음) `fix` 가 마스크를 MapAnything 전처리 규칙(map-anything @3d10cf7 `utils/cropping.py` `crop_resize_if_necessary`: 가로·세로 비 중 큰 쪽으로 덮게 줄이고 가운데를 자름)으로 모델 해상도로 옮겨, 조금이라도 덮인 픽셀의 점을 뺀다. Pod 의 `fused.ply`·`pointcloud.glb` 에는 사람이 남는다(이 티켓은 쓰지 않음, 아래 넘긴 일).

## World 고정 규칙 (`frame.py`)

X_world = s · Ra · (X_mapanything − o). 길이 한도는 MapAnything 단위(모델 추정 미터)다.

1. **상방 사전값**: 카메라 영상 위쪽(−y) 방향의 평균. 바로 선 설치 전제(`check_cameras_upright` 와 같음).
2. **바닥(Z = 0)·중력**: 사전값과 30° 안인 평면을 RANSAC 으로 찾고 법선을 상방 쪽으로 돌린다. 모든 카메라가 0.5 m 넘게 위이고, 평면 아래 0.10 m 넘게 떨어진 점이 3% 이하인 평면 중 가장 낮은 것. "가장 낮다"는 카메라 중심 평균에서 잰 카메라 높이가 가장 큰 것이다(MapAnything 원점 기준 offset 으로 비교하면 원점이 첫 카메라 = 방 가장자리라, 법선이 몇 도 다른 평면끼리 수평 팔 길이만큼 높낮이가 뒤바뀐다). 천장 높이 기록도 같은 기준점에서 잰다. RANSAC 다듬기는 인라이어가 줄면 이전 평면을 유지한다. 없으면 **바닥을 비운다**(`FLOOR_NOT_FOUND`, 변환·보정 문서 없음, Z = 0 을 지어내지 않음). 중력은 바닥 법선의 반대.
3. **발 접지점 확인**(입력이 있을 때): 접지점과 바닥의 **절대** 거리 중앙값 median(|d|) ≤ 0.05 m 이고 |d| 90 백분위 ≤ 0.15 m 가 아니면 실패(`FLOOR_FOOT_MISMATCH`). 부호 중앙값은 위·아래 오차가 섞이면 0 으로 상쇄되므로 편향 진단값(`signed_median_distance_ma`)으로만 기록한다. 10개 미만이면 확인하지 못함(`NOT_CHECKED`, 실패도 확인도 아님).
4. **경계 벽**: 바닥 위 0.3 m 보다 높은 점에서 상방과 수직(±10°)인 평면. 바닥 점이 거의 모두 한쪽(밖 0.10 m 넘는 점 5% 이하)이고 벽 방향 길이 1 m 이상인 것만 경계 벽이다. 법선은 방 안쪽.
5. **축**: 경계 벽을 방향 묶음(평행 ±10°)으로 묶어 지지 점이 많은 순서로 서로 수직(90 ± 10°)인 두 묶음을 고른다. 묶음이 없거나(`NO_WALL`) 하나뿐이거나(`ONE_WALL_DIRECTION`) 수직 쌍이 없으면(`WALLS_NOT_ORTHOGONAL`) **축이 모호해 실패**(`AXES_AMBIGUOUS`)다. 세 방향 이상이면 경고.
6. **원점**: 두 묶음에서 각각 지지 점이 가장 많은 벽과 바닥, 세 평면의 교점(방 모서리 바닥). X·Y 는 두 벽의 방 안쪽 법선이고 X × Y = +Z(오른손)가 되게 어느 벽이 X 인지 정한다. 두 벽의 직교 오차는 Y 가 흡수한다(기록 `y_vs_wall_deg`). 직교화한 축에서 바닥 점이 0.10 m 넘게 −X·−Y 쪽인 비율이 5% 를 넘으면 실패(`AXES_AMBIGUOUS`, QUADRANT)다: 원점 모서리가 둔각(안쪽 법선 사이 < 90°)이면 직교 축으로는 방이 음의 쪽으로 넘어간다. 예각 모서리는 방이 사분면 안에 든다. 기준은 경계 벽의 밖 점 기준과 같다(같은 "방 경계" 뜻). 그래서 성공한 결과의 방은 +X·+Y 쪽에 있다.
7. **스케일**(`choose_scale`, 명세 4.3 우선순위): 실측 정물(정물별 평균 → 정물끼리 평균, 정물끼리 2% 넘게 어긋나면 그 근거를 버리고 다음으로) > 키를 아는 사람 > 사람 키 통계(표본 최다) > 표준 사물 > 모델 추정(MapAnything 메트릭 = 1, 언제나 마지막 근거). 고른 값과 5%(CALIBRATED 교차 확인 허용치) 안인 다른 근거를 교차 확인으로, 나머지를 불일치로 기록한다. 지금 노트북 입력은 `--scale-evidence`(배율 = 실제 / MapAnything 길이)이고 근거를 만드는 쪽(-100 정물 검출, 사람 키)은 이 티켓 밖이다.

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

## 결과 파일 (`simai.world_alignment` 0.1.0)

-214 `load_world_alignment` 가 읽는 형식에 근거를 더했다(계약이 아닌 도구 형식, contracts/ 변경 없음).

- `status` OK/FAILED, `reason`·`detail`. OK 일 때만 `mapanything_to_world`(4×4 [sR | T])와 `calibration`(계약 calibration 2.0.0 문서)을 싣는다. FAILED 결과는 -214 가 거절한다(`load_world_alignment`, 임시 정렬로 바꾸지 않음).
- `cameras_json_sha256`: 이 변환을 만든 cameras.json. -214 `adjust` 는 `--cameras-json` 과 다르면 거절한다(다른 실행의 MapAnything world 는 원점·축이 다르다).
- `calibration`: state **PROVISIONAL**(독립 검증점이 없다, 4.4), provider_source G1_AUTO(입력 영상·근거 중 SIM 이 있으면 SIM, DATASET_GT 면 DATASET_GT → CALIBRATED 불가, -214 와 같은 규칙), scale_basis·scale_cross_check_basis 는 위 스케일 선택, K·왜곡은 내부 파라미터 기록, R·t 는 MapAnything 자세 × World 변환, validation 없음, `check_calibration`(스키마·기하) 통과가 조건(못 하면 FAILED `CALIBRATION_CHECK`).
- `input_provenance`: 입력 폴더(영상)·내부 파라미터 기록·스케일 근거·발 접지점의 출처를 -214 `combine_input_provenance` 로 합친다. frames 는 입력 폴더 출처(교실 사진 FILE `classroom-stills-…`), calibration 은 MODEL_ESTIMATE `mapanything-<run_id>`(-214 와 같은 값)이고 입력 중 하나라도 SIM·DATASET_GT 면 calibration 이 그 출처(SIM 우선)가 되어 `is_upper_bound` 가 출처와 함께 true 다(`check_input_provenance` 통과, -214 `load_world_alignment` 가 그대로 읽고 이어 쓴다). 어느 입력이 상한인지는 `upper_bound_inputs`. provider_source 도 그 출처라 G1_AUTO 로 바뀌지 않는다.
- 연결 확인(종료 2, 같은 카메라 이름의 다른 입력을 섞지 않게): 입력 폴더의 `intrinsics.sha256` 이 `--intrinsics` 와 다름, MapAnything `metrics.json` 의 `prepared_sha256` 이 `--prepared` 의 prepared.json 과 다름, prepared 모드 실행(`prepared_sha256` 또는 `args.mode` prepared)인데 `--prepared` 없음(마스크·출처·내부 파라미터 확인이 빠져 SIM 입력이 G1_AUTO 로 바뀜), `--prepared` 를 줬는데 그 폴더로 돈 기록이 없음(images 모드·metrics.json 없음: 왜곡 안 편 깊이에 펴진 영상 마스크가 어긋남). -214 `adjust` 도 결과의 `inputs.intrinsics.sha256` 이 `--intrinsics` 와 다르면 거절한다.
- `evidence`: 바닥(법선·지지·rms·사전값과의 각도·중력·발 확인)과 바닥 후보 목록, 벽 목록(경계 여부·밖 비율·길이·기울기), 축(묶음, 직교 오차, 규칙), 원점(MapAnything world 좌표·규칙), 스케일(후보·교차 확인·불일치·우선순위), 방(바닥 XY 2·98 백분위, 사분면 밖 비율, 카메라 높이, 천장 높이(모든 카메라보다 위인 수평면이 있을 때)).
- `reprojection`: **카메라 간 왕복 재투영**(카메라 i 의 점 → j 에 투영 → j 깊이와 5% 안이면 그 j 점을 다시 i 로, 처음 픽셀과의 거리). 모델 해상도 px 와 원본 px(원본 폭 비). 2D 관측 재투영 오차가 아니라 MapAnything 깊이·자세의 일관성이라 계약 `reprojection_error_px` 에는 넣지 않는다(관측 재투영은 -214 번들 조정이 보정 문서에 넣는다).

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
| 한 방향 벽만 | AXES_AMBIGUOUS(ONE_WALL_DIRECTION) |
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
- 원점 모서리 선택은 데이터(지지 점 수)에 따른다. World 는 첫 CALIBRATED 때 한 번 정하므로 그 결과의 모서리를 기록으로 고정하고 다시 돌리지 않는다(4.1). 모서리를 사람이 지정하는 입력은 없다.
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
