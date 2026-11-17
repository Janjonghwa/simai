# S15P31A101-787 맵 보강 MapAnything 에 고정 카메라 보정 자세 넣기

관련 명세: 4.3(세팅 맵), 5.2(Tier 0 위치 정본, 보조 촬영은 World·판정에 넣지 않음). 앞 문서는
`docs/design/S15P31A101-605-map-supplement-capture.md`(보조 촬영 흐름), `docs/design/S15P31A101-652-session14-ops.md`(관문 -652),
`docs/design/S15P31A101-760-walk-refine-extrinsics.md`(걷기 다듬기·정렬 기준 문서), `docs/design/final-map-pipeline.md` 3.1(-532:
기하·텍스처·뷰어는 같은 자세를 써야 한다)이다.

## 1. 원인

EC2 DB 실측. 11-17 16:13·16:26 에 맵 보강 다시 굽기(SUPPLEMENT_REBAKE)를 두 번 했고, 두 번 모두 보강 프레임 40/40 이 등록됐지만
FALLBACK / ALIGNMENT_WORSE 로 버려졌다.

| 항목 | 기준(보강 없음) | 보강 묶음 | 관문 |
|---|---|---|---|
| 중심 잔차 | 0.158 m | 0.125 m, 0.141 m | `align_max_center_m` 절대선 0.08 m → 실패 |
| 고정 카메라 재투영 | 6.27 px | 6.71 px, 7.06 px | 상한 8.33 px(= 6.27 × 1.25 + 0.5) → 통과 |

- 실패 때 기준 보정은 걷기 다듬기 판(`...-jba2-922a04f9-walk-10d38c63`, -760 은 11-16 15:11 머지)이다. 11-16 13:25 에 APPLIED 된
  건은 다듬기 전 보정(jba2)이었고 기준 잔차가 0.00001 m 였다.
- `supplement_capture` 의 관문은 MapAnything 카메라 중심을 보정 문서 중심에 닮음 변환으로 맞춘 잔차(`centers_only_residual` →
  `solve_fixed_alignment`)를 0.08 m 절대선으로 본다. 이 선은 "고정 4대가 보정과 거의 같다(Stage A 는 MapAnything 중심을 그대로
  쓴다)"는 가정 위에 있다.
- 걷기 다듬기 보정은 카메라 중심을 최대 20 cm(실측 최대 16 cm) 옮긴다. 세팅 대여 안에서는 `alignment_reference.json`(Stage A)이
  정렬 기준이라 문제가 없었지만, 다시 굽기 작업 폴더(`rebake`)에는 그 파일이 없고 `calibration.json` = 다듬은 보정이다(코드 확인:
  `site_setup_stages.rebake` 가 기준 보정 문서만 쓴다). 그래서 보강 없는 기준부터 0.158 m 이고, 보강이 잔차를 줄여도(0.125 m)
  0.08 m 를 넘어 늘 버려진다.
- 관문을 느슨하게 하지 않는다. 이 잔차는 MapAnything 기하와 보정(텍스처·뷰어 VDT 가 쓰는 자세)의 어긋남이고, 커지면 -532 처럼 색이
  섞인다.

## 2. MapAnything 이 view 마다 다른 입력을 받는가

받는다. 근거(이미지가 쓰는 커밋 `map-anything@3d10cf7` 원본을 받아 확인):

- README "Multi-Modal Inference": "supports any combination of inputs", 예시 view 0 = 이미지 + K, view 2 = 이미지 + K + 깊이 + 자세.
- `mapanything/utils/inference.py` `validate_input_views_for_inference`: view 마다 허용 키(`intrinsics`·`camera_poses`·
  `is_metric_scale` 등)를 따로 본다. view 사이 조건은 하나 — **자세를 준 view 가 하나라도 있으면 view 0(기준)에도 자세가 있어야 한다**.
  `preprocess_input_views_for_inference` 는 자세가 있는 view 만 `camera_pose_quats/trans` 로 바꾼다.
- `mapanything/models/mapanything/model.py`: 입력 자세는 view 0 기준 틀로 옮겨 쓰고(`camera_poses` "can be any world frame"),
  자세 없는 view 는 view 별 입력 마스크로 빠진다. 출력 세계 틀이 입력 세계라는 보장은 없다 → 사후 닮음 정렬은 그대로 둔다.
- EXP-01 C4b/C4c(`docs/experiments/EXP-01_design.md`): 자세를 넣으면 출력이 입력과 2~8 cm·0.3° 안, 이동량을 1.25 배 하면 출력이
  97~98% 따라간다(모델이 자세를 실제로 쓴다, 다만 소프트 조건).

보강 묶음은 고정 4 view 가 앞(view 0 = 고정 카메라)이라 이 조건을 늘 만족한다. `run_mapanything.prepared_views` 도 같은 조건을 먼저
검사해 분명한 오류로 멈춘다.

## 3. 바꾼 흐름

