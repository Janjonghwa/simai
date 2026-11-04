# 관제 중 카메라 회전 자동 복구 설계 (S15P31A101-454)

관련 명세: `docs/spec/04-coordinates.md` §4.4(장비 위치 이동·드리프트 감시·재보정), `docs/design/site-setup-pipeline.md` §7(드리프트 감시와 회전 복구 비교), `docs/design/clean-background-accumulator.md`(카메라별 정적 배경 누적).
코드: `ai/src/simai_ai/calibration/rotation_recovery.py`, `ai/services/tier0/rotation_recovery.py`, `ai/services/tier0/clean_background.py`, `ai/services/tier0/settings.py`, `ai/services/tier0/service.py`.
시험: `ai/tests/services/tier0/test_rotation_recovery.py`.

## 배경과 의도

현장 세팅(G1_AUTO)을 마친 뒤 관제(LIVE) 중 삼각대 헤드가 살짝 처지거나 사람이 스쳐 **카메라 위치($C_i$)는 그대로인데 회전($R_i$)만 틀어지는 일**이 자주 발생한다. 세션 11 로비 실험(`.agy/rotcheck.py`)에서 `cam_C`가 13.17° 회전했을 때 기존 사람 관절 기반 드리프트 감시·재보정만으로는 사람이 없는 시간대에 감지·복구할 수 없었고, 사람이 있어도 큰 회전에서는 2D-3D 연관 자체가 깨졌다.

반면 같은 카메라의 초기 기준 배경과 최근 정적 배경 사이의 대응점은 장면 깊이와 무관하게 순수 회전 호모그래피

$$
H = K \, \Delta R \, K^{-1}
$$

를 따른다(세션 11 `cam_C` 실측: 중앙값 잔차 `0.69 px`, $K^{-1} H K$ 특이값 비율 `0.988`). 이 성질을 이용해 **사람이 없어도 정적 배경만으로 순수 회전과 위치 이동(시차)을 구분**하고, 순수 회전이면 카메라 중심 $C_i = -R_i^\top t_i$와 World 좌표계·축척을 보존한 채 회전 행렬만 갱신한다. 삼각대 자체가 옮겨진 위치 이동이나 허용 각도를 넘는 큰 틀어짐은 자동으로 덮어쓰지 않고 **"현장 세팅 다시 하기"** 알림(`DRIFTED` 전환·상태·헬스 저하)으로 운영자에게 안내한다.

## 핵심 선택과 이유

### 1. 특징점 매칭기: 960p 축소 그레이스케일 CPU SIFT + 침식 유효 마스크

- **선택**: `extract_features`(`ai/src/simai_ai/calibration/rotation_recovery.py`)에서 정적 배경 영상을 긴 변 `max_image_side = 960` 그레이스케일로 축소하고, 유효 배경 마스크(`valid_mask`)를 `9×9` 커널로 침식(`cv2.erode`)한 뒤 OpenCV CPU SIFT(`max_features = 1600`)와 상호 최근접 + Lowe 비율(`0.75`) 교차 검증(`match_features`)을 사용한다. 검출된 특징점 좌표는 원본 해상도로 복원한 뒤 `cv2.undistortPoints(..., P=K)`로 렌즈 왜곡을 제거한다.
- **이유**:
  1. **Tier 0 GPU 무간섭**: 관제 중 GPU는 `tier0-detect`(YOLO/RTMPose)가 선점한다. ALIKED+LightGlue를 관제 프로세스에서 주기적으로 돌리면 CUDA 컨텍스트·VRAM·커널 스케줄링 경합이 생길 수 있는 반면, 960p CPU SIFT는 GPU를 전혀 쓰지 않는다.
  2. **충분한 정밀도**: 세션 11 `rotcheck.py` 실측에서 SIFT만으로도 4대 카메라 중 정지 3대는 `0.02°~0.03°` 변화·잔차 `0.15~0.20 px`, 회전한 `cam_C`(`13.17°`)는 잔차 중앙값 `0.69 px`·특이값 비율 `0.988`로 순수 회전을 명확히 분리했다.
  3. **마스크 경계 오염 방지**: 사람 가림이나 분산 마스크 경계에서 SIFT 패치Descriptor가 검은 마스킹 경계를 특징으로 잡지 않도록 유효 마스크를 `9×9` 침식해 정적 배경 내부만 사용한다.

