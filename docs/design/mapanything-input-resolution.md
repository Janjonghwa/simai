# MapAnything 입력 해상도 비교 (S15P31A101-282)

관련: 명세 4.3 1단계(MapAnything 초기 추정), 20장 EXP-01. 코드: `ai/experiments/static_scene/run_mapanything.py`(`--resize-mode`·`--size`), `session3_jobs.py`(`--resolutions`), `compare_resolutions.py`. 시험: `ai/tests/experiments/static_scene/test_input_resolution.py`.

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

## 버린 대안
- 원본만 바로 쓰기: 학습 분포 밖이라 품질이 떨어질 수 있어 단계별로 잰다.
- 그림을 타일로 나눠 넣기: 타일 사이 자세·깊이 일관성을 다시 맞춰야 해 이번 범위 밖.

## 잔여 위험·넘긴 일
- rfull 의 VRAM·지연은 추정(60,352 토큰, r518 의 약 19배)이고 GPU 에서 재야 한다. OOM 이면 Run 기록이 OOM 으로 남는다.
- 자세는 518, 깊이만 원본 해상도로 다시 추정하는 혼합 경로는 S15P31A101-283(Depth Anything 3).