코드: `ai/src/simai_ai/scene/supplement_capture.py`, `ai/src/simai_ai/scene/run_mapanything.py`(prepared 모드),
시험: `ai/tests/scene/test_supplement_capture.py`.

1. 조건 자세 = `reference_poses(root)`: World 정렬(`solve_fixed_alignment`)이 맞추는 바로 그 문서 — `alignment_reference.json` 이
   있으면 그것(세팅 대여 = Stage A), 없으면 `calibration.json`(다시 굽기 = 기준 보정, 다듬은 판일 수 있음). R, t → cam2world(OpenCV, m).
   조건 자세와 정렬 기준이 같아야 정렬 잔차가 "모델이 자세를 얼마나 따랐나"를 잰다.
2. 입력 폴더(`build_joint_prepared`): 고정 view 마다 `pose_cam2world`(4×4) + 기존 `K_undistorted`, 보조 프레임은 이미지만(K·자세
   없음). 문서에 `pose_conditioning`(mode, views, source, calibration_version) 기록. `run_mapanything` prepared 모드는
   `pose_cam2world` 가 있는 view 만 `camera_poses` + `is_metric_scale=True` 로 넣는다(known 모드와 같은 규칙). metrics 에
   `pose_conditioned_views`.
3. 관문 기준도 같은 입력: 자세 조건 시도는 먼저 고정 4 view 만 자세 조건으로 MapAnything 을 한 번 돌려(`supplement/posed/baseline`)
   기준 중심 잔차·재투영을 잰다. 묶음은 `supplement/posed/chunk_XX`. 관문 `gate_chunk` 와 0.08 m 절대선, 재투영 상대 기준
   (×1.25 + 0.5 px)은 그대로다.
4. 자세 따름 지표(`pose_follow`): 입력·출력 모두 첫 자세 view 기준 상대 자세로 비교한 최대 중심 오차(m)·회전 오차(°)·카메라 쌍 거리
   비(출력/입력) 중앙값, `followed`(World 정렬 문턱 0.10 m·5° 와 같은 값, 기록용). 기준 실행(`attempts[0].baseline_pose_follow`)과
   묶음마다(`chunks[].pose_follow`) 남는다. 세계 틀과 무관하게 비교된다.
5. 폴백(단순): 자세 조건 시도에 받아들인 묶음이 없으면(자세를 못 만듦·기준 실행 실패·관문 실패 모두) 자세 없이(K 만, -605 원래
   동작, 작업 폴더의 고정 카메라 Run 이 기준) 한 번 더 돈다(`supplement/chunk_XX`). 단 자세 조건 묶음을 돈 뒤 시간 예산(600 s)을
   넘었으면 건너뛴다(`k_only_retry_skipped=TIME_BUDGET`). 실패는 요약(FALLBACK + 사유)으로만 바뀌고 세팅·다시 굽기는 실패하지 않는다.
6. 기록: 요약(계약 `supplement_summary`, 스키마 변경 없음)의 `alignment` 는 채택된(또는 묶음을 돌린 첫) 시도의 기준·묶음 값이다.
   `supplement/result.json` detail 에 `mode`, `attempts[]`(시도별 기준·묶음·오류), `alignment_reference`(조건 자세 문서·버전),
   `k_only_baseline`(자세 없는 Run 과 정렬 기준의 잔차와 `exceeds_align_max_center_m`)를 남긴다. 그 값이 0.08 m 를 넘으면 워커 로그에
   "보정과 MapAnything 이 원래 어긋남(걷기 다듬기 보정 등)" 경고를 남긴다 → 11-17 같은 경우가 로그 한 줄로 보인다.
7. 병합(`materialize_world_run`): 조건 자세는 추론 입력일 뿐이라 병합 prepared view 항목에서 뺀다(World 자세 정본은 병합
   cameras.json). `world_alignment.json` `supplement.mode` 에 입력 방식을 남긴다. 기하(병합 Run 깊이·자세) = 보정 자세 근처이므로
   텍스처 굽기·뷰어 VDT 가 쓰는 보정 자세와 맞다(-532 교훈과 같은 방향).

## 4. 정한 수치와 근거

| 값 | 근거 |
|---|---|
| 관문 0.08 m·재투영 ×1.25 + 0.5 px | 바꾸지 않음(-652). 기준·묶음을 같은 입력으로 재므로 상대 비교가 공정해진다 |
| `pose_follow.followed` 0.10 m·5° | 새 수치를 만들지 않고 World 정렬 문턱(`ALIGN_MAX_CENTER_M`·`ALIGN_MAX_ROTATION_DEG`)을 그대로 씀. 판정이 아니라 기록 |
| `is_metric_scale=True` | 보정 문서는 미터(카메라 높이 실측으로 축척). known 모드·README 예시와 같다 |
| K 만 재시도 1회, 예산 600 s 안에서만 | 단순 폴백. 기존 시간 예산(`time_budget_s`)을 그대로 쓴다 |
| 기준 실행 1회 추가(고정 4 view) | 4 view 추론은 1 s 안팎(EXP-01 C4b 755 ms)이고, 모델 적재 시간이 더 크다(아래 잔여 위험) |

