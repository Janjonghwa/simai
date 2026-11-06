# S15P31A101-535: MapAnything 3D 맵 ↔ 보정 World 좌표계 정렬 원인 분해 및 바닥 평면 제약 정렬 보고서

- **Jira 티켓**: `S15P31A101-535` (Owner: Argon)
- **대상 데이터**:
  - 12차 배경 단일 세트 MapAnything 출력 번들: `C:/Users/SSAFY/simai-local/align-535/ma_single/` (`1793851520690`)
  - 12차 활성 보정본: `cal-lobby-init-f0ab1c6b-jba2-2f6cc097` (`ma_single/calibration.json`)
  - 12차 교차 검증 보정본: `cal-lobby-init-af8ad4ca-jba2-12816bf5`, 동일 번들 초기화 보정본 `ma_single_init`
  - 실측 기준: 4대 카메라 높이 `cam_A=2.44 m, cam_B=2.44 m, cam_C=2.48 m, cam_D=2.48 m`, 안전고깔 높이 `67.5 cm` (`session12_scale_summary.json`)
- **산출물 경로**:
  - `C:/Users/SSAFY/simai-local/align-535/alignment_comparison_summary.json`
  - `C:/Users/SSAFY/simai-local/align-535/alignment_comparison_table.md`
  - `C:/Users/SSAFY/simai-local/align-535/floor_height_map_before_after.png`
  - `C:/Users/SSAFY/simai-local/align-535/pareto_center_vs_floor_tilt.png`
  - `C:/Users/SSAFY/simai-local/align-535/candidate_a_known_poses_input.json`

---

## 1. 현상 재현 및 3대 근본 원인 수치 분해 (Step 1)

### 1.1 현상 재현 (`0_baseline_rot_only`)

기존 [`reconstruct_tasks.align_to_calibration`](../../src/simai_ai/scene/reconstruct_tasks.py) 을 `ma_single` 번들에 실행하면 다음 값으로 `STATIC_WORLD_ALIGNMENT_FAILED` 가 발생한다(게이트 기준: `max_center <= 0.10 m`, `max_rotation <= 5.0°`):

- `scale = 0.999793`
- 카메라별 중심 잔차 (`||s Q C_ma + shift - C_cal||`) 및 회전 잔차:
  - `cam_A`: **`0.1506 m`** (`dx=-0.0338, dy=-0.0184, dz=+0.1457 m`), `rot = 1.074°` → **중심 게이트(0.10 m) 초과**
  - `cam_B`: `0.0826 m` (`dx=-0.0183, dy=-0.0782, dz=-0.0192 m`), `rot = 1.313°`
  - `cam_C`: **`0.1037 m`** (`dx=+0.0227, dy=+0.0231, dz=-0.0985 m`), `rot = 1.880°` → **중심 게이트(0.10 m) 초과**
  - `cam_D`: `0.0840 m` (`dx=+0.0294, dy=+0.0736, dz=-0.0280 m`), `rot = 1.886°`
- 정렬 후 MapAnything 바닥 평면(RANSAC 인라이어 `184,121`점, 내부 평면 `RMS = 2.68 cm`)의 World 기울기 및 높이:
  - World `+Z` 대비 바닥 법선 기울기: **`1.119°`** (`pitch_x = -0.598°, roll_y = +0.946°`)
  - 바닥 점 World `Z` 분포: 중앙값 **`+2.15 cm`**, `IQR = 7.91 cm`, `p5..p95 = [-6.84, +6.55] cm` (90% 폭 `13.39 cm`)
  - 안전고깔(`X=3.712 m, Y=6.547 m`, 보정 삼각측량 꼭짓점 `Z=68.43 cm`) 위치의 맵 바닥 높이가 `+4.06 cm` 로 들려, 맵 바닥 대비 고깔 높이가 **`64.37 cm` (`-3.13 cm` 오차)** 로 축소됨.

---

### 1.2 원인 분해: (i) 모델 자체 왜곡 vs (ii) 보정 오차 vs (iii) `world_frame`·`align_to_calibration` 알고리즘 불일치

수치 분해 결과, 이번 실패는 단일 원인이 아니라 **3가지 요인이 순차적으로 결합된 현상**이며, 그중 **게이트 실패(`0.1506 m > 0.10 m`)의 직접적 주원인은 `(iii)` `align_to_calibration` 의 회전 전용(`Q_rot`) 추정 알고리즘 결함**이고, **정렬 후 바닥 기울기(`1.12°~1.58°`)의 원인은 `(ii)` 보정본 `C_cal` 에 박힌 `-1.057°` 바닥 기울기와 `(i)` MapAnything 추론 간 `±0.52°` 내부 경사 변동의 결합**임이 증명되었다.

