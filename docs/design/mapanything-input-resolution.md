# MapAnything 입력 해상도 비교 (S15P31A101-282)

관련: 명세 4.3 1단계(MapAnything 초기 추정), 20장 EXP-01. 코드: `ai/experiments/static_scene/run_mapanything.py`(`--resize-mode`·`--size`), `session3_jobs.py`(`--resolutions`), `compare_resolutions.py`, `intrinsics_check.py`(출력 K 검사, S15P31A101-312). 시험: `ai/tests/experiments/static_scene/test_input_resolution.py`, `test_intrinsics_check.py`.

> **4차 세션(2026-10-28) r1036·r1554·rfull 결과는 무효다.** 모델이 출력 K 를 세로로 틀리게 예측했다(아래 "4차 세션 비교 무효"). 해상도 비교 결론은 이 결과로 내지 않는다.

## 왜
3차 GPU 세션(2026-10-26, H100) w1 은 공식 기본 전처리(`fixed_mapping`, 긴 변 518)로 돌아 모델 입력이 518×294 였다. 원본(펴진) 2304×1296 의 4.45 px 가 점 하나이고, 점구름이 화면 배경으로 쓰기엔 성기다. VRAM 최대 6.4 GB 로 H100 에 여유가 크다. map-anything @3d10cf7 의 `load_images`·`preprocess_inputs` 는 `resize_mode`(fixed_mapping·longest_side·fixed_size·square)·`size` 를 받는다.

## 정한 것
- 보정용(자세·바닥·벽: 바닥 지지 점·rms, 카메라 간 왕복 재투영)과 배경용(원본 px/점, 유효 비율, 점 수, 가장자리 선명도는 패널로 눈으로)을 나눠 고른다. 학습 해상도(518) 밖이라 높을수록 낫다고 가정하지 않는다(ViT 위치 정보 보간으로 깊이가 흔들릴 수 있음).
- 비교 단계(2304×1296 기준 모델 입력): `r518` 518×294(기본) · `r1036` 긴 변 1036 → 1036×588 · `r1554` 1554×868 · `rfull` fixed_size 2304×1296 → 2296×1288(14 배수로 내림). 4장 토큰(14×14 패치)은 3,108 · 12,432 · 27,528 · 60,352.
- 같은 입력·가중치(w1 기본 map-anything, CC-BY-NC-4.0)만 쓴다. apache 판(w2)은 518 에서 바닥을 못 찾았으므로 해상도 비교에서 뺀다.
- `world_frame.cli fix` 의 사람 마스크 이동(`inputs.mask_to_model_res`)은 모델 해상도를 raw.npz 에서 읽고 같은 `crop_resize_if_necessary` 규칙을 쓰므로 해상도와 무관하다.

## 다음 GPU 세션 큐
```sh
python ai/experiments/static_scene/session3_jobs.py --root /srv/simai --repo <사본> --py /opt/simai/ai/.venv/bin/python \
  --prepared /srv/simai/datasets/classroom4-prepared-20261026 --dataset-id classroom-stills-20261026 \
  --session <세션> --resolutions r518,r1036,r1554,rfull -o exp-jobs-282.json
```
4차 세션(S15P31A101-291)은 같은 항목을 `session4_jobs.py` 가 `simai_bootstrap.py scene` 으로 감싸(static 가중치 받기·Tier 0 우선) EXP 큐에 넣는다(Run 기록 jira -282). r1554·rfull 은 예상 600 s·시한 2400 s. 노트북에서 각 Run 에 `world_frame.cli fix --out <Run>/world-fix` 를 돌린 뒤 `compare_resolutions.py <Run...>`.
3차 세션 w1·w2 로 확인한 표(518): w1 바닥 지지 48,858·rms 0.0115·재투영 중앙(카메라 중앙값) 14.2 px, w2 FLOOR_NOT_FOUND.

## 4차 세션 비교 무효: 원인과 수정 (S15P31A101-312)

**측정.** 같은 렌즈(펴진 K fx = fy = 1161, 2304×1296)인데 기본(r518) 밖 해상도 Run 의 출력 K(raw.npz `<cam>__intrinsics`)가 모두 세로로 틀어졌다.