### 2. 순수 회전 vs 위치 이동(시차)·대각 회전 판별 (`fit_rotation` & `evaluate_pairs`)

카메라별로 기준 배경 특징점 $p$와 최근 배경 특징점 $q$를 매칭한 뒤 다음 두 단계를 모두 통과해야만 자동 복구 후보(`PROPOSED`)로 인정한다.

1. **단일 카메라 호모그래피·$SO(3)$ 적합 검사 (`fit_rotation`)**:
   - 전체 매칭의 4/5로 RANSAC 호모그래피 $H$를 추정하고, 적합에 쓰지 않은 **독립 1/5 홀드아웃 대응점(`held`)** 에서 투영 잔차를 평가해 과적합을 막는다.
   - 정규화 행렬 $M = K^{-1} H K$($\det(M) < 0$이면 부호 보정)의 특이값 분해 $M = U \Sigma V^\top$에서 최근접 회전 행렬 $\Delta R = U \operatorname{diag}(1, 1, \det(U V^\top)) V^\top \in SO(3)$와 회전 호모그래피 $H_{\text{rot}} = K \Delta R K^{-1}$를 구한다.
   - **순수 회전 판정 조건**:
     - 매칭 수 `matches >= 40` (`min_matches`)
     - RANSAC 인라이어 비율 `inlier_ratio >= 0.60` (`min_inlier_ratio`)
     - 특징점 분포 면적 비율 `coverage >= 0.12` (`min_coverage`, 한쪽 구석 패치만 매칭된 퇴화 경우 배제)
     - 정규화 호모그래피 최소/최대 특이값 비율 $\sigma_3 / \sigma_1 \ge 0.98$ (`min_singular_ratio`)
     - 홀드아웃 95분위 잔차 $\max(\text{residual\_p95}, \text{rotation\_residual\_p95}) \le 3.0\text{ px}$ (`max_residual_px`)
   - 위 조건을 하나라도 어기면 카메라 위치 이동으로 인한 다중 깊이 시차(parallax) 또는 정적 배경 부족으로 판정해 `REQUIRES_SETUP`을 반환한다.
   - 순수 회전 조건을 만족하더라도 회전각 $\theta = \arccos((\operatorname{tr}(\Delta R)-1)/2)$가 `min_angle_rad`(`0.5°`) 미만이면 `STABLE`(무시), `max_angle_rad`(`15.0°`) 초과이면 시야 이탈이 커 인접 카메라 공통 시야가 깨지므로 `REQUIRES_SETUP`을 반환한다.
2. **인접 카메라 쌍 2-뷰 삼각측량 재투영 개선 검증 (`evaluate_pairs` & `pairs_improve`)**:
   - 회전한 카메라의 자세를 카메라 중심 $C_i = -R_{\text{ref}, i}^\top t_{\text{ref}, i}$ 고정 조건으로 갱신한다(`fixed_center_camera`):
     $$
     R_i' = \operatorname{proj}_{SO(3)}(\Delta R_i \, R_{\text{ref}, i}), \qquad t_i' = -R_i' C_i
     $$
   - 회전 카메라와 인접 카메라 쌍의 최근 배경 특징점 매칭(Fundamental Matrix RANSAC 인라이어 `>= 40`)을 `cv2.triangulatePoints`로 삼각측량해 갱신 전·후 2-뷰 재투영 RMS 오차(`pair_reprojection`)를 비교한다.
   - 회전 카메라를 포함하는 인접 쌍들의 재투영 중앙값 합이 최소 `10%`(`min_pair_gain = 0.10`) 이상 감소하고, 갱신 후 모든 쌍의 95분위 재투영 오차가 `5.0 px`(`max_pair_p95_px`) 이하이며, 비회전 쌍 악화가 `0.5 px`(`max_pair_worsening_px`) 이하여야만 후보를 채택한다. 인접 쌍 재투영이 개선되지 않으면 실제 위치 이동이 섞인 것으로 보고 `REQUIRES_SETUP`으로 전환한다.

