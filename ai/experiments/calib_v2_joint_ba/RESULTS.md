# S15P31A101-449 — V2 4차(최종): 변수 투영 솔버 수렴·속도 달성 및 점 확대 실험

## 판단 및 채택 권고

1. **중심을 고정하는 회전 보정 단계(Stage A, `--solver projected`)를 채택 후보로 권고한다(단, 독립 실측 검증 후 운영 승격).**
   - 변수 투영(`ProjectedProblem`) 솔버가 저장 트랙 재생(`val01-calib-v2-20261103T172936879695Z`)과 원본 해상도·4개 시각 배경 확대 추출(`val01-calib-v2-20261103T173718765751Z`) 양쪽에서 **모든 단계·민감도 100% 수렴(`all_solves_converged=True`)** 및 **단계당 10초 이내(저장 트랙 0.28~0.58초, 확대 트랙 1.05~5.23초)** 목표를 달성했다.
   - 회전 단계(Stage A)만으로 쌍별 홀드아웃 중앙값이 초기 **AB 1.468 / BC 7.366 / CD 5.823 / DA 1.924 px** → **0.330 / 0.203 / 4.086(n=2) / 0.237 px** (확대 트랙 기준 초기 **2.682 / 7.039 / 3.608 / 1.401 px** → **0.451 / 0.480 / 2.234(n=18) / 0.233 px**)로 크게 줄고, 악화된 쌍은 없으며 데이터 전용 Schur 상대 최소 특이값도 **0.0207(확대 0.0273), 약한 모드 0개**로 관측성이 확보된다.
2. **카메라 중심 이동 단계(Stage B / Stage C)는 관측성 진단에 따라 채택하지 않는다(채택 금지 유지).**
   - 중심 이동을 허용하면 데이터 전용 Schur 야코비안의 최소 특이값이 **3.16e-09 ~ 8.17e-09(상대 2e-11, 약한 모드 1~2개)**로 떨어져 2-view 배경 특징점만으로는 전역 축척과 중심 이동을 유일하게 결정하지 못한다.
   - 초기 baseline 축척 +2% 교란(`initial_scale_plus_2pct`) 시 수렴 후에도 최종 baseline 축척비가 **1.0199 ~ 1.0200**, 중심 위치 차이가 최대 **18.67 cm(확대 18.63 cm)**로 그대로 남는다. 확인된 바닥 Z=0 점(0개)과 독립 정물 치수 검증이 없으므로 중심 이동 결과는 채택을 금지한다(`HOLD`).

---

## 1. 투영 솔버(`projected`) 정지 원인과 수정 요약

1. **초기 회전/중심 패킹의 부동소수점 잔여값으로 인한 초기 신뢰영역 반경(`Delta`) 붕괴 수정 (`numeric.py:pack_cameras`)**:
   - `R[c] = R0[c]`에서 `Rotation.from_matrix(R[c] @ R0[c].T).as_rotvec()`가 정확한 `0`이 아니라 `~1e-15 rad`를 반환했다.
   - `Problem`(joint)은 3D 점 좌표(수 미터)가 함께 패킹되어 `norm(x0 / scale) ≈ 2205`였으나, 카메라 변수만 패킹하는 `ProjectedProblem`은 `Delta = norm(x0 / scale) ≈ 1e-12`로 시작해 첫 TRF 스텝이 `4.6e-15`로 잘리고 `actual_reduction ≈ 2.4e-9`로 `ftol` 조기 종료(`nfev=2`)되었다.
   - `pack_cameras`에서 `1e-12` 미만의 미세 라운딩 오차를 정확히 `0.0`으로 클리핑해 SciPy TRF가 정상 초기 신뢰영역(`Delta = 1.0`)에서 출발하도록 고쳤다.
