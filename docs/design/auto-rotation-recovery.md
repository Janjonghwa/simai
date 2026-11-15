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
- 배경 스레드가 누적기의 불변 표본을 재사용해 새 표본만 축소한다. 준비 게이트는 `min_samples_per_camera`(기본 20장)를 유지하고, 회전 판단에는 최근 최대 10장을 사용한다. 작업자는 축소 표본의 관측 중앙값과 채널별 분산 마스크를 계산한다. 미관측 영역은 유효 특징에서 제외한다.
- **S15P31A101-494**: 무거운 계산은 `simai_ai.calibration.rotation_worker` 상주 프로세스 하나가 맡는다. CPU 예산은 이 작업자의 `time.process_time()`으로 입력 역직렬화부터 계산 완료까지 측정한다. Tier 0 검출·판정 스레드의 CPU는 섞이지 않는다. OpenCV·BLAS 스레드 제한은 자식 프로세스에만 적용하며 부모 설정은 바꾸지 않는다. 예산 초과 시 `BUDGET_DEFERRED`로 응답하고 완료한 기준·최근 특징과 합성을 보관한다. 서비스 종료 시 자식을 회수한다.

## 초기 임계값 설정 (`Tier0Settings.rotation_recovery`)

Tier 0 설정 파일(`tier0.json`)의 `rotation_recovery` 객체로 노출되며 기동 시 유효성을 검증한다.

| 설정 키 | 초기 기본값 | 설명 |
|---|---|---|
| `enabled` | `true` | 배경 기반 회전 자동 복구 워커 활성화 여부 |
| `safety_enabled` | `true` | 2회 연속 확인(`CandidateConfirmation`) 및 교체 후 60초 감시(`SwapProbation`) 적용 |
| `interval_ms` | `15000` (15초) | 회전 감시·복구 스레드 실행 주기 |
| `cpu_budget_ms` | `1000` (1000ms, -746 에서 500 → 1000) | 회전 전용 작업자 프로세스 CPU 시간 예산. 카메라 단위 추출 시작 전에 검사하고, 못 뽑은 카메라는 다음 주기에 먼저 뽑는다(끝낸 카메라 특징은 30 s 까지 이어씀, `S15P31A101-746-rotation-recovery-budget.md`) |
| `min_angle_rad` | `0.0087266` (`0.5°`) | 노이즈와 구분해 회전 복구를 발동하는 최소 회전각 |
| `max_angle_rad` | `0.2617994` (`15.0°`) | 자동 복구를 허용하는 최대 회전각 (초과 시 `REQUIRES_SETUP`) |
| `max_residual_px` | `3.0` | 홀드아웃 1/5 대응점의 $H$ 및 $K \Delta R K^{-1}$ 95분위 투영 잔차 상한 (px) |
| `min_singular_ratio` | `0.98` | $M = K^{-1} H K$의 최소/최대 특이값 비율 $\sigma_3 / \sigma_1$ 하한 |
| `min_inlier_ratio` | `0.60` | 호모그래피 RANSAC 인라이어 비율 하한 |
| `min_matches` | `40` | 단일 카메라 및 인접 카메라 쌍 최소 특징점 매칭 수 |
| `min_coverage` | `0.12` | 매칭된 특징점 경계 상자의 화면 면적 비율 하한 |
| `max_features` | `1600` | 카메라당 SIFT 최대 특징점 수 |
| `max_image_side` | `960` | SIFT 해상도 상한(px). 기준 특징이 충분하면 320px, 부족하면 이 상한으로 복귀 |
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

## 관리자 화면 "현장 세팅 다시 하기" 알림 (S15P31A101-480)

관련 명세: `docs/spec/04-coordinates.md` §4.4, `docs/spec/12-interfaces.md`(I1 health, I2 viewer_health), 계약 `contracts/messages/health.schema.json` 2.2.0 · `viewer_health.schema.json` 1.3.0(README v7.11.0).
코드: `ai/services/tier0/service.py`(`_record_requires_setup`·`re_setup_field`), `ai/services/tier0/health.py`, `server/.../domain/health/ViewerHealthService.java`, `front/apps/admin/src/features/layout/ReSetupBanner.tsx`·`status.ts`.
시험: `ai/tests/services/tier0/test_rotation_recovery.py`·`test_tier0_health.py`, `server/.../health/HealthIntegrationTest`, `front/apps/admin/src/__tests__/features/layout/ReSetupBanner.test.tsx`·`AppLayout.test.tsx`.

### 배경

위 §4의 `REQUIRES_SETUP`(위치 이동·허용 각도 초과·스트림 변경·매칭 부족)은 Tier 0 안의 `calibration_status["re_setup_*"]`와 health `DEGRADED`로만 남아 관리자 화면까지 오지 않았다. "카메라가 옮겨지면 시스템이 알려 준다"를 시연하려면 같은 신호가 화면의 상단 알림 자리에 떠야 한다. 이번 일은 **알림 전달만** 한다. 판정(회전 복구 가부)·경보 등급·`system_health`는 바꾸지 않는다.