### 3. 복구 직후 배경 누적 창 리셋 정책: 초기 기준 배경 보존 + 회전 카메라 최근 창만 리셋

- **결정**:
  1. 회전 복구 후보가 2회 연속 확인을 통과해 제안되거나 `REQUIRES_SETUP`이 발생하면 **즉시 해당 카메라의 최근 배경 누적 창만 `clean_background.reset_camera(cid)`로 비운다**(다른 정지 카메라의 누적 표본은 그대로 유지).
  2. 반면 초기 현장 세팅(또는 마지막 전체 보정)에서 확정된 **기준 배경(`clean_background._reference[cid]`)과 기준 자세(`calibration.safety_reference()[0]`)는 새 회전 배경으로 덮어쓰지 않고 보존**한다.
- **이유**:
  1. **회전 전·후 혼합 표본 오염 차단**: 최근 배경 누적 창에 회전 전 표본이나 흔들리는 도중의 표본이 남아 있으면, 교체 직후 `SwapProbation` 감시 주기에 이전 표본이 재합성되어 거짓 악화 롤백을 유발하거나 이미 복구된 회전을 중복 감지할 수 있다. 회전 카메라의 최근 창을 즉시 비우면 교체 후 새 자세에서 찍힌 정적 배경 표본이 `min_samples_per_camera`만큼 다시 모인 뒤 깨끗하게 감시를 이어간다.
  2. **다중 복구 시 누적 드리프트 방지**: 복구할 때마다 기준 배경(`_reference`)을 최근 배경으로 갈아끼우면, 여러 차례 미세 회전 복구가 반복될 때 호모그래피 추정 오차가 연쇄 누적(random walk)되고 `SwapProbation` 롤백 기준점도 흐려진다. 항상 초기 세팅 기준 배경(`_reference`)과 초기 기준 자세(`safety_reference`)에서 상대 회전 $\Delta R_i$를 직접 구하고, 현재 활성 자세(`active["R"]`)와의 차이가 `min_angle_rad`(`0.5°`) 이상일 때만 후보를 내면 누적 오차 없이 일관된 World 좌표계를 유지할 수 있다.

### 4. 기존 안전장치 통합 및 "현장 세팅 다시 하기" 알림 경로

- **2회 연속 확인 (`CandidateConfirmation`)**:
  - 첫 회전 후보가 감지되면 즉시 교체하지 않고 `CONFIRMING`(`count = 1`) 상태로 둔다.
  - 다음 주기에 **더 최신 시각(`validation_end_ms`)의 배경 표본**에서 다시 같은 카메라 집합이 검출되고, 이전 후보와의 회전각 차이(`consistent_rotation`)가 `confirmation_tolerance_rad`(`0.5°`) 이내일 때만 2회 연속 확인(`count = 2`)을 통과해 `Tier0Service._propose_rotation_calibration`을 호출한다.
  - 카메라가 아직 처지는 중이어서 연속 주기 사이에 회전각이 `0.5°` 넘게 바뀌면 확인 카운트를 `1`로 리셋해 카메라가 완전히 멈춘 뒤 복구한다.
- **판정 틱 경계 원자적 교체와 설정 묶음 재발행**:
  - 후보 채택 시 `_pending_calibration`에 등록되고, 다음 판정 틱 시작(`_begin_calibration_tick`)에서 `RuntimeCalibrationStore.activate_provisional`로 새 `calibration_version`을 원자적으로 활성화한다.
  - 동시에 `calibration.known` 갱신 및 I1 `calibration_update`(`state = "CALIBRATED"`, `reason = "RECALIBRATED"`) 전환 이벤트를 발행해 설정 묶음(config bundle) 재발행 경로와 그대로 연결된다.
- **교체 후 60초 감시와 자동 롤백 (`SwapProbation`)**:
  - 활성화 직후 60초 동안 `SwapProbation`이 가동된다. 감시 기간 안에 배경 검증이 다시 틀어져(`PROBATION_BAD`) 10초 이상 지속되면 자동으로 `prepare_rollback`을 호출해 다음 틱 경계에서 직전 안정 보정으로 되돌리고(`cal-rollback-*`), 추가 자동 복구를 보류(`blocked_reason`)한다.