2. **내부 점 풀이(3×3 safeguarded Newton/IRLS) 정밀화 수렴 조건 및 포락선 정리 해석 야코비안 (`numeric.py:ProjectedProblem`)**:
   - 매 외곽 평가마다 현재 카메라 자세에서 공개 Tier 0 batch DLT(`triangulate_normalized_many`)로 초기화를 수행하고, 픽셀 Huber·바닥·깊이 장벽 목적함수에 대해 정확한 3×3 점 헤시안(Huber 곡률 + 원근 분모 미분 포함, 고유값 하한 보장)과 선탐색, TRF/IRLS 폴백으로 점별 최대 절대 gradient `≤ 1e-8`(허용기준 `1e-4` 대비 충분한 여유)까지 수렴시킨다.
   - 선탐색의 미세 비용 정체 구간(`slack = 1e-12 * max(cost, 1.0)`)에서도 gradient 감소를 허용해 조기 정지를 막고, 외곽 야코비안(`profiled_jacobian`)은 점 정류 방정식 음함수 미분($\partial X^*/\partial c = -H_{XX}^{-1} H_{Xc}$)으로 해석적으로 계산하여 외곽 유한차분(`residual_calls == nfev`)을 제거했다.

---

## 2. 저장 트랙 재생 결과 (`--solver projected` vs 기존 `--solver joint`)

- **실행 Run**: `val01-calib-v2-20261103T172936879695Z` (`C:/Users/SSAFY/simai-local/codex-tmp/449/val01-calib-v2-20261103T172936879695Z/`)
- **입력 트랙**: `val01-calib-v2-20261103T153758154194Z/tracks.json` (단일 중앙값 배경, `feature_scale=0.5`, `max_keypoints=4096`)
- **기존 `joint` Run (`val01-calib-v2-20261103T155843985952Z`)과의 비교**:
  - 기존 `joint`는 3단계 모두 `nfev=4000` 한도 소진으로 **미수렴(`success=False`)**했고 단계당 **167.6 ~ 279.3초**(총 1753.1초)가 걸렸다.
  - 수정된 `projected`는 **3단계와 민감도 4회 모두 완전 수렴(`success=True`, `nfev=5~13`)**했고, 단계 전체 시간은 **A 0.575초 / B 0.327초 / C 0.280초**(총 Run **4.876초**)로 **300배 이상 빠르면서 더 낮은 목적함수 비용**에 도달했다:
    - Stage A 최종 비용: `joint`(미수렴) **234.861** vs `projected`(수렴) **102.729** (동일 해를 `joint` 목적함수에 대입해도 비용 102.729260, 카메라 최대 gradient 1.26e-3으로 정류점 일치 확인)
    - Stage B 최종 비용: `joint`(미수렴) **125.857** vs `projected`(수렴) **75.602**
    - Stage C 최종 비용: `joint`(미수렴) **108.306** vs `projected`(수렴) **61.707**
  - `compare_runs.py`로 미수렴 `joint`와 수렴 `projected`를 비교하면 Stage C는 모든 쌍의 홀드아웃 중앙값 차이가 **0.004 ~ 0.034 px (≤ 0.05 px 이내)**로 일치하고, Stage A/B에서는 `projected`가 미수렴 `joint`가 덜 내려간 잔차를 마저 줄여 홀드아웃 중앙값이 더 낮다(Stage A AB 0.557→0.330 px, BC 0.468→0.203 px, DA 0.386→0.237 px). 합성 노이즈 데이터(`test_camera_only_solve_matches_joint_holdout`)에서는 두 솔버 모두 수렴하므로 Stage A/B 모든 쌍이 ≤ 0.05 px 이내로 일치한다.

### 2.1 단계별 홀드아웃 재투영 오차 (`val01-calib-v2-20261103T172936879695Z`, 저장 트랙)