### 선택과 이유

1. **새 메시지가 아니라 기존 health에 선택 필드로 싣는다.** `health` 2.2.0 `re_setup`(마이너, 선택): `[{camera_id, reason, detected_at}]`. 이미 1 Hz로 EC2에 가는 메시지라 별도 전송 경로·재전송·순서 문제가 없고, 이전 Tier 0는 필드가 없으니 "모름"이다.
   - 빈 배열 = "재세팅이 필요한 카메라 없음"(해소), 필드 없음 = 모름. `tier0_warnings`와 같은 규칙이라 수신 측이 이미 익숙하다.
   - 카메라마다 사유가 다를 수 있어 객체 하나(`camera_ids`+`reason`)가 아니라 카메라별 항목으로 했다. `detected_at`은 같은 카메라가 다시 감지돼도 해소되기 전에는 바뀌지 않는다(처음 감지 시각, 사유만 최신).
2. **Tier 0: 해소 시점 = 기존 상태가 풀리는 두 자리.** `_begin_calibration_tick`의 새 보정 활성화와 `_adopt_setup_calibration`의 세팅 보정 채택에서 이미 `re_setup_required=False`로 풀던 곳에 `_re_setup_cameras.clear()`를 더했다. 회전 복구 판정·스레드는 건드리지 않고(`on_requires_setup` 콜백이 이미 카메라 ID와 사유를 넘긴다) 보고 필드만 더했다. 읽기는 판정 lock(`judge_lock`, RLock) 안에서 복사만 한다.
3. **서버: 새 표·Flyway 없이 메모리 최신 상태.** health 원문은 이미 `system_health` 표에 저장되고(`HealthIngestor`), 화면이 쓰는 "현재 상태"는 `HealthState`의 마지막 Tier 0 health다. 덮어쓰는 현재 상태라 이력 표(V39)가 필요 없어 만들지 않았다. `ViewerHealthService`가 `viewer_health` 1.3.0 `re_setup`으로 내준다(GET `/api/health`와 `/topic/health`가 같은 값).
   - Tier 0 연결이 살아 있고 마지막 health가 `re_setup` 배열을 실었을 때만 싣는다. 끊겼거나 필드가 없으면 싣지 않는다(오래된 값을 "재세팅 필요"로 내지 않고, 모름을 "없음"으로도 내지 않는다).
   - Tier 0 payload는 서버가 항목 단위로 검증하지 않으므로 세 필드만 옮기고 형식이 맞지 않는 항목은 버린다(화면 계약을 깨지 않게). `camera_id` 오름차순.
   - `tier0_health` 원문에도 같은 값이 그대로 남는다. 별도 최상위 필드를 둔 이유는 화면이 원문 구조(`tier0_health`는 열린 객체)에 기대지 않고 연결 상태 판단까지 서버가 한 번에 하게 하려는 것이다.
4. **화면: 장애 띠와 따로, 같은 알림 자리에.** `AppLayout`의 `Notices`에서 장애 띠(`FailureBand`) 아래·경보 배너 위에 `ReSetupBanner`를 둔다. 알림 자리는 모든 화면 공통이라 관제(`/`)뿐 아니라 어느 화면에서도 보인다.
   - 문구 "카메라 cam_X 위치가 바뀌었습니다 — 현장 세팅을 다시 하세요" + 카메라별 사유 + "작업 관리로"(`/work`로 이동, 세팅은 작업 관리에서 시작한다). 카메라가 여럿이면 ID를 쉼표로 모은다.
   - 색: 장애 띠의 줄무늬도 위험 등급색(`sev-*`)·지연색도 아닌 주 색(파랑 `bg-primary`). 경보와 섞여 보이지 않게 한다. 시험이 클래스에 `sev-`·`delay`·`stripes`가 없음을 확인한다.
   - EC2와 끊기면(`useLinkAlive`) 마지막으로 받은 안내를 그대로 보이지 않는다(장애 띠가 "현장 연결 끊김"을 알린다). 해소(빈 배열)되면 사라지고, 다시 감지되면 다시 뜬다.

### 버린 대안

- **관리자 토픽을 새로 만든다**: 이미 1초마다 `/topic/health`가 화면으로 가고 접속 때 GET `/api/health`로 채우므로(`useHealthSnapshot`) 같은 길을 쓰는 편이 재접속·초기 표시 문제가 없다.
- **Tier 0 `calibration_update`(DRIFT_DETECTED)로 알린다**: 상태 전이 이벤트라 "지금 재세팅이 필요한 카메라 목록"과 해소를 한 번에 알리기 어렵고, 카메라 ID·사유가 계약에 없다. 이력이 필요하면 이 계약을 쓰고, 이 일은 현재 상태만 다룬다.
- **`re_setup` 상태를 서버 DB에 이력으로 남긴다(Flyway V39)**: 시연에 필요한 것은 "지금"이다. 이력은 health 원문(`system_health` 표)으로 사후에 재구성할 수 있어 새 표를 만들지 않았다.