- **위치 이동·대각 회전 시 "현장 세팅 다시 하기" 알림 (`REQUIRES_SETUP`)**:
  - `fit_rotation`이나 `evaluate_pairs`가 `REQUIRES_SETUP`을 반환하면 보정을 덮어쓰지 않고:
    1. `Tier0Service._record_requires_setup`으로 `calibration_status["re_setup_required"] = True`, `re_setup_camera_id = cid`, `re_setup_reason = "현장 세팅 다시 하기: <cid> (<사유>)"`를 기록한다.
    2. `MonitorStatus.DRIFTED`(`STREAM_CHANGED`면 `MonitorStatus.STREAM_CHANGED`) 보고서를 `_apply_drift_report`에 전달해 `RuntimeCalibrationStore`를 `DRIFTED`로 전환하고 I1 `calibration_update`(`DRIFT_DETECTED`)를 발행한다.
    3. `HealthReporter`의 `calibration_issue`가 `("DRIFTED", "CALIBRATION_DRIFTED")`를 반환해 `system_health`를 `DEGRADED`로 낮춘다.

### 5. Tier 0 판정 루프 무간섭 실행 경계

- `BackgroundRotationRecovery`는 전용 저우선순위 데몬 스레드 `tier0-rotation-recovery`(POSIX `os.nice(10)`)에서 주기적(`interval_ms = 15000`)으로 돈다.
- 판정 틱(`judge.tick()`)은 `_begin_calibration_tick`에서 이미 준비된 `_pending_calibration` 포인터만 확인할 뿐, 배경 합성·SIFT 추출·호모그래피 적합·파일 I/O를 절대 기다리지 않는다.
- 최근 배경 합성(`BackgroundCleanBackground.synthesize_recent`)은 디스크 I/O 없이 메모리 내 최근 `min_samples_per_camera` 표본을 그레이스케일로 변환해 행 묶음(`np.partition` 중앙값 + 분산 마스크)으로 빠르게 합성한다.
- 기준 배경 특징점(`_ref_cache`)과 최근 배경 서명별 특징점(`_recent_cache`)을 카메라별로 캐시하며, 매 단계 `time.thread_time()` 기반 `cpu_budget_ms`(`500 ms`)를 검사한다. 예산을 넘기면 완료된 카메라의 특징점 캐시만 남긴 채 `BUDGET_DEFERRED`로 양보하고 다음 주기에 이어서 처리한다.

## 초기 임계값 설정 (`Tier0Settings.rotation_recovery`)

Tier 0 설정 파일(`tier0.json`)의 `rotation_recovery` 객체로 노출되며 기동 시 유효성을 검증한다.

| 설정 키 | 초기 기본값 | 설명 |
|---|---|---|
| `enabled` | `true` | 배경 기반 회전 자동 복구 워커 활성화 여부 |
| `safety_enabled` | `true` | 2회 연속 확인(`CandidateConfirmation`) 및 교체 후 60초 감시(`SwapProbation`) 적용 |
| `interval_ms` | `15000` (15초) | 회전 감시·복구 스레드 실행 주기 |
| `cpu_budget_ms` | `500` (500ms) | 1회 주기당 스레드 CPU 시간 예산 (`time.thread_time()` 기준, 초과 시 다음 주기로 이월) |
| `min_angle_rad` | `0.0087266` (`0.5°`) | 노이즈와 구분해 회전 복구를 발동하는 최소 회전각 |
| `max_angle_rad` | `0.2617994` (`15.0°`) | 자동 복구를 허용하는 최대 회전각 (초과 시 `REQUIRES_SETUP`) |
| `max_residual_px` | `3.0` | 홀드아웃 1/5 대응점의 $H$ 및 $K \Delta R K^{-1}$ 95분위 투영 잔차 상한 (px) |
| `min_singular_ratio` | `0.98` | $M = K^{-1} H K$의 최소/최대 특이값 비율 $\sigma_3 / \sigma_1$ 하한 |
| `min_inlier_ratio` | `0.60` | 호모그래피 RANSAC 인라이어 비율 하한 |
| `min_matches` | `40` | 단일 카메라 및 인접 카메라 쌍 최소 특징점 매칭 수 |
| `min_coverage` | `0.12` | 매칭된 특징점 경계 상자의 화면 면적 비율 하한 |
| `max_features` | `1600` | 카메라당 SIFT 최대 특징점 수 |
| `max_image_side` | `960` | SIFT 특징점 추출 전 긴 변 축소 해상도 (px) |
| `max_pair_p95_px` | `5.0` | 회전 복구 후 인접 카메라 쌍 2-뷰 삼각측량 재투영 95분위 상한 (px) |
| `min_pair_gain` | `0.10` (`10%`) | 회전 카메라 인접 쌍들의 재투영 중앙값 합 최소 개선율 |
| `max_pair_worsening_px` | `0.5` | 비회전 카메라 쌍의 재투영 중앙값 허용 악화 폭 (px) |
| `confirmation_tolerance_rad` | `0.0087266` (`0.5°`) | 2회 연속 확인 시 이전 후보와 현재 후보 사이 허용 회전각 차이 |