| Run | 모델 입력 | 출력 fy/fx(cam_A) | 출력 K − 기대 K(4대 범위) | 비고 |
|---|---|---|---|---|
| 교실 r518 | 518×294 | 1.001 | fx −1.0~−0.2%, fy −0.9~−0.1% | |
| 교실 r1036 | 1036×588 | 0.571 | fx +19.9~+23.7%, fy −29.7~−31.9% | |
| 교실 r1554 | 1554×868 | 0.575 | fx +4.3~+7.0%, fy −39.2~−40.0% | |
| 교실 rfull | 2296×1288 | 0.583 | fx +7.5~+9.9%, fy −35.6~−38.6% | |
| 로비 r518 | 518×294 | 1.001 | fx −0.3~+1.9%, fy −0.1~+2.1% | 재투영 중앙 12~19 px |
| 로비 rfull | 2296×1288 | 0.599 | fx +0.3~+7.8%, fy −36.7~−40.5% | World 고정 FLOOR_NOT_FOUND, 재투영 중앙 29~73 px |

주점도 r1036 이상에서 cx −2.0~+6.2%, cy +2.1~+7.7% 밀렸다(r518 은 ±0.3%). 출력 세로 화각이 58° → 77~86° 로 커져 가로 화각(77~89°)과 거의 같아진다(정사각 그림처럼 예측). fy/fx 0.57 ≈ 1296/2304 인 이유다.

**원인: 모델(가설 ②).** 근거:
1. 라이브러리 전처리는 K 를 등방으로 옮긴다. map-anything@3d10cf7 `mapanything/utils/cropping.py` 의 `rescale_image_and_other_optional_info` 는 배율 하나(`scale_final = max(output / input)`)로 줄이고 `camera_matrix_of_crop` 이 `[:2, :] *= scaling` 한 뒤 가운데를 자른다. `preprocess_inputs`(`utils/image.py`)는 그 K 를 그대로 싣고, `infer` 의 `preprocess_input_views_for_inference`(`utils/inference.py`)는 모델 입력 그림 크기로 `get_rays_in_camera_frame` 광선을 만든다. 모델 내부에서 그림·광선을 다시 줄이지 않는다.
2. CPU 재현: 4차 세션 입력(교실·로비 prepared, 실제 2304×1296 영상)을 같은 커밋의 `crop_resize_if_necessary`·`get_rays_in_camera_frame`·`recover_pinhole_intrinsics_from_ray_directions` 로 돌리면 모델에 들어간 K 는 모든 모드에서 fx = fy(r518 263.4, r1036 526.8, r1554 783.1, rfull 1157.0, 주점 가운데)이고 광선에서 되찾은 K 도 같다. 우리 코드가 넘긴 K 와 그림 크기(가설 ③)도 r518 과 같은 경로라 문제가 없다(r518 은 정상).
3. 출력 K 는 모델이 예측한 광선에서 다시 구한 값이다(`postprocess_model_outputs_for_inference` → `recover_pinhole_intrinsics_from_ray_directions`). r518 에서도 카메라마다 입력과 0.1~2.1% 다르다(입력 K 를 조건으로만 쓴다). 학습 해상도 밖에서 그 예측이 무너진다.
4. 공식 README "Notes on Input Requirements": MapAnything 은 긴 변 518 이 입력 요구다(학습 설정도 518 many_ar).

즉 고해상도 Run 의 깊이·자세·K 는 서로 맞춰진 채 함께 틀어진 모델 예측이라, 출력 K 만 바꿔 끼우는 것으로 고칠 수 없다(깊이 z = 광선 길이 × 틀어진 광선 z, 자세도 그 광선으로 추정). 그래서 코드로 덮지 않았다.