| 단계 | 구분 | 대상 | n(관측) | 중앙값 px | p90 px | p95 px | 초기 대비 악화 |
|---|---|---|---:|---:|---:|---:|---|
| initial | cameras | cam_A | 39 | 1.914 | 4.250 | 6.936 |  |
| initial | cameras | cam_B | 49 | 4.056 | 8.352 | 8.433 |  |
| initial | cameras | cam_C | 22 | 6.761 | 7.395 | 7.541 |  |
| initial | cameras | cam_D | 12 | 1.748 | 2.052 | 3.184 |  |
| initial | pairs | cam_A-cam_B | 56 | 1.468 | 4.589 | 5.655 |  |
| initial | pairs | cam_B-cam_C | 42 | 7.366 | 8.369 | 8.454 |  |
| initial | pairs | cam_C-cam_D | 2 | 5.823 | 6.838 | 6.965 |  |
| initial | pairs | cam_D-cam_A | 22 | 1.924 | 2.236 | 2.269 |  |
| A_rotation | cameras | cam_A | 39 | 0.328 | 0.751 | 1.089 |  |
| A_rotation | cameras | cam_B | 49 | 0.291 | 0.710 | 0.790 |  |
| A_rotation | cameras | cam_C | 22 | 0.176 | 0.624 | 0.661 |  |
| A_rotation | cameras | cam_D | 12 | 0.236 | 0.927 | 1.964 |  |
| A_rotation | pairs | cam_A-cam_B | 56 | 0.330 | 0.744 | 0.835 |  |
| A_rotation | pairs | cam_B-cam_C | 42 | 0.203 | 0.621 | 0.661 |  |
| A_rotation | pairs | cam_C-cam_D | 2 | 4.086 | 4.798 | 4.887 |  |
| A_rotation | pairs | cam_D-cam_A | 22 | 0.237 | 0.856 | 0.952 |  |
| B_strong | cameras | cam_A | 39 | 0.264 | 0.725 | 0.972 |  |
| B_strong | cameras | cam_B | 49 | 0.230 | 0.637 | 0.674 |  |
| B_strong | cameras | cam_C | 22 | 0.170 | 0.449 | 0.469 |  |
| B_strong | cameras | cam_D | 12 | 0.244 | 0.573 | 0.720 |  |
| B_strong | pairs | cam_A-cam_B | 56 | 0.288 | 0.662 | 0.711 |  |
| B_strong | pairs | cam_B-cam_C | 42 | 0.172 | 0.469 | 0.550 |  |
| B_strong | pairs | cam_C-cam_D | 2 | 0.326 | 0.384 | 0.392 |  |
| B_strong | pairs | cam_D-cam_A | 22 | 0.244 | 0.772 | 0.857 |  |
| C_relaxed_diagnostic | cameras | cam_A | 39 | 0.278 | 0.702 | 0.985 |  |
| C_relaxed_diagnostic | cameras | cam_B | 49 | 0.260 | 0.620 | 0.663 |  |
| C_relaxed_diagnostic | cameras | cam_C | 22 | 0.191 | 0.448 | 0.632 |  |
| C_relaxed_diagnostic | cameras | cam_D | 12 | 0.241 | 0.842 | 0.981 |  |
| C_relaxed_diagnostic | pairs | cam_A-cam_B | 56 | 0.297 | 0.660 | 0.706 |  |
| C_relaxed_diagnostic | pairs | cam_B-cam_C | 42 | 0.222 | 0.448 | 0.526 |  |
| C_relaxed_diagnostic | pairs | cam_C-cam_D | 2 | 1.436 | 1.698 | 1.730 |  |
| C_relaxed_diagnostic | pairs | cam_D-cam_A | 22 | 0.224 | 0.691 | 0.867 |  |

### 2.2 비용·수렴·소요 시간·자세·관측성·민감도 (`val01-calib-v2-20261103T172936879695Z`)

| 단계 | 변수 | 수렴 | 초기 비용 | 최종 비용 | 초기→최종 픽셀 Huber | 초기→최종 이차 prior | nfev/njev | optimality | 솔버 s | 진단 s | 단계 전체 s | 10s 목표 |
|---|---:|---|---:|---:|---|---|---|---:|---:|---:|---:|---|
| A_rotation | 9 | True | 2930.417090 | 102.729260 | 2928.450786→100.677579 | 1.966304→2.051680 | 11/10 | 0.00126632 | 0.506 | 0.056 | 0.575 | True |
| B_strong | 18 | True | 102.729260 | 75.602469 | 100.677579→67.348800 | 2.051680→8.253669 | 5/5 | 0.00200331 | 0.221 | 0.093 | 0.327 | True |
| C_relaxed_diagnostic | 18 | True | 68.397907 | 61.706593 | 67.348800→58.600571 | 1.049107→3.106022 | 5/5 | 0.00106192 | 0.194 | 0.075 | 0.280 | True |

