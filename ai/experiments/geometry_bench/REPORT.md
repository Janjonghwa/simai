# S15P31A101-528: 정적 메쉬 형상·텍스처 품질 향상 비교 실험 (Geometry & Texture Benchmark)

- **Jira 티켓**: `S15P31A101-528` (Owner: Argon)
- **기준 세션/보정**: 12차 로비 세션 (`cal-lobby-init-f0ab1c6b-jba2-2f6cc097`, 고정 카메라 4대 `cam_A`~`cam_D`, 해상도 `2304×1296`)
- **패키지 경로**: [`ai/experiments/geometry_bench/`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-528-geometry-texture-bench/ai/experiments/geometry_bench)
- **로컬 산출물 경로**:
  - 노트북 CPU 텍스처·정제 비교: `C:/Users/SSAFY/simai-local/geometry-bench/laptop_bench_summary.{json,md}`
  - GPU Pod 원본 산출물: `C:/Users/SSAFY/simai-local/geometry-bench/pod_results/`
  - Variant 2(`v2_da3_hybrid`) 메시 정리(`v2_da3_clean`) 및 4종 비교: `C:/Users/SSAFY/simai-local/geometry-bench/v2_clean/`

---

## 1. 실험 목적 및 비교 변형 (Variants 0~4 + `v2_da3_clean`)

현행 12차 파이프라인(MapAnything 518 단일 프레임 깊이 + TSDF 복셀 4 cm + 150k 면 + `texrecon`)의 형상 뭉개짐·벽/바닥 구멍(`cam_D` 결손)·텍스처 이음새 문제를 진단하고, **복셀 해상도 + 다중 프레임 깊이 통합 + DA3 1008 하이브리드 깊이 + 4단계 기하 후처리(`v2_da3_clean`) + OpenMVS (`ReconstructMesh` / `RefineMesh` / `TextureMesh`)** 조합 중 어떤 단계가 실제로 품질을 올리는지 동일 지표로 비교했다.

| 변형 ID | 구성 요약 | 깊이 입력 | 형상 복원 / 정제 | 텍스처 베이킹 |
|---|---|---|---|---|
| **Variant 0 (`v0_baseline`)** | 현행 12차 기준선 | MapAnything 518 (단일 프레임) | TSDF (`voxel=0.04 m`, `150k` faces) | `texrecon` (`area` + `gmi`, `--skip_hole_filling`) |
| **Variant 1 (`v1_fine_multiframe`)** | 세밀한 TSDF + 다중 프레임 중앙값 | MapAnything 518 ($N$장 픽셀별 중앙값; 12차 단일 프레임 시 자동 대체) | TSDF (`voxel=0.015 m`, `550k` faces) | `texrecon` |
| **Variant 2 (`v2_da3_hybrid`)** | Variant 1 + DA3 1008 하이브리드 | MapAnything 포즈 고정 + DA3 1008px 깊이 (`sim3` 정렬 + 평면 정리) | TSDF (`voxel=0.015 m`, `550k` faces, `plane_cleanup=True`) | `texrecon` |
| **Variant 2 Clean (`v2_da3_clean`)** | Variant 2 + 4단계 기하 정리 필터 | Variant 2 동일 | `(a)` 긴 변 제거 → `(b)` 방 범위 밖·천장 판 제거 → `(c)` 전 카메라 그레이징 면 제거 → `(d)` 작은 연결 성분 제거 (`438.7k` faces) | `v2_da3_hybrid` GLB 텍스처·UV 무손실 보존 (`v2_clean.py`) |
| **Variant 3 (`v3_openmvs_refine_texrecon`)** | 메쉬 + OpenMVS `RefineMesh` | Variant 1 / 12차 메쉬 | TSDF + OpenMVS `RefineMesh` (`--close-holes 0 --decimate 1`) | `texrecon` |
| **Variant 4 (`v4_openmvs_full`)** | OpenMVS `TextureMesh` 비교 | 12차 메쉬 / MapAnything 점군 | OpenMVS `RefineMesh` (`--close-holes 0`) | OpenMVS `TextureMesh` (`--empty-color 8421504`) |

---

## 2. 좌표·단위·내부 파라미터 변환 규약 (`mvs_io.py`)