## 세션 12 GPU 현장 시험 절차 (임계값 확정 프로토콜)

세션 12 GPU 워크스테이션 및 4대 실카메라 현장에서 초기 임계값을 최종 확정하기 위해 다음 3단계 시험을 수행한다.

### 사전 준비
1. 4대 카메라 현장 세팅(`G1_AUTO`)을 완료해 초기 활성 보정(`state = "CALIBRATED"`)을 확정한다.
2. 관제 화면 안에 사람이 없는(또는 통행이 적은) 상태에서 약 100초 동안 `tier0-clean-background`가 카메라 4대의 초기 기준 배경(`_reference`, 카메라당 20장)을 모두 내보낼 때까지 대기한다.
3. Tier 0 상태 엔드포인트 또는 로그에서 `status()["calibration"]["rotation_recovery"]["last_status"] == "STABLE"`임을 확인한다.

### 시험 A: 제어된 소각·중각 순수 회전 (3° / 6° / 10°)
1. **조작**: 삼각대 다리 위치는 바닥 마스킹 테이프로 고정한 채, 대상 카메라 1대(예: `cam_C`)의 볼헤드/팬틸트 헤드만 풀어 수평(pan) 또는 수직(tilt)으로 **약 3°**, 이후 **약 6°**, 이후 **약 10°** 회전시킨 뒤 잠근다.
2. **관측**:
   - 회전 직후 최근 배경 창(기본 5초 × 20장 = 100초; 빠른 현장 시험 시 `clean_background.sample_interval_ms = 2000`, `min_samples_per_camera = 10`으로 단축 가능)이 새 자세로 채워진 뒤:
   - 1차 주기에서 `rotation_recovery.last_status == "CONFIRMING"`(`confirmation_count == 1`)으로 진입하는지 확인한다.
   - 다음 새 표본 도착 후 2차 주기에서 `last_status == "PROPOSED"`(`confirmation_count == 2`)가 되고, 다음 판정 틱에 새 `calibration_version`(`cal-rot-*`)으로 교체되며 `calibration_status["probation"] == "교체 뒤 감시 중"`이 되는지 확인한다.
   - 교체 직후 회전한 카메라의 `clean_background` 표본 수(`sample_count`)만 `0`으로 리셋되고 나머지 3대 카메라는 유지되는지 확인한다.
   - 60초 감시 기간 뒤 롤백 없이 `CALIBRATED` 상태로 정착하는지 확인한다.
3. **점검 지표 (`last_report["rotations"][cid]` 및 `last_report["pairs"]`)**:
   - `residual_p95_px`, `rotation_residual_p95_px`가 `3.0 px` 이하(목표 `0.5~2.0 px`)인지 기록한다.
   - `singular_ratio`가 `0.98` 이상인지 기록한다.
   - 인접 쌍 `after.median_px` 감소율과 `after.p95_px`(`<= 5.0 px`)를 기록한다.