#### ① 직접 원인 `(iii)`: `align_to_calibration` 의 회전 평균 전용 `Q_rot` 계산과 Stage A 회전 전용 BA 의 불일치 (기여도: 중심 잔차 `15.06 cm` 중 **`12.66 cm`**)

1. **코드 결함 구조**:
   기존 `align_to_calibration` 은 3D 회전 `Q` (`MapAnything -> World`)를 구할 때 카메라 중심 위치 `src -> dst` 를 전혀 쓰지 않고 **오직 카메라 방향 행렬의 합** `u, _, vt = svd(sum(R_cal_i.T @ R_ma_i))` 만으로 `Q_rot` 를 구한 뒤, `scale` 과 `shift` 만 `src, dst` 평균으로 맞췄다.
2. **파이프라인 불일치 메커니즘**:
   `site_setup` 파이프라인에서 `calibration.json` 은 (1) 초기 MapAnything 점구름에서 `fix_world_frame` 으로 카메라 중심 `C_cal` 과 초기 회전 `R_init` 을 고정한 뒤, (2) **V2 Stage A 회전 전용 Joint BA (`run_stage_a_rotation_ba`)** 가 2-view SIFT 특징점 트랙으로 **카메라 중심 `C_cal` 은 고정한 채 카메라 회전 `R_cal` 만 `1.1°~1.9°` 미세 회전**시켜 생성된다.
3. **수치 증거**:
   - `Q_rot`(카메라 회전 평균)와 `Q_cen`(카메라 중심 배치 `src -> dst` 의 Umeyama 최적 회전) 사이의 상대 회전각은 **`1.4511°`** (Euler XYZ `[+0.9927°, -0.9772°, -0.4151°]`)이다.
   - 로비 대각선 카메라들의 중심 평균으로부터의 팔 길이(lever arm)는 평균 **`4.997 m`** (`7.42 m × 6.65 m`)이다.
   - 따라서 카메라 중심 배치를 `Q_cen` 대신 `Q_rot` 로 `1.4511°` 기울여 회전시키면, 방 양 끝 카메라에 **`4.997 m × sin(1.4511°) = 12.66 cm`** 의 인위적인 수직(`dz`) 지렛대 오차가 즉시 발생한다(`cam_A dz = +14.57 cm`, `cam_C dz = -9.85 cm`).
   - **결정적 반증(동일 번들 초기화 실험)**: 12차 실패 로그(`site-setup-failed/job-09676785`) 및 `ma_single` 자체로 초기 보정본(`ma_single_init`)을 만든 뒤 Stage A BA 만 거친 경우, 카메라 중심 `C_cal` 은 `ma_single` 의 `C_ma` 와 **완전히 동일한 점(진성 중심 잔차 `0.0000 m`)**임에도 불구하고, 기존 `Q_rot` 방식은 Stage A 회전 보정량 때문에 `max_center = 0.1235 m` (실패 로그에서는 `0.150 m`)로 **자기 자신과의 정렬조차 실패**한다. 반면 카메라 중심 기반 Umeyama(`Q_cen`)로 바꾸는 즉시 동일 번들 잔차는 `0.0000 m`, `f0ab1c6b` 대비 잔차는 **`0.1506 m -> 0.0627 m`** 로 줄어 게이트(`<= 0.10 m`)를 통과한다.

#### ② 바닥 기울기 주원인 `(ii)`: 활성 보정본(`f0ab1c6b`)의 `C_cal` 에 고정된 `-1.057°` X축 피치 기울기

| 카메라 | 실측 높이 (m) | 보정본 `C_cal` World Z (m) | 보정본 오차 (cm) | `ma_single` 맵 바닥 대비 높이 (m) | `ma_single` 오차 (cm) | 차이 `h_ma - Z_cal` (cm) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `cam_A` (우측 상단, `X=7.06m`) | `2.4400` | `2.3592` | **`-8.08 cm`** | `2.4878` | `+4.78 cm` | `+12.86 cm` |
| `cam_B` (우측 하단, `X=7.08m`) | `2.4400` | `2.3821` | **`-5.79 cm`** | `2.4554` | `+1.54 cm` | `+7.33 cm` |
| `cam_C` (좌측 하단, `X=-0.35m`) | `2.4800` | `2.5417` | **`+6.17 cm`** | `2.4586` | `-2.14 cm` | `-8.31 cm` |
| `cam_D` (좌측 상단, `X=0.22m`) | `2.4800` | `2.5528` | **`+7.28 cm`** | `2.4295` | `-5.05 cm` | `-12.33 cm` |
| **평균 (Mean)** | **`2.4600`** | **`2.4590`** | **`-0.10 cm`** | **`2.4578`** | **`-0.22 cm`** | **`-0.11 cm`** |