### 잔여 위험·넘긴 일

- `re_setup`은 새 보정이 활성화되거나 세팅 보정이 채택될 때만 풀린다. 카메라를 원위치로 되돌려 배경이 다시 기준과 맞아도(`STABLE`) 자동으로 풀리지 않는다 — 회전 복구 판정 로직을 건드리지 않는다는 이번 범위 때문이다(기존 `re_setup_required`와 같은 동작). 필요하면 별도 티켓에서 `STABLE` 연속 확인 뒤 해소를 설계한다.
- Tier 0가 재시작하면 메모리의 재세팅 필요 목록이 비어 배너가 사라진다(재시작 뒤 첫 판정 주기에 다시 `REQUIRES_SETUP`이 나오면 다시 뜬다). 체크포인트에 싣지 않았다.
- 사유(`reason`)는 회전 복구가 낸 문장 그대로라 `STREAM_CHANGED: …` 같은 내부 표기가 보일 수 있다. 화면 문구 정리는 후속이다.


## S15P31A101-494: 실행 비용과 안전성 보완

- 계약 `calibration.schema.json`의 `NONE`, `OPENCV_PINHOLE`, `OPENCV_FISHEYE`를 수용한다. 예전 내부 별칭 `OPENCV`는 pinhole과 같은 계산을 한다. fisheye는 전용 `cv2.fisheye.undistortPoints`를 사용한다. 계약 변경은 없다.
- 기준 캐시 키에는 폴더·배경 세대·보정 버전·카메라 파라미터·이미지/마스크 메타·설정을 넣는다. 최근 캐시는 표본 서명(세대·시작·끝 시각)과 카메라 파라미터·추출 설정을 따른다. 새 서명으로 무효화되며 같은 서명의 이월 작업은 합성/추출을 다시 하지 않는다.
- 320px에서 기준 특징이 `max(80, 2 * min_matches)`개 이상이면 그 카메라의 합성·SIFT를 같은 크기로 수행한다. 부족하면 기존 상한 960px를 쓴다. 원본 크기 불일치는 축소 전에 거부한다. 특징 좌표는 원본 픽셀로 되돌리고 기존 3px 잔차·시차·중심 고정·이웃 쌍 개선·2회 확인·롤백 게이트를 유지한다. 무조건 480px로 줄이는 대안은 기존 회전 시험의 매칭 부족으로 버렸다.
- 누적기에서 원본 표본·사람 마스크를 그대로 공유한다. 기존 완성 JPEG 배경은 현재 창과 생성 시점/해상도가 다르고 손실 압축이 있으므로 최근 배경 대신 재사용하지 않는다. 부모는 새 표본의 상한 해상도 축소를, 작업자는 선택 해상도 축소를 각각 표본 시각별로 캐시한다(카메라당 최대 10장). 판정 tick은 이 연산과 IPC를 기다리지 않는다.
- 마스크는 면적 축소 시 완전히 관측된 셀만 남긴다. 관측 수는 `max(2, ceil(n/2))`, 채널별 시간 분산은 225 이하를 적용한다. 전체 관측 시 작은 표본 축의 `np.partition`, 부분 관측 시 미관측 값을 제외한 정렬 중앙값을 쓴다. 색 변화만 있는 영역도 거부한다.
- 상태의 `last_cpu_ms`: `reference_read`, `reference_extract`, `recent_prepare`, `recent_extract`, `matching`, `estimation`, `total`. `cpu_scope=ROTATION_WORKER_PROCESS`, `worker_pid`, `feature_sides`로 작업 경계를 확인한다. 총 CPU에는 역직렬화·작은 조립 비용도 포함되므로 단계 합과 차이가 날 수 있다.
- 시험: `ai/tests/services/tier0/test_rotation_recovery.py`, `test_clean_background.py`. 수동 벤치: `PYTHONPATH=. .venv/Scripts/python tests/services/tier0/bench_rotation_recovery.py --temp-root C:/Users/SSAFY/simai-local/codex-tmp/494`(ai/에서, 2304×1296 합성 4대, seed=494). 측정·환경 한계는 `.agy/notes.md`, `.agy/run_record.json`에 기록한다. 합성 자료의 CPU 측정이며 G1/G2 제품 정확도 근거로 쓰지 않는다.
- 잔여 위험: 960px 복귀 장면·많은 특징/부분 가림·회전 후보의 추가 이웃 쌍 검사 비용은 500ms를 넘을 수 있다. 단계 검사는 단일 OpenCV 호출 중간에 끊지 못한다. 실제 로비의 렌즈·조명·가림·연속 표본 갱신 조건에서 예산 이월과 복구 수렴을 확인해야 한다.