### 시험 B: 삼각대 헤드 처짐(sag) 재현 시험
1. **조작**: 카메라 틸트 잠금장치를 미세하게 느슨하게 하여 15~30초에 걸쳐 아래쪽으로 **2°~8° 서서히 처지다가 멈추는 상황**을 재현한다.
2. **관측**:
   - 카메라가 처지는 도중에는 연속 주기 사이 회전각 변화가 `confirmation_tolerance_rad`(`0.5°`)를 넘어 `confirmation_count`가 `1`로 리셋되면서 성급한 교체가 일어나지 않는지 확인한다.
   - 처짐이 멈춘 뒤 연속 2회 확인을 거쳐 최종 정착 각도로 한 번에 복구되는지 확인한다.

### 시험 C: 삼각대 위치 이동(시차) 및 대각 회전(> 15°) 거부 시험
1. **조작 1 (위치 이동)**: 삼각대 전체를 옆으로 **20~40 cm 평행 이동**시킨다(근거리·원거리 정적 구조물이 함께 보이는 구도).
2. **조작 2 (대각 회전)**: 원위치 복구 후 카메라를 **18° 이상** 크게 회전시킨다.
3. **관측**:
   - 두 경우 모두 자동 보정 교체가 일어나지 않고(`_pending_calibration is None`), `rotation_recovery.last_status == "REQUIRES_SETUP"`이 되는지 확인한다.
   - `calibration.state == "DRIFTED"`, `calibration_status["re_setup_required"] == True`, `re_setup_reason`에 `"현장 세팅 다시 하기"` 문구가 채워지고 I1 `calibration_update`(`DRIFT_DETECTED`) 및 `system_health == "DEGRADED"`가 발행되는지 확인한다.
4. **임계값 미세 조정 기준**:
   - 현장 렌즈 왜곡 잔여나 조명 변화로 순수 회전(3°~10°)에서 `rotation_residual_p95_px`가 `2.5~3.5 px` 사이에 걸리면 `max_residual_px`를 `3.5 px`까지 완화하되, 20 cm 평행 이동 시의 `rotation_residual_p95_px` 및 `singular_ratio`와 최소 20% 이상 여유(margin)를 유지하도록 `tier0.json`의 `rotation_recovery` 임계값을 확정한다.

## 버린 대안

- **관제 중 매 주기 ALIKED + LightGlue GPU 매칭 실행**: 특징점 반복성은 높지만 Tier 0 실시간 검출기와 GPU 메모리·스트림을 공유해야 해 관제 틱 지터(jitter) 위험이 있다. 정적 배경 간의 동일 카메라 회전 적합은 시점 변화가 작아 960p CPU SIFT만으로도 충분한 인라이어와 서브픽셀 정밀도를 얻는다.
- **인접 카메라 쌍 검증 없이 단일 카메라 $H = K \Delta R K^{-1}$만으로 즉시 적용**: 카메라 앞에 평면 벽이나 바닥만 크게 보이는 구도에서는 카메라가 평행 이동해도 평면 호모그래피가 성립해 순수 회전처럼 보일 수 있다. 인접 카메라와의 2-뷰 삼각측량 재투영 개선(`evaluate_pairs` & `pairs_improve`)을 필수 조건으로 묶어 평면 착시에 의한 거짓 회전 복구를 차단했다.
- **복구 직후 기준 배경(`_reference`)을 최근 배경으로 덮어쓰기**: 여러 번 미세 복구가 일어날 때 기준 자세와 기준 배경의 호모그래피 추정 오차가 누적될 수 있어 버리고, 초기 세팅 기준 배경(`_reference`)은 보존한 채 회전 카메라의 최근 누적 창만 리셋하기로 결정했다.

## 잔여 위험

- 카메라 시야 전체가 질감이 거의 없는 흰 벽·유리창으로만 채워져 SIFT 매칭 수가 `min_matches`(`40`) 미만이면 자동 복구 대신 `REQUIRES_SETUP`("매칭 부족")으로 운영자에게 재세팅을 요청한다(안전 측 실패).
- 삼각대가 이동했지만 시야 내 모든 배경 특징점이 카메라로부터 매우 먼 단일 평면(실효 무한원점)에만 몰려 있고 인접 카메라 공통 시야에서도 시차가 검출 임계값 이하인 특수 구도에서는 소각 회전으로 근사될 수 있다. 세션 12 실측에서 실내 로비·교실 거리(2~9m) 기준으로 `max_residual_px`와 `min_singular_ratio`의 분리 여유를 최종 점검한다.