- `-462` 보고서와 동일하게, 활성 보정본 `f0ab1c6b` 는 이전 시각 번들(`1793846677630`)에서 `fix_world_frame` 이 바닥 RANSAC 법선만으로 `+Z` 축을 잡을 때(실측 카메라 높이는 단일 스칼라 `scale` 산출에만 사용됨) **`-1.057°` (`7.42 m` 폭에서 `13.7 cm`)의 X축 바닥 피치 기울기**가 보정 카메라 중심 `C_cal` 의 Z 좌표에 그대로 박혔다.
- 단, **4대 카메라 높이의 평균값**은 실측 `2.4600 m`, 보정본 `2.4590 m`, `ma_single` `2.4578 m` 로 **셋 모두 `2.2 mm` 이내로 정확히 일치**한다.

#### ③ 부수 원인 `(i)`: MapAnything 다중 뷰 추론의 내부 경사(`+0.524°`) 및 대각 기선 비등방성(`±6.5 cm`)

| 카메라 쌍 | `ma_single` 기선 길이 (m) | 보정본 `f0ab1c6b` 기선 길이 (m) | 길이 차이 `d_ma - d_cal` (cm) | 비율 `d_cal / d_ma` |
| :--- | :---: | :---: | :---: | :---: |
| `cam_A - cam_B` (우측 단변) | `5.9756` | `5.9840` | `-0.84 cm` | `1.0014` |
| `cam_C - cam_D` (좌측 단변) | `6.6632` | `6.6524` | `+1.08 cm` | `0.9984` |
| `cam_A - cam_D` (상단 장변) | `6.8748` | `6.8673` | `+0.75 cm` | `0.9989` |
| `cam_B - cam_C` (하단 장변) | `7.4310` | `7.4342` | `-0.32 cm` | `1.0004` |
| `cam_A - cam_C` (대각선 1) | `9.9519` | `10.0207` | **`-6.88 cm`** | `1.0069` |
| `cam_B - cam_D` (대각선 2) | `9.9508` | `9.8884` | **`+6.24 cm`** | `0.9937` |

- `ma_single` 은 전체 스케일이 `0.999965` 로 거의 정확하지만, 사각형 대각선 방향에 `±6.2~6.9 cm` (`0.65%`)의 마름모 전단 변형(shear anisotropy)이 존재해 순수 강체·유사 변환만으로는 약 `5.2~6.3 cm` 의 수평(`dx, dy`) 하한 잔차가 남는다.
- 또한 `ma_single` 내부에서는 바닥 대비 카메라 높이가 `cam_A(+4.78 cm) > cam_D(-5.05 cm)` 로 **`+0.524°` 의 반대 방향 X축 경사**를 가진다.
- 이 때문에 제약 없는 카메라 중심 Umeyama(`1_umeyama_centers`)로 `C_ma -> C_cal` 을 맞추면, 보정본의 `-1.057°` 경사와 `ma_single` 의 `-0.524°` 역경사가 더해져 **정렬된 맵 바닥이 World 에서 `1.582°` (`pitch_x = -1.581°, roll_y = -0.051°`) 기울어지는 부작용**이 발생한다.

---

## 2. 정렬 방식 후보 (a), (b), (c) 설계 및 수치 비교 (Step 2 & Step 3)

### 2.1 후보별 설계 요약