1. **World 좌표계 (`AGENTS.md` 규칙 3)**:
   - 오른손 좌표계, 단위 미터(`m`), `+Z` 상방, 바닥 `Z = 0`.
   - glTF 2.0 `.glb` 내보내기(`GLTF_Y_UP`: `(x, z, -y)`)와 역변환(`(x, -z, y)`)은 `simai_ai.scene.texture_mesh.to_glb_points` 및 [`mvs_io.from_glb_points`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-528-geometry-texture-bench/ai/experiments/geometry_bench/mvs_io.py#L194-L202) 한 곳에서만 수행한다.
2. **OpenMVS `.mvs` 바이너리(`InterfaceMVS` v3) 정규화 내부 파라미터(`K_norm`) 핵심 규약**:
   - OpenMVS `InterfaceCOLMAP` 은 초점거리(`fx`, `fy`)뿐 아니라 **주점(`cx`, `cy`)도 모두 `scale = max(width, height)` (`2304`)로 나누어 저장**한다:
     $$K_{\text{norm}} = \begin{bmatrix} f_x / S & 0 & c_x / S \\ 0 & f_y / S & c_y / S \\ 0 & 0 & 1 \end{bmatrix}, \quad S = \max(W, H)$$
   - [`write_openmvs_mvs_file`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-528-geometry-texture-bench/ai/experiments/geometry_bench/mvs_io.py#L792-L838) 에서 `InterfaceCOLMAP` 과 바이트 단위로 동일한 `scale = max(W, H)` 정규화를 적용했다.

---

## 3. 노트북 CPU 벤치마크 결과 (동일 메쉬 기준 `texrecon` vs OpenMVS `TextureMesh` / `RefineMesh`)

동일한 12차 로비 메쉬(`current-scene-textured.glb`, `149,997` faces, `192.63 m²`)와 왜곡을 편 4대 카메라 사진(`2304×1296`)을 입력으로 `texrecon` vs OpenMVS `TextureMesh` 및 OpenMVS `RefineMesh` 전후를 비교했다(`C:/Users/SSAFY/simai-local/geometry-bench/laptop_bench_summary.md`).

### 3.1 요약 비교표 (`laptop_bench_summary.md`)

| 변형 (Variant) | 메쉬 면 수 | 용량 (MB) | 관측 면 비율 | Self PSNR (dB) | Self SSIM | LOO PSNR (dB) | LOO Blur8 (dB) | LOO SSIM | EdgeGradCos | EdgeF1 (2px) | CrossView AbsRel p50 | 정제 시간(s) | 텍스처 시간(s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `v0_texrecon` | 149,997 | 12.05 | 69.4% | **16.65** | **0.7098** | **13.69** | **17.13** | **0.4815** | **0.6749** | **0.7556** | **0.0008** | 0.0 | 32.5 |
| `v4_openmvs_tex` | 149,997 | 15.60 | 99.9% | 16.62 | 0.6692 | 13.25 | 16.92 | 0.3837 | 0.6497 | 0.6638 | 0.0009 | 0.0 | **12.0** |
| `v3_refine_texrecon` | 150,370 | 20.24 | 68.4% | 15.61 | 0.6264 | 13.26 | 16.60 | 0.4935 | 0.5969 | 0.7359 | 0.0008 | 12.7 | 15.8 |
| `v4_refine_openmvs_tex` | 149,997 | 15.97 | 99.9% | **16.81** | 0.6803 | 13.43 | 16.94 | 0.4181 | 0.6641 | 0.6672 | 0.0010 | 12.7 | 12.0 |

### 3.2 핵심 판단 (Verdict)

1. **동일 메쉬 기준 `texrecon` 이 LOO SSIM·경계 선명도(EdgeF1)·관측/미관측 분리에서 우세**:
   - `v0_texrecon` 은 Self SSIM(`0.7098` vs `0.6692`), **LOO PSNR(`13.69 dB` vs `13.25 dB`)**, **LOO Blur8(`17.13 dB` vs `16.92 dB`)**, **LOO SSIM(`0.4815` vs `0.3837`)**, **EdgeF1(`0.7556` vs `0.6638`)** 모든 핵심 지표에서 `v4_openmvs_tex` 를 앞선다.
   - OpenMVS `TextureMesh` 는 메쉬 면적의 **99.9%** 에 텍스처를 입히지만, 비스듬하거나 가려진 면까지 억지로 투영해 텍스처 늘어짐이 발생하고 `AGENTS.md` 규칙 2·7(관측/추정 구분)에도 맞지 않는다.
2. **4대 광각(Wide-Baseline) 환경에서 OpenMVS `RefineMesh` 는 이득 없음**:
   - `v3_refine_texrecon` 은 `v0_texrecon` 대비 Self PSNR `-1.04 dB`, LOO PSNR `-0.43 dB` 하락한다.
   - Pod 실행에서도 `conda-forge` `InterfaceCOLMAP` 바이너리가 컨테이너 내부 경로에서 `recon.mvs` 를 생성하지 못해(`FileNotFoundError: .../v3_openmvs_refine_texrecon/refine/recon.mvs`) 중단되었으며, 노트북 CPU 벤치마크에서 이미 `RefineMesh` 가 광각 4카메라에서 이득이 없음이 확인되었으므로 정적 씬 파이프라인에서 `RefineMesh` 는 제외한다.

---

## 4. GPU Pod 실험 1: MapAnything 마스크 비교 (A~D) · TSDF 손실 분해 · 카메라별 구멍 분류 (`pod_results/hole_analysis_par/`)

Pod 에서 실행한 [`hole_and_tsdf_analysis.md`](file:///C:/Users/SSAFY/simai-local/geometry-bench/pod_results/hole_analysis_par/hole_and_tsdf_analysis.md) 결과는 **왜 현행 파이프라인(`v0`, `v1`)에서 검은 구멍이 생겼는지**를 명확히 규명했다.

### 4.1 MapAnything 마스크 조건 비교 (A / B / C / C2 / D)

| 조건 | `mask_edges` | `conf_percentile` | 유효 픽셀 비율 (`A / B / C / D` 평균) | ① 동일 시점 검은 구멍 비율 | ② 새 면 사진 지지율 | ③ 떠 있는 조각 증가 (`m²`) | ④ 다중 카메라 재투영 오차 중앙값 (`px`) | TSDF 면적 (`m²`) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **`A_baseline`** (기준) | `True` | `10.0` | `0.9007` | `38.67%` | `100.00%` | `+0.000` | `76.44` (`0.220 m`) | `165.13` |
| **`B_no_edge_mask`** | `False` | `10.0` | `0.9007` | `38.67%` | `100.00%` | `+0.000` | `76.44` (`0.220 m`) | `165.13` |
| **`C_conf_relaxed_p2`** | `True` | `2.0` | `0.9805` | `34.43%` (`-4.24%p`) | `94.75%` | `+0.000` | `75.20` (`0.220 m`) | `172.37` |
| **`C2_conf_relaxed_p0`** (선택) | `True` | `0.0` | `1.0000` | **`33.41%`** (`-5.26%p`) | **`94.04%`** | **`+0.000`** | **`75.34`** (`0.224 m`) | **`174.16`** |
| **`D_both_off`** | `False` | `2.0` | `0.9805` | `34.43%` (`-4.24%p`) | `94.75%` | `+0.000` | `75.20` (`0.220 m`) | `172.37` |

- **결론**: MapAnything 518 에서 `mask_edges` 는 거의 영향을 주지 않으며, `conf_percentile` 하위 10% 절단이 유효 바닥·벽면 약 `9.0 m²`(구멍 `5.26%p`)를 잘라내고 있었다. 그러나 신뢰도를 모두 풀어도(`C2`) 여전히 `33.41%` 의 구멍이 남는데, 그 원인은 아래 4.2·4.3 에서 드러난다.

### 4.2 TSDF 및 후처리 단계별 면적 손실 폭포 (Waterfall)

| 단계 | 설정 | 메쉬 면적 (`m²`) | 단계 손실 (`m²`) | 누적 손실 (`m²`) | 검은 구멍 비율 |
|---|---|---:|---:|---:|---:|
| `S0_raw_tsdf_unfiltered` | 모든 후처리 OFF | `207.88` | `0.00` | `0.00` | `36.38%` |
| `S1_after_flying_mask` | `+ depth flying_mask` | `204.60` | `-3.28` | `-3.28` | `36.93%` |
| **`S2_after_conflict_keep`** | **`+ TSDF conflict_keep`** | **`166.96`** | **`-37.65`** | **`-40.93`** | **`47.41%` (`+10.48%p`)** |
| `S3_after_manifold_clean` | `+ manifold clean` | `166.96` | `0.00` | `-40.93` | `47.41%` |
| `S4_after_small_cluster_removal` | `+ small cluster removal` | `165.17` | `-1.78` | `-42.71` | `47.62%` |
| `S5_after_quadric_decimation` | `+ quadric decimation (150k)` | `165.13` | `-0.04` | `-42.75` | `47.74%` |

- **핵심 원인 발견**:
  1. 전체 면적 손실 `42.75 m²` 중 **88.1%(`37.65 m²`)가 단 하나의 단계인 `S2_after_conflict_keep` 에서 사라진다.**
  2. MapAnything 518 의 카메라 간 깊이 불일치 중앙값이 `0.220 m` 인데, TSDF 절단 거리(`trunc = 8 × voxel`)는 `voxel = 0.04 m` 일 때 `0.32 m`, **`voxel = 0.015 m` 일 때 `0.12 m`** 이다.
  3. 따라서 시점 간 깊이 정렬 없이 복셀만 `0.04 m → 0.015 m` 로 줄이면(`v1_fine_multiframe`), 절단 거리(`0.12 m`)가 카메라 간 깊이 오차(`0.22 m`)보다 작아져 **`conflict_keep` 이 `cam_D` 영역 전체를 충돌로 판정해 지워버린다(`cam_D` 덮음 `13.4% → 0.0%`).**

### 4.3 카메라별 구멍 분류 (`pod_results/hole_analysis_par/hole_classification/`)

| 카메라 | **A: 입력 없음·면 없음** (빨강) | **B: 입력 있음·면 없음** (노랑, TSDF/후처리 손실) | **C: 입력 있음·면 있음·미관측** (파랑) | **OK: 정상 관측 렌더** (초록) |
|---|---:|---:|---:|---:|
| `cam_A` | `9.48%` | `10.67%` | `4.69%` | `75.16%` |
| `cam_B` | `8.98%` | `15.10%` | `3.55%` | `72.37%` |
| `cam_C` | `8.60%` | `13.52%` | `5.06%` | `72.81%` |
| **`cam_D`** | `22.92%` | **`63.65%`** | `0.00%` | **`13.43%`** |

- `cam_D` 화면의 **63.65%** 가 깊이 입력은 유효했으나 TSDF `conflict_keep` 에서 소실된 **B등급 구멍**이었다. 이를 근본적으로 해결한 것이 **DA3 1008px 깊이 + `sim3` 시점 정렬 + 평면 정리(`v2_da3_hybrid`)** 이다.

---

## 5. GPU Pod 실험 2 및 2차 지시: `v2_da3_hybrid` 메시 정리 (`v2_da3_clean`) 및 4종 비교

### 5.1 `v2_da3_hybrid` 의 성과와 자유 시점 아티팩트 원인

- **성과**: `v2_da3_hybrid` 는 `cam_D` 화면 덮음을 `13.4% (v0) / 0.0% (v1) → 93.5%` 로 복원하며 4대 카메라 평균 전체 덮음을 `61.4% → 91.1%`, LOO 덮음을 `37.4% → 70.4%` 로 끌어올렸다. 카메라 시점(`compare_self_cam_*.jpg`, `compare_loo_cam_A.jpg`)에서 벽·창문·바닥·책상이 가장 선명하게 복원되었다.
- **자유 시점 문제**: 자유 사선 시점(`compare_free_obl3.jpg`, `compare_free_obl2.jpg`, `compare_free_obl1.jpg`)에서 교실 상부를 거대한 회색/검은색 판과 늘어진 삼각형이 덮었다.
  - 기하 분석 결과, 4대 카메라의 보정 World 설치 높이는 `Z = [2.359, 2.382, 2.542, 2.553] m`(최소 `2.36 m`, 평균 `2.46 m`)인데, `v2_da3_hybrid` 메쉬의 삼각형 `551,743`개 중 **`89,432`개(`16.21%`)가 천장 높이(`Z > 2.35~2.65 m`) 또는 방 XY 경계 밖에 생성된 천장 판**이었고, 이 천장 판이 사선 시점 카메라(`obl3` `Z=3.0m`, `obl1/2` `Z=3.6m`) 바로 앞을 가로막고 있었다.

### 5.2 4단계 기하 정리 필터 (`v2_clean.py`) 및 단계별 면 제거율·덮음 변화

[`ai/experiments/geometry_bench/v2_clean.py`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-528-geometry-texture-bench/ai/experiments/geometry_bench/v2_clean.py) 에서 `v2_da3_hybrid/all/textured.glb` 의 UV·머티리얼·PNG 텍스처 바이트를 재인코딩 없이 그대로 유지하면서 삼각형 인덱스만 정제하는 4단계 필터를 적용했다:

1. **`(a)` 긴 변 삼각형 제거 (`a_long_edge`)**:
   - 근거: 복셀 크기 `voxel_m = 0.015 m` 의 TSDF 절단 대역폭은 `trunc_m = 8 × voxel_m = 0.120 m` 이다. 깊이 불연속 경계를 가로지르는 비평면/미관측 다리 삼각형(`> 8 × voxel_m = 0.120 m`)과 `plane_cleanup` 평면 단순화 한계를 넘는 초대형 삼각형(`> 10 × voxel_m = 0.150 m`)을 제거하되, 평탄한 바닥·수직벽의 관측 삼각형(`<= 0.150 m`)은 보존해 바닥 점구멍(pepper-hole)을 방지한다.
2. **`(b)` 방 범위 밖 및 천장 높이 위 삼각형 제거 (`b_room_ceiling_bounds`)**:
   - 근거: 보정 World 좌표(`+Z` 상방, 바닥 `Z = 0`)에서 방 XY 외곽(`[-0.35, 8.40] × [-0.45, 12.10] m`), 바닥 아래(`Z < -0.10 m`), 벽 상단 위(`Z > 2.65 m`), 그리고 카메라 설치 높이(`Z > 2.35 m`, 최소 카메라 높이 `2.359 m`) 위의 수평 천장 판(`|n_z| > 0.45`) 및 실내 중앙 상부 판을 제거한다.
3. **`(c)` 전 카메라 그레이징(Grazing) 삼각형 제거 (`c_grazing_all_cameras`)**:
   - 근거: 삼각형 중심이 시야(FOV) 안에 들어오는 모든 카메라에 대해 시선과 법선의 코사인 절대값 $\max_c |\cos\theta_c| < 0.25$ ($\theta > 75.5^\circ$)인 면을 제거한다.
4. **`(d)` 작은 연결 성분 제거 (`d_small_components`)**:
   - 근거: `1 mm` 정점 병합(vertex welding) 인접 그래프에서 삼각형 수 `500`개 미만인 부유 조각(368개 성분 중 355개 미세 조각)을 제거한다.

| 단계 | 설명 | 남은 면 수 (비율) | 단계 제거 면 수 (비율) | 누적 제거율 | 관측 면 수 | 미관측 면 수 | 면적 (`m²`) | 카메라 전체 덮음 | 카메라 관측 덮음 | 자유시점 미관측 판 비율 (`obl3` / `obl2`) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `raw` | 0. 원본 (`v2_da3_hybrid` raw) | 551,743 (100.00%) | 0 (0.00%) | 0.00% | 344,540 | 207,203 | 233.08 | 91.08% | 84.72% | **28.8% / 17.0%** |
| `a_long_edge` | (a) 긴 변 제거 (`> 8~10×voxel`) | 551,386 (99.94%) | 357 (0.06%) | 0.06% | 344,407 | 206,979 | 232.54 | 90.98% | 84.66% | 28.8% / 17.0% |
| `b_room_ceiling_bounds` | (b) 방 범위 밖·천장 높이 위 제거 (`Z>2.35~2.65m`) | 461,954 (83.73%) | **89,432 (16.21%)** | 16.27% | 311,393 | 150,561 | 204.23 | 83.76% | 80.11% | **3.1% / 5.8%** |
| `c_grazing_all_cameras` | (c) 전 카메라 그레이징 면 제거 (`|cosθ|<0.25`) | 442,861 (80.27%) | **19,093 (3.46%)** | 19.73% | 311,393 | 131,468 | 198.54 | 83.56% | 80.12% | **2.9% / 5.0%** |
| `d_small_components` | (d) 작은 연결 성분 제거 (`< 500 faces`) | **438,703 (79.51%)** | **4,158 (0.75%)** | **20.49%** | **309,761** | **128,942** | **197.58** | **83.45%** | **80.05%** | **2.9% / 4.9%** |

### 5.3 `[v0_baseline | v1_fine_multiframe | v2_da3_hybrid | v2_da3_clean]` 정량 지표 비교 (`v2_clean_summary.md`)

| 변형 | 삼각형 수 | 관측 / 미관측 면 | 면적 (`m²`) | GLB 크기 (MB) | Self PSNR (dB) | Self Blur8 (dB) | Self SSIM | Self 관측 덮음 | Self 전체 덮음 | Edge F1 (≤2px) | LOO PSNR (dB) | LOO Blur8 (dB) | LOO SSIM | LOO 덮음 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `v0_baseline` | 150,571 | 113,165 / 37,406 | 165.13 | 18.66 | 15.20 | 18.96 | 0.5572 | 58.19% | 61.42% | 0.6422 | 12.74 | 16.02 | 0.4362 | 37.39% |
| `v1_fine_multiframe` | 551,682 | 336,409 / 215,273 | 195.58 | 43.91 | **18.02** | **23.35** | **0.6832** | 53.31% | 59.42% | **0.7998** | **14.45** | **18.55** | 0.5036 | 31.66% |
| `v2_da3_hybrid` | 551,743 | 344,540 / 207,203 | 233.08 | 45.84 | 17.46 | 22.42 | 0.6694 | **84.76%** | **91.10%** | 0.7605 | 14.17 | 18.52 | 0.4887 | **70.35%** |
| **`v2_da3_clean`** | **438,703** | **309,761 / 128,942** | **197.58** | **39.87** | **17.41** | **22.35** | **0.6686** | **80.07%** | **83.50%** | **0.7583** | **14.22** | **17.72** | **0.5055** | **66.50%** |

#### 카메라별 전체 / 관측 화면 덮음 (`cam_A..D`)

| 변형 | `cam_A` (전체 / 관측) | `cam_B` (전체 / 관측) | `cam_C` (전체 / 관측) | `cam_D` (전체 / 관측) |
|---|---:|---:|---:|---:|
| `v0_baseline` | `79.7% / 75.1%` | `75.7% / 72.2%` | `76.9% / 72.2%` | `13.4% / 13.4%` |
| `v1_fine_multiframe` | `80.8% / 73.6%` | `78.0% / 70.4%` | `78.8% / 69.3%` | `0.0% / 0.0%` |
| `v2_da3_hybrid` | `89.9% / 84.6%` | `87.9% / 81.0%` | `93.1% / 84.8%` | `93.5% / 88.6%` |
| **`v2_da3_clean`** | **`83.8% / 80.7%`** | **`79.1% / 75.3%`** | **`82.7% / 79.1%`** | **`88.3% / 85.2%`** |

- **8시점 비교 이미지 경로 (`C:/Users/SSAFY/simai-local/geometry-bench/v2_clean/compare/`)**:
  - 자기 시점(Self 4종): `compare_self_cam_{A,B,C,D}.jpg`
  - 한 대 빼고(LOO 4종): `compare_loo_cam_{A,B,C,D}.jpg`
  - 자유 시점(Free 4종): `compare_free_{top,obl1,obl2,obl3}.jpg`

---

## 6. 운영 반영(정적 맵 굽기) 판단 자료: 실행 시간 · GPU 메모리 · 복셀 2 cm 권장안

### 6.1 Pod 실측 단계별 실행 시간 및 GPU 메모리 (`pod_results/logs/run528.try2.log`, H100 80GB 기준)

| 단계 | 실행 명령 / 모듈 | 실측 시간 (s) | 피크 GPU 메모리 (VRAM) | 비고 |
|---|---|---:|---:|---|
| **1. 입력 준비 (`prepare`)** | `reconstruct_tasks prepare` | `2.08 s` | CPU | 이미지 왜곡 폄(`2304×1296`) 및 마스크 생성 |
| **2. MapAnything 518 추론** | `reconstruct_tasks mapanything` | **`1.63 s`** (warm) / `9.8 s` (cold) | **`6,369 MB`** (`5,931 MB` reserved) | 4대 카메라 단일 패스 추론 |
| **3. World 보정 정렬** | `rt.align_to_calibration` (`max_center_m=0.20`) | `< 0.10 s` | CPU | `scale=0.99979`, 중심 오차 `0.083~0.151 m` (`cam_A` `0.151 m` 때문에 기본 `0.10 m` 대신 `0.20 m` 필요), 회전 오차 `1.07°~1.89°` |
| **4. DA3 1008 하이브리드 깊이** | `run_hybrid_depth.py predict --adapter da3 --process-res 1008` | **`21.77 s`** | **`~4,800 MB`** (별도 `/root/da3` venv) | 순수 GPU 추론은 `1.23 s + 0.53 s = 1.76 s`; 프로세스 기동·가중치 로드·NPZ 저장 포함 총 `21.8 s` |
| **5. TSDF 융합 + `sim3` 정렬 + 평면 정리** | `texture_mesh.fuse_mesh` (`voxel=0.015 m`, `550k` faces) | `~35.0 s` | CPU (`open3d`) | `align_views="sim3"`, `plane_cleanup=True` |
| **6. 4단계 기하 후처리 (`v2_clean`)** | `v2_clean.compute_v2_cleanup_stage_masks` | **`~1.20 s`** | CPU (`scipy` + `numpy`) | 천장 판·방 외부·그레이징·부유 조각 `20.5%` 제거 |
| **7. `texrecon` 텍스처 베이킹 (`all` 1회)** | `texrecon` (`area` + `gmi`) + GLB 패키징 | `~42.0 s` (`550k` 면) / `~32.0 s` (`438k` 면) | CPU (OpenMP) | 벤치마크용 LOO 4회 추가 실행은 운영에서 불필요 |
| **총합 (운영 1회 정적 맵 굽기, `voxel=1.5 cm`)** | 단계 1~7 전체 | **약 `93~103 s` (~1.6분)** | **최대 `6.4 GB` VRAM** | MapAnything 종료 후 DA3 가 별도 프로세스로 실행되므로 VRAM 중첩 없음 |

### 6.2 복셀 `2 cm` (`voxel_m = 0.02 m`, `target_faces = 300,000`) 적용 시 예상 및 권장안

| 항목 | `voxel_m = 0.04 m` (현행 `v0`) | `voxel_m = 0.015 m` (`v2_da3_clean`) | **`voxel_m = 0.02 m` + `v2_clean` (운영 권장안)** |
|---|---:|---:|---:|
| **TSDF 절단 거리 (`trunc_m = 8 × voxel`)** | `0.320 m` | `0.120 m` | **`0.160 m`** (다중 카메라 잔여 깊이 오차에 33% 더 강건) |
| **TSDF 복셀 수 비율** | `0.14×` | `1.00×` | **`0.42×` (`(1.5/2.0)³`, 메모리·융합 시간 58% 절감)** |
| **정리 후 최종 삼각형 수** | `~150,000` | `438,703` | **`~240,000 ~ 260,000`** |
| **최종 `.glb` 파일 크기** | `18.7 MB` | `39.9 MB` | **`~22 ~ 25 MB` (웹 뷰어 로딩·렌더링 부담 절반)** |
| **전체 정적 맵 굽기 시간 (1회)** | `~45 s` | `~95 s` | **`~65 ~ 75 s` (`DA3 22 s` + `TSDF 18 s` + `v2_clean 0.8 s` + `texrecon 25 s`)** |

- **최종 운영 반영 권고**:
  1. **깊이 단계**: 세팅 맵 굽기(오프라인/일회성 Job)에 **DA3 1008px 하이브리드 깊이 + `align_views="sim3"` + `plane_cleanup=True`** 를 도입한다. 추가 시간은 **`21.8 s`** 에 불과하며, 현행 파이프라인 최대 결함인 `cam_D` 통구멍(`13.4% → 88.3%`)과 LOO 덮음(`37.4% → 66.5%`)을 해결한다.
  2. **보정 정렬 임계값**: 4대 코너 광각 카메라의 보정 중심 오차(`cam_A` `0.151 m`)를 수용하도록 `rt.align_to_calibration` 의 `max_center_m` 기본값을 `0.10 m → 0.20 m` 로 완화한다.
  3. **메쉬 해상도 및 후처리**: `voxel_m = 0.02 m` (`trunc_voxels = 8`, `target_faces = 300,000`)로 TSDF 를 추출한 뒤, **`texrecon` 실행 전에 `v2_clean` 의 4단계 삼각형 필터(`(a)~(d)`)를 `mesh.ply` 에 먼저 적용**한다. 이렇게 하면 천장/그레이징 면(`~20%`)에 텍스처 아틀라스 슬롯을 낭비하지 않아 `texrecon` 시간도 단축되고 자유 시점(`top`, `obl1~3`)을 덮는 거대 판 아티팩트도 원천 제거된다.