| 단계 | 카메라 | 회전 ° | 중심 이동 cm | 높이 잔차 cm | 높이 비용(이차) | 특징점만 최소 특이값 | 상대 최소 특이값 | 약한 모드 수 | prior 포함 최소 특이값 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A_rotation | cam_A / B / C / D | 1.6493 / 0.9862 / 0.7589 / 0.0000 | 0.000 / 0.000 / 0.000 / 0.000 | -1.413 / -0.000 / 1.390 / -0.065 | 0.998 / 0.000 / 0.966 / 0.002 | 3.38936 | 0.0207432 | 0 | 3.39129 |
| B_strong | cam_A / B / C / D | 1.5466 / 0.9754 / 0.5826 / 0.0000 | 0.807 / 1.103 / 7.627 / 0.000 | -0.733 / 0.663 / 0.608 / -0.065 | 0.268 / 0.220 / 0.185 / 0.002 | 3.16187e-09 | 1.91588e-11 | 2 | 0.50000 |
| C_relaxed_diagnostic | cam_A / B / C / D | 2.4256 / 1.4859 / 1.7033 / 0.0000 | 8.301 / 8.727 / 16.815 / 0.000 | -0.523 / 0.886 / -0.657 / -0.065 | 0.137 / 0.393 / 0.216 / 0.002 | 3.67061e-09 | 2.22410e-11 | 1 | 0.100004 |

- **높이 사전값 확인**: 수렴한 `projected` Stage B에서는 카메라 높이 잔차가 `cam_A -0.733 cm`, `cam_B +0.663 cm`, `cam_C +0.608 cm`로 모두 **±1 cm 이내**에 들어온다(기존 미수렴 `joint`의 `cam_B +3.728 cm`는 미수렴 도중의 값이었다).
- **민감도(Stage C 기준)**: `weight_half`(최대 중심 차이 5.544 cm, 축척비 0.999738, 0.503s), `weight_double`(4.881 cm, 1.000984, 0.870s), `initial_jitter`(2.338 cm, 1.001997, 1.274s), `initial_scale_plus_2pct`(**18.665 cm, 축척비 1.019943**, 0.888s) — 모두 수렴(`True`).

---

## 3. 점 늘리기 실험 (원본 해상도 `feature_scale=1.0` · `max_keypoints=8192` · 4개 시간 구간 중앙값 배경, GPU)

- **실행 Run**: `val01-calib-v2-20261103T173718765751Z` (`C:/Users/SSAFY/simai-local/codex-tmp/449/val01-calib-v2-20261103T173718765751Z/`)
- **방법(메모리 및 누수 방지)**:
  - 카메라별로 한 번에 하나씩만 32개 샘플 프레임을 읽고(약 273 MiB), 이를 4개의 연속 시간 구간(구간당 8프레임)으로 나누어 행 청크 중앙값 배경 4장을 생성(`median_backgrounds`).
  - RTX 4050 Laptop GPU에서 원본 해상도(2304×1296) ALIKED(`max_keypoints=8192`) + LightGlue 매칭 후, 서로 다른 시간 구간에서 중복 검출된 동일 물리 2-view 대응점을 `multiwindow_merge_radius_px=2.0` 이내에서 병합(`deduplicate_multiwindow_matches`)하고, 공간 격자 분할 후 홀드아웃 픽셀 반경 2.0 px 이내의 다른 쌍/시간 구간 학습 트랙도 격리(`quarantine_shared_keypoints`)하여 홀드아웃 누수와 정적 배경점 중복 가중을 모두 차단했다.
- **트랙 확대 결과**:
  - 전체 학습/홀드아웃 트랙이 기존 **257 / 61개**에서 **988 / 268개(약 4배)**로 증가했다.
  - 특히 약한 쌍이던 **C–D가 학습 15→22개, 홀드아웃 1→9개(관측 18개, `holdout_insufficient=False`)**로 늘어 4개 모든 이웃 쌍이 최소 홀드아웃 기준(`min_holdout_per_pair=3`)을 통과했다.

| 쌍 | 기존 학습/홀드아웃 트랙 | 확대 학습/홀드아웃 트랙 | 확대 홀드아웃 관측(n) | 홀드아웃 부족 여부 |
|---|---:|---:|---:|---|
| cam_A-cam_B | 98 / 28 | 402 / 103 | 206 | False |
| cam_B-cam_C | 87 / 21 | 386 / 103 | 206 | False |
| cam_C-cam_D | 15 / 1 | 22 / 9 | 18 | False |
| cam_D-cam_A | 57 / 11 | 178 / 53 | 106 | False |