1. **후보 `(a)` — 보정된 카메라 외부 파라미터(`R, t`) + 내부 파라미터(`K`)를 MapAnything 에 조건 입력**:
   - [`run_mapanything.py`](../../src/simai_ai/scene/run_mapanything.py) 확인 결과: 현재 프로덕션(`reconstruct_tasks.mapanything`)은 `--mode prepared` 로 호출되어 [`build_prepared_views`](../../src/simai_ai/scene/run_mapanything.py#L162-L168) 가 왜곡 보정된 영상과 `intrinsics`(`K_undistorted`)만 넘기고 `camera_poses` 는 넘기지 않는다.
   - 반면 [`build_known_views`](../../src/simai_ai/scene/run_mapanything.py#L119-L144) (`--mode known --use-poses 1`)는 각 뷰 딕셔너리에 `"camera_poses": torch.tensor(T_c2w)` 와 `"is_metric_scale": torch.tensor([True])` 를 함께 넣어 `preprocess_inputs` 로 전달한다.
   - **한계 및 검증 준비**:
     1. 선행 실험(`docs/experiments/EXP-01_design.md` C4a/C4b)에서 확인되었듯 MapAnything 은 입력 `camera_poses` 를 하드 제약(hard lock)으로 고정하지 않고 트랜스포머 인코더의 소프트 조건 토큰으로만 사용하므로, 출력 `cameras.json` 의 예측 자세가 입력 자세로부터 여전히 `2~8 cm / 0.3°~1.0°` 어긋나고 스케일도 `2~3%` 덜 따라가 사후 정렬(`align_to_calibration`)이 여전히 필수다.
     2. 초기 현장 설정(`site_setup`) 시점에는 아직 `calibration.json` 이 존재하지 않으므로 `(a)` 를 사용할 수 없다.
     3. GPU Pod 추론 큐에 바로 투입할 수 있도록 노트북 CPU 에서 `prepared.json + calibration.json` 을 읽어 `known` 모드 입력 명세(`candidate_a_known_poses_input.json`)를 생성·검증하는 [`pod_verify_known_poses.py`](./pod_verify_known_poses.py) 를 작성·검증 완료했다.
2. **후보 `(b)` — 바닥 평면 제약 결합 가중 최소제곱 Similarity 정렬 (`solve_candidate_b_wls`, 채택)**:
   - 카메라 중심 변위(`src -> dst`), 공선/공면 영공간 정규화용 미소 카메라 회전 사전값(`eps_rot = 1e-3`), 그리고 MapAnything 점구름에서 `find_floor` 로 추출한 **바닥 법선 제약(`Q @ n_floor_ma ≈ +Z`, 가중치 `w_normal`)** 과 **바닥 높이 제약(`z_floor_world ≈ 0`, 가중치 `w_height`)** 을 단일 닫힌 형태(closed-form) SVD 로 결합한다:
     $$H(w_n) = Y^T X + \varepsilon_{\text{rot}} \sum_i R_{\text{cal}, i}^T R_{\text{ma}, i} + w_n e_z n_{\text{floor, ma}}^T, \quad Q = U \operatorname{diag}(1, 1, \det(UV^T)) V^T$$
     $$\text{scale} = \frac{\operatorname{tr}(Q X^T Y)}{\|X\|_F^2}, \quad \text{shift} = \bar{C}_{\text{cal}} - \text{scale} \cdot Q \bar{C}_{\text{ma}}, \quad \text{shift}_z \leftarrow \text{shift}_z - \frac{w_z}{N + w_z} z_{\text{floor}}(\bar{C}_{\text{ma}})$$
   - **적응형 신뢰 영역(Adaptive Trust-Region Bisection)**: 보정본 자체에 바닥 기울기가 박혀 있는 경우(예: 12차 `f0ab1c6b` 의 `-1.06°`), `w_n` 을 무한정 키우면 맵 바닥은 완전히 수평(`0.15°`)이 되지만 보정본의 기울어진 카메라 중심 `C_cal` 과의 괴리가 `0.131 m` 로 커져 `0.10 m` 게이트를 넘는다. 따라서 `max_center_residual <= 0.95 * max_center_m`(`0.095 m`)을 만족하는 최대 `w_n in [0, 60]` 을 이분 탐색(24회 SVD, `< 1 ms`)으로 자동 선택한다.
3. **후보 `(c)` — 초기 Similarity 정렬 후 점구름-보정 평면 Huber ICP 미세 보정 (`solve_candidate_c_post_icp`)**:
   - `1_umeyama_centers` 로 초기 변환 `M_init` 을 구한 뒤, 바닥 인라이어 점구름(`Z = 0` 목표 평면, Huber 손실 $\delta = 3\text{ cm}$)과 보정 카메라 중심(`C_cal`) 사이의 $\text{SE}(3)$ Gauss-Newton 반복 최적화(15회)를 수행한다.

---

### 2.2 후보 전체 정량 비교표 (`ma_single` vs `cal-lobby-init-f0ab1c6b-jba2-2f6cc097`)

| 후보 | 게이트 (`<=0.10m, <=5°`) | `scale` | 카메라 중심 잔차 `A / B / C / D` (m) | `max_center` (m) | `max_rot` (°) | World 바닥 기울기 (°) | 바닥 Z `med / IQR / p5..p95` (cm) | 고깔 높이 (cm, 실측 67.5) | 2-View 재투영 중앙값 (px) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `0_baseline_rot_only` (기존) | **FAIL** | `0.999793` | `0.1506 / 0.0826 / 0.1037 / 0.0840` | **`0.1506`** | `1.89°` | `1.119°` | `+2.15 / 7.91 / [-6.8, +6.5]` | `64.37 (-3.13)` | `27.17` |
| `1_umeyama_centers` (`w_n=0`) | **PASS** | `0.999965` | `0.0627 / 0.0598 / 0.0518 / 0.0550` | **`0.0627`** | `3.08°` | `1.582°` | `+0.53 / 6.45 / [-7.2, +7.0]` | `68.91 (+1.41)` | `28.93` |
| **`2b_floor_wls_gate_safe` (채택)** | **PASS** | **`0.999914`** | **`0.0950 / 0.0597 / 0.0547 / 0.0882`** | **`0.0950`** | **`2.92°`** | **`0.801°`** | **`-0.16 / 3.14 / [-3.8, +4.3]`** | **`68.80 (+1.30)`** | **`25.92`** |
| `2b_floor_wls_fixed_w50` | **PASS** | `0.999917` | `0.0937 / 0.0591 / 0.0538 / 0.0870` | **`0.0937`** | `2.92°` | `0.825°` | `-0.13 / 3.24 / [-3.9, +4.4]` | `68.80 (+1.30)` | `25.91` |
| `3c_post_icp_gate_safe` | **PASS** | `0.999965` | `0.0950 / 0.0605 / 0.0511 / 0.0839` | **`0.0950`** | `2.93°` | `0.842°` | `+0.19 / 3.30 / [-3.6, +4.8]` | `68.52 (+1.02)` | `26.17` |
| `3c_post_icp_unconstrained` | **FAIL** | `0.999965` | `0.1309 / 0.0815 / 0.0803 / 0.1153` | **`0.1309`** | `2.90°` | `0.233°` | `-0.27 / 1.96 / [-2.0, +2.8]` | `68.32 (+0.82)` | `26.71` |
| `2b_floor_wls_hard_level` (`w_n=500`) | **FAIL** | `0.999794` | `0.1313 / 0.0835 / 0.0862 / 0.1239` | **`0.1313`** | `2.94°` | `0.156°` | `-0.59 / 2.03 / [-2.2, +2.5]` | `68.71 (+1.21)` | `26.71` |

> [!IMPORTANT]
> **보정 카메라 삼각측량 고깔 높이와 맵 바닥 정합**:
> `-462` 에서 보정 카메라 2D 꼭짓점 삼각측량으로 얻은 안전고깔 꼭짓점의 World Z 는 `68.43 cm`(실측 `67.5 cm` 대비 `+0.93 cm`)이고, `ma_single` 점구름 내부에서 바닥 대비 고깔 점구름 최대 높이는 **`68.40 cm`**(`+0.90 cm`)이다.
> 기존 `0_baseline_rot_only` 에서는 고깔 위치의 맵 바닥이 `Z = +4.06 cm` 로 들려 맵 바닥 대비 고깔 높이가 `64.37 cm`(`-3.13 cm`)로 왜곡되었으나, 채택안 **`2b_floor_wls_gate_safe`** 에서는 바닥 중앙값이 **`-0.16 cm`**, 바닥 높이 사분위수 퍼짐(`IQR`)이 `7.91 cm -> 3.14 cm`(**`-60.3%` 감소**), 90% 높이 폭(`p5..p95`)이 `13.39 cm -> 8.11 cm` 로 줄어들고, 2-view SIFT 교차 재투영 오차 중앙값도 `27.17 px (Umeyama 28.93 px) -> 25.92 px` 로 가장 낮다.

---

### 2.3 다중 보정본 교차 검증 (`f0ab1c6b`, `af8ad4ca`, `ma_single_init`)

동일한 알고리즘(`2b_floor_wls_gate_safe`, `w_normal=60, w_height=4, target_cap=0.95*max_center_m`)을 12차의 다른 보정본들에 적용한 결과:

| 보정본 시나리오 | 기존 `0_baseline_rot_only` | `1_umeyama_centers` (`w_n=0`) | 채택안 `2b_floor_wls_gate_safe` |
| :--- | :---: | :---: | :---: |
| **`f0ab1c6b` (12차 활성 보정본)** | `0.1506 m` (**FAIL**), tilt `1.119°` | `0.0627 m` (PASS), tilt `1.582°` | **`0.0950 m` (PASS)**, tilt **`0.801°`** (`w_n=53.0`) |
| **`af8ad4ca` (12차 이전 보정본)** | `0.1956 m` (**FAIL**), tilt `1.448°` | `0.0625 m` (PASS), tilt `0.905°` | **`0.0725 m` (PASS)**, tilt **`0.304°`** (`w_n=60.0`) |
| **`ma_single_init` (동일 번들 초기화)** | `0.1235 m` (**FAIL**), tilt `1.045°` | `0.0000 m` (PASS), tilt `0.000°` | **`0.0000 m` (PASS)**, tilt **`0.000°`** (`w_n=60.0`) |

- **`af8ad4ca`** 에서는 기존 방식이 `0.1956 m` 로 크게 실패했으나, 채택안은 `max_center = 0.0725 m` 로 여유 있게 통과하면서 바닥 기울기를 **`1.448° -> 0.304°`** 로 낮춘다.
- **`ma_single_init`**(동일 번들 초기화 + Stage A BA)에서도 기존 방식은 `0.1235 m` 로 실패하나, 채택안은 `max_center = 0.0000 m`, `floor_tilt = 0.000°` 로 완벽히 복원한다.

---

## 3. 채택 권고 및 프로덕션 반영 설계 (Step 4)

### 3.1 왜 후보 `(b)` (바닥 평면 제약 결합 WLS Similarity)를 기본값(`floor_constraint=True`)으로 채택하는가?

1. **게이트 통과율 및 수치 안정성**:
   - 후보 `(b)` 는 단일 $3\times 3$ SVD 와 이분 탐색만으로 닫힌 형태(closed-form) 전역 최적해를 구하므로, 초기값이나 점구름 샘플링 노이즈에 민감한 반복 ICP(후보 `(c)`)보다 빠르고 결정론적(deterministic)이다.
   - 후보 `(c)` 도 신뢰 영역 제약을 걸면 유사한 성능(`max_center = 0.0950 m, tilt = 0.842°`)을 내지만, 본질적으로 강체 변환 자유도(6-DoF) 안에서 움직이므로 `(b)` 가 달성하는 파레토 곡선(`tilt = 0.801°, IQR = 3.14 cm`)을 넘어설 수 없다.
2. **기본 활성화(`floor_constraint=True`) 타당성 및 안전한 폴백**:
   - MapAnything 점구름(`mapanything/raw.npz` 또는 `mapanything/pointcloud.npz`)이 존재하고 `find_floor` 가 유효한 바닥 평면을 찾으면 바닥 법선(`+Z`)과 바닥 높이(`Z=0`) 제약을 기본 적용하되, 적응형 신뢰 영역(`0.95 * max_center_m`) 안으로 가중치를 자동 제한하므로 **바닥 제약 때문에 정당한 정렬이 게이트에서 탈락하는 일이 절대 없다**.
   - 점구름 파일이 없는 경우(예: 카메라 자세만 주어진 합성 단위 테스트)나 `job.json` 의 `input.options.floor_constraint = False`(또는 환경변수 `SIMAI_ALIGN_FLOOR_CONSTRAINT=0`)가 지정된 경우에는 자동으로 `w_normal = 0`(카메라 중심 Umeyama + 미소 회전 사전값 `eps_rot = 1e-3`)으로 동작한다.
   - 미소 회전 사전값(`eps_rot = 1e-3`) 덕분에 카메라가 2대뿐인 공선(collinear) 배치나 수평 공면(coplanar) 배치에서도 영공간 회전(roll)이 완벽히 정규화되어 기존 2-카메라 합성 단위 테스트와도 100% 호환된다.
   - 기존 프로덕션 게이트 한도(`ALIGN_MAX_CENTER_M = 0.10`, `ALIGN_MAX_ROTATION_DEG = 5.0`)와 에러 코드(`STATIC_WORLD_ALIGNMENT_FAILED`)는 완화 없이 그대로 유지한다.