**수정(이 티켓).** 원인 제거가 아니라 검출이다.
- `intrinsics_check.py`: 기대 K = 넘긴 K 를 전처리 규칙(덮는 배율 하나 + 가운데 자르기, 위 라이브러리 규칙을 다시 구현해 반 픽셀 안에서 일치)으로 옮긴 것. 출력 K 의 fx·fy·cx·cy 상대 오차를 잰다. 라이브러리가 모델에 넘긴 K 도 같이 재(`input`) 전처리 쪽 어긋남과 모델 쪽 어긋남을 가른다.
- `run_mapanything.py` known·prepared 모드: Run 끝에 metrics `intrinsics_check`(status OK·MISMATCH·SKIPPED, 최대 오차, 카메라별 값, fy/fx)를 쓰고 MISMATCH 면 `warnings` 에 한 줄을 넣는다. `exp_job.py` 가 이를 Run 기록 지표(`intrinsics_check.status`, `intrinsics_check.max_rel_err`, `warnings.count`)로 줄인다. images 모드는 넘긴 K 가 없어 SKIPPED(OK 로 채우지 않는다).
- `compare_resolutions.py`: "출력 K 검사" 열. 검사 전 Run 은 비우고, 노트북에서 `intrinsics_check.py <Run> --prepared <입력 폴더>` 로 다시 본다(위 표가 그 출력).

**허용치 5%(경고, 실패 아님).** 정상 Run(r518 교실·로비 8대) 최대 2.1%, DA3 자세 조건 0.3% 이하·자세 없이 2.2%, 틀어진 Run(16대) 최소 29.7%(fy). 5% 는 정상의 2배 넘게 여유를 두고 틀어진 최소의 1/6 이다. 거친 실패 검출용이며 보정 품질은 World 고정의 재투영·바닥 rms 가 잰다. 실패로 끝내지 않는 이유: 추론 자체는 끝났고 그 결과가 "해상도를 올리면 무너진다"는 비교 근거다. 다만 MISMATCH Run 의 K·깊이·자세는 보정·World 고정·해상도 비교 근거로 쓰지 않는다.

**Lead 결정 필요(대안).**
- (가) MapAnything 은 학습 해상도 518(fixed_mapping)만 쓰고, 고해상도 깊이는 DA3 혼합 경로(S15P31A101-283, 자세·K 는 MapAnything 518 고정)로만 얻는다. 권장: 지금 코드 그대로 되고 GPU 추가 실행이 필요 없다.
- (나) 고해상도 출력의 광선 길이(depth_along_ray)를 입력 광선(넘긴 K)에 얹어 점을 다시 만든다. 자세가 틀어진 광선으로 추정됐으므로 효과는 불확실하다. raw.npz 의 depth_z·출력 K 로 노트북에서 근사 시험은 가능하다(GPU 불필요).
- (다) 518 을 넘는 해상도 비교 자체를 접는다. -282 의 "배경용 점 밀도" 질문은 (가)가 답한다.
- 4차 세션 고해상도 Run 을 같은 코드로 다시 돌릴 이유는 없다(같은 모델 동작이 나온다). 생성기는 그대로 동작한다(아래 큐, `gpu_queue check` 통과).

## 버린 대안
- 출력 K 를 기대 K 로 바꿔 끼우기: 깊이·자세가 틀어진 광선과 함께 추정돼 K 만 고치면 기하가 더 어긋난다. 모델 출력을 지어내는 셈이다.
- 원본만 바로 쓰기: 학습 분포 밖이라 품질이 떨어질 수 있어 단계별로 잰다.
- 그림을 타일로 나눠 넣기: 타일 사이 자세·깊이 일관성을 다시 맞춰야 해 이번 범위 밖.

## 잔여 위험·넘긴 일
- (-312) 모델이 518 에서도 출력 K 를 0.1~2.1% 흔든다. 허용치 5% 아래의 흔들림은 경고하지 않는다(World 고정 재투영이 본다).
- (-312) `world_frame.cli fix` 는 `intrinsics_check` 를 읽지 않는다. MISMATCH Run 을 거르는 일은 사람(또는 후속 티켓)이 한다.
- (-312) `docs/design/measured-object-scale.md`(-100)가 기대한 "-282 고해상도 실행으로 다시 재기"는 이 결과로는 할 수 없다.
- rfull 의 VRAM·지연은 추정(60,352 토큰, r518 의 약 19배)이고 GPU 에서 재야 한다. OOM 이면 Run 기록이 OOM 으로 남는다.
- 자세는 518, 깊이만 원본 해상도로 다시 추정하는 혼합 경로는 S15P31A101-283(Depth Anything 3).