### 3.1 점 확대 Run 단계별 홀드아웃 및 솔버 요약 (`val01-calib-v2-20261103T173718765751Z`)

| 단계 | AB 중앙값/p90 px (n=206) | BC 중앙값/p90 px (n=206) | CD 중앙값/p90 px (n=18) | DA 중앙값/p90 px (n=106) | 초기→최종 비용 | 수렴 (nfev/njev) | 단계 전체 s |
|---|---|---|---|---|---|---|---|
| initial | 2.682 / 8.783 | 7.039 / 9.564 | 3.608 / 4.611 | 1.401 / 2.006 | - | - | 0.000 |
| A_rotation | 0.451 / 0.846 | 0.480 / 1.207 | 2.234 / 3.208 | 0.233 / 0.555 | 13144.906→458.507 | True (11/10) | 5.228 |
| B_strong | 0.254 / 0.557 | 0.248 / 0.643 | 0.763 / 1.858 | 0.246 / 0.535 | 458.507→189.115 | True (6/6) | 1.925 |
| C_relaxed_diagnostic | 0.239 / 0.546 | 0.238 / 0.597 | 1.049 / 2.127 | 0.231 / 0.481 | 166.226→147.231 | True (5/5) | 1.050 |

- **자세 변화 및 관측성(점 확대 Run)**:
  - **Stage A(회전만)**: 회전 변화 `cam_A 0.2212° / cam_B 1.4720° / cam_C 0.8679° / cam_D 0°`, 중심 이동 0 cm. 데이터 전용 Schur 최소 특이값 **9.19896(상대 0.02734, 약한 모드 0개)**로 기존보다 관측성이 약 2.7배 더 강해졌고, 단계 전체 시간 **5.228초**로 10초 이내에 완전 수렴했다.
  - **Stage B / C(중심 이동 허용)**: 트랙을 4배로 늘려도 2-view 기하 한계로 인해 데이터 전용 Schur 최소 특이값은 **8.17e-09 / 7.41e-09(상대 ~2.2e-11, 약한 모드 1개)**에 머물며, Stage B에서 `cam_B +4.007 cm(중심 이동 6.35 cm)`, `cam_C +4.390 cm(중심 이동 11.95 cm)` 높이 잔차가 발생하고 초기 축척 +2% 교란 시 **18.633 cm(축척비 1.020011)** 중심 이동이 그대로 남는다. 따라서 점을 늘려도 바닥/실측 거리 제약 없이는 중심 이동 단계를 채택할 수 없다.

---

## 4. 재현 및 검증 명령

```powershell
Set-Location 'C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-449-calib-v2-joint-ba'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'

# 1. 단위·수치 시험 전체 (31개)
./.venv-solver/Scripts/python.exe -m unittest discover -s ai/experiments/calib_v2_joint_ba -p "test_*.py" -v

# 2. 저장 트랙 재생 (projected, CPU, ~5초)
./.venv-solver/Scripts/python.exe ai/experiments/calib_v2_joint_ba/v2.py --config ai/experiments/calib_v2_joint_ba/config.json --solver projected --from-tracks C:/Users/SSAFY/simai-local/codex-tmp/449/val01-calib-v2-20261103T153758154194Z/tracks.json

# 3. 원본 해상도·4개 시각 배경 확대 추출 및 투영 BA (GPU, ~232초)
& 'C:/Users/SSAFY/simai-local/calib-v0/.venv/Scripts/python.exe' ai/experiments/calib_v2_joint_ba/v2.py --config C:/Users/SSAFY/simai-local/codex-tmp/449/config-fullscale-8192-w4-cuda.json --solver projected

# 4. Run 기록 정본 스키마 검증
& ./ai/experiments/calib_v2_joint_ba/validate_record.ps1 -Record 'C:/Users/SSAFY/simai-local/codex-tmp/449/val01-calib-v2-20261103T172936879695Z/run_record.json'
& ./ai/experiments/calib_v2_joint_ba/validate_record.ps1 -Record 'C:/Users/SSAFY/simai-local/codex-tmp/449/val01-calib-v2-20261103T173718765751Z/run_record.json'
```