## 5. 버린 대안

- **관문 완화(0.08 → 0.16 m 등)**: 잔차가 곧 기하와 텍스처 자세의 어긋남이라 색 섞임(-532)을 받아들이는 것이다.
- **상대 관문(기준 대비 개선이면 통과)**: 기준이 0.158 m 처럼 이미 나쁘면 "덜 나쁨"을 통과시켜 같은 어긋남이 맵에 들어간다. -652
  가 상대 2 cm 를 절대선으로 바꾼 이유(기준 ≈ 0)와 반대 상황이라 기준값을 신뢰할 수 없다.
- **다시 굽기에도 `alignment_reference.json`(Stage A) 전달**: 보강 정렬은 통과하겠지만 맵이 다듬기 전 자세 기준이 되어 다듬은
  calibration.json(Tier 0·뷰어)과 최대 16 cm 어긋난다. 서버·계약(`rebake_base`)도 바꿔야 한다.
- **보조 프레임에도 자세·K**: 손 기기 자세·K 는 모른다(지어내지 않는다, 규칙 2).

## 6. 잔여 위험

- MapAnything 은 자세를 소프트 조건으로 쓴다(C4b 2~8 cm). 보정과 이미지 내용이 크게 어긋나면(다듬은 자세가 MapAnything 고유 기하와
  16 cm 다름) 모델이 자세를 덜 따르거나 깊이가 나빠질 수 있다. -539 실측에서 WorldMirror 2.0 에 보정 자세를 조건으로 넣은 방식 2 는
  공관측 표면 불일치 9.47 cm 로 실패했다(`final-map-pipeline.md` 9.4). MapAnything 도 같은 경향일 수 있다 → `pose_follow`·기준
  재투영으로 확인해야 하고, 나쁘면 관문이 막고 K 만 재시도로 간다(그 경우 다듬은 보정에서는 여전히 FALLBACK).
- 재투영 관문은 같은 입력 기준끼리 비교하지만, 자세 조건 기준 자체의 재투영이 자세 없는 기준보다 나빠도 통과할 수 있다(상대
  비교라서). `attempts[0].baseline_fixed_reprojection_px` 와 `k_only_baseline.fixed_reprojection_px` 를 같이 봐야 한다.
- `run_mapanything.main` 은 호출마다 모델을 적재한다. 자세 조건 기준 1회 + (실패 때) K 만 묶음 1회가 더해져 보강 단계 시간이
  늘어난다(단계 제한 1200 s, 예산 600 s 는 묶음 추가만 막는다).
- 시험은 가짜 MapAnything 이다(자세를 완전히 따름/완전히 무시/거부 세 가지). 실제 부분 추종은 GPU 에서만 확인된다.
- 맵 정렬 바닥 제약(`floor_constraint`)이 높이를 2 cm 안팎 당긴다(합성 시험 확인, 기존과 같음).

## 7. 다음 GPU 세션 검증

1. 11-17 실패 세션의 보강 파일(같은 SUPPLEMENT_REBAKE 입력: 기준 보정 `...-walk-10d38c63`, 굽기 입력 사진, 보강 동영상)로 다시 굽기.
2. 볼 것(`supplement/result.json`·워커 로그):
   - 요약 `status` APPLIED 여부, `alignment.baseline_center_residual_m`(자세 조건 기준, 기대 ≤ 0.08 m)·`supplement_center_residual_m`.
   - `detail.k_only_baseline.center_residual_m` ≈ 0.158 m(원인 재현), `exceeds_align_max_center_m` true, 경고 로그 한 줄.
   - `detail.attempts[0].baseline_pose_follow`: `max_center_err_m`(기대 ≤ 0.08)·`distance_ratio_median`(기대 0.97~1.03)·`followed`.
   - 재투영: 자세 조건 기준 vs `k_only_baseline.fixed_reprojection_px`(6.27 px) — 자세 조건이 크게 나쁘면(예 > 8.33 px) 깊이를 해친 것.
   - `metrics.json`(`supplement/posed/baseline/mapanything`) `pose_conditioned_views` = 고정 4대, `intrinsics_check` OK.
3. 맵 품질: 새 Scene 과 기존 Scene 을 뷰어에서 비교(상판·벽 색 섞임, 바닥 높이, 보강 영역이 실제로 채워졌는지), 공관측 상판 높이 차.
4. 다듬기 전 보정(jba2) 세션으로도 한 번: 예전처럼 APPLIED 이고 잔차가 0.08 m 안인지(회귀 확인).
5. APPLIED 가 안 되면 `attempts[]` 로 자세 조건(관문 사유·자세 따름)과 K 만 결과를 갈라 보고 다음 조치(관문이 아니라 입력 쪽)를 정한다.
