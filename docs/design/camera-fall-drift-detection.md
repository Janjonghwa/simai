# 카메라 쓰러짐·큰 틀어짐 감지 및 Tier 0 융합 제외 설계

S15P31A101-540. 기획서 §4.3(보정 수명주기 상태: `CALIBRATED`·`PROVISIONAL`·`DRIFTED`·`UNAVAILABLE`), §4.4(틀어짐 감시 및 재세팅 요구), §5.2(다시점 2대 이상 삼각측량), §7.2(R7 감시 조건 상실), `AGENTS.md` 규칙 1(측정 없는 3D 위치 생성 금지)·규칙 2(2대 미만 관절 생성 금지)·규칙 5(보정 없거나 큰 틀어짐 시 정상 공간 판정 금지)·규칙 7(관측과 추정 구분) 기준이다.

코드는 다음 파일에 있다:
- `ai/src/simai_ai/calibration/live_safety.py` (`CandidateConfirmation.observe_drift`, `reset_candidate`, `clear_camera_drift`)
- `ai/src/simai_ai/calibration/rotation_recovery.py` (`Features.valid_ratio`, `extract_features`, `fit_rotation`, `make_candidate`)
- `ai/src/simai_ai/calibration/rotation_worker.py` (`synthesize_samples`, `RotationComputation.compute`)
- `ai/services/tier0/clean_background.py` (`BackgroundCleanBackground._publish_rotation`, `rotation_snapshot`, `recent_signature`)
- `ai/services/tier0/rotation_recovery.py` (`BackgroundRotationRecovery.drifted_cameras`, `_handle_requires_setup`, `run_once`)
- `ai/services/tier0/perception.py` (`Perception.excluded_cameras`, `exclude_cameras`, `replace_rig`, `choose_t`, `step`)
- `ai/services/tier0/judge.py` (`JudgmentLoop._monitoring`)
- `ai/services/tier0/service.py` (`Tier0Service._record_requires_setup`, `_apply_drift_report`)

시험은 `ai/tests/services/tier0/test_rotation_recovery.py`에 있다.

---

## 1. 문제 진단 (재현 결과)

11-08 로비 녹화 중 카메라 2대가 쓰러져 완전히 다른 곳(바닥·벽)을 비추는 사고가 있었다. 기존 코드에서 카메라 1~2대가 30° 이상 회전하거나 완전히 다른 장면을 비출 때(빈 방 / 사람 있음) 어떻게 동작하는지 재현한 결과 4가지 결함이 확인되었다:

1. **사람 관절 기반 `drift_watch`(`ReprojectionMonitor`)의 구조적 불감**:
   - `SIMAI_TIER0_HUMAN_CALIBRATION`이 기본 비활성(`0`)일 뿐 아니라, 활성화하더라도 사람이 없는 빈 방에서는 표본이 0개(`NO_EVIDENCE`)다.
   - 사람이 시야 안에 있어도 카메라가 15°~30° 이상 크게 틀어지거나 쓰러지면 `CrossViewAssociator`의 에피폴라 거리 게이트(`max_epipolar_px = 30.0`)와 2대 삼각측량 잔차 게이트(`max_two_view_residual_px = 12.0`)에서 해당 카메라의 2D 검출이 먼저 탈락한다. `ReprojectionMonitor.observe_association`은 3대 이상 묶인 `ViewCluster` 안의 뷰만 검사하므로, 정작 크게 틀어진 카메라는 표본이 0개(`INSUFFICIENT_DATA`)가 되어 영구히 감지하지 못한다.
2. **배경 기반 `REQUIRES_SETUP`의 단일 주기 즉시 확정(오탐 취약) 및 다중 카메라 누락**:
   - `BackgroundRotationRecovery.run_once`에서 순수 회전 후보(`PROPOSED`)는 `CandidateConfirmation`(2회 연속 확인)을 거쳤으나, `REQUIRES_SETUP`은 단 1회 주기에 즉시 `DRIFTED`와 `clean_background.reset_camera`를 발동했다.
   - `make_candidate`가 첫 번째 실패 카메라 1대만 기록(`if failed is None: failed = ...`)하고, 그 카메라의 배경 누적 창이 리셋되거나 계속 실패하면서 두 번째로 쓰러진 카메라는 `health.re_setup`에 영원히 보고되지 못했다.
3. **부분 가림·조명 밝기 변화에서의 오탐**:
   - `BackgroundCleanBackground._publish_rotation`이 MapAnything 전체 내보내기용 `max_unrevealed_ratio = 0.02`를 그대로 사용해, 정지한 사람이 화면의 2% 이상만 가려도 회전 감시 스냅샷에서 카메라가 빠졌다.
   - `fit_rotation`에서 유효 마스크 비율(`valid_ratio < 0.5`)이나 부분 가림에 의한 특징점 분포 면적 감소(`coverage < 0.12`, 단 회전각 `< 0.5°` 및 잔차 정상)를 `NO_EVIDENCE`/`STABLE`이 아닌 `REQUIRES_SETUP`(`DRIFTED`)으로 처리했다.
   - `synthesize_samples`의 시간 분산 게이트(`variance <= 225`)가 원시 픽셀 밝기만 검사해, 최근 표본 창 내부에서 조명이 밝아지거나 어두워지면(전역 스칼라 밝기 단차 > 15) 모든 픽셀이 `valid = 0`이 되어 `"매칭 부족"`으로 `DRIFTED` 오탐이 났다.
4. **`DRIFTED` 카메라의 Tier 0 3D 융합 미제외**:
   - `Tier0Service._apply_drift_report`가 `calibration.state = "DRIFTED"`와 `health.re_setup`만 갱신하고 `Perception`의 3D 삼각측량(`CrossViewAssociator`), `WorldTrackManager`의 관절 게이트(`JointGate`), `JudgmentLoop._monitoring`(R7 구역 감시)에서 해당 카메라를 제외하지 않아, 틀어진 카메라의 2D 관측이 계속 3D 계산에 섞였다.

---

## 2. 대안 비교

| 대안 | 사람 없는 빈 방 감지 | 다중 카메라 추락 감지 | 가림·조명 오탐 방지 | 규칙 1·2·5 준수 | 계약 호환성 | 판단 |
|---|---|---|---|---|---|---|
| (현행) `drift_watch`(사람 관절) + 1회성 `REQUIRES_SETUP` | 빈 방 감지 불가, 사람 있어도 30° 회전 시 클러스터 탈락으로 불감 | 첫 실패 카메라만 보고·리셋되어 2번째 카메라 누락 | 조명 단차·가림 시 1회 주기로 즉시 `DRIFTED` 오탐, 융합 미제외 | `DRIFTED` 카메라가 3D에 계속 섞임 | 지킴 | 버림 |
| (대안 A) `CrossViewAssociator` 에피폴라 게이트를 500 px로 넓혀 `ReprojectionMonitor`에 큰 틀어짐 표본 강제 공급 | 빈 방에서 여전히 동작 불가 | 사람 있을 때만 가능 | 정상 추적에서 다른 사람 검출끼리 오연결(misbinding) 급증 | 오연결로 잘못된 3D 좌표 생성 | 지킴 | 버림: 정상 다중 객체 추적을 망가뜨리고 빈 방에서 무용지물 |
| (대안 B) 별도의 광류(Optical Flow) / 프레임 차분 전담 데몬 추가 | 쓰러지는 순간만 감지, 이미 쓰러진 뒤 정지하면 놓침 | 가능하나 상태 체계 이중화 | 사람 이동·조명 변화 시 광류 급증으로 오탐 심각 | 별도 상태 체계 필요 | 새 상태 필요 | 버림: 지시서 금지(새 상태 체계 금지) 및 정지 후 감지 불가 |
| **(선택) 기존 배경 비교(`rotation_recovery`) + `CandidateConfirmation` 카메라별 2회 연속 확인 + `NO_EVIDENCE` 가림 분리 + 무채색 전역 밝기 보정 + `Perception` 영구 융합 제외** | 사람 없이 배경만으로 30° 회전·바닥 추락 모두 감지 | 카메라별 독립 확인 및 기확정 `DRIFTED` 카메라 분리로 동시·순차 추락 모두 감지 | 부분 가림·과반 가림·전역 조명 단차·1회성 글리치 모두 방어 | `DRIFTED` 카메라 즉시 제외, `< 2`대 시 `pelvis_xyz`/`joints` 모두 `null`, 재세팅 전 자동 복귀 금지 | I1/I2 계약 변경 없음 | **선택** |

---

## 3. 선택한 임계값과 근거

| 항목 | 선택한 값 | 근거 |
|---|---|---|
| 복구 불가능 큰 회전 경계 (`max_angle_rad`) | `15°` (`math.radians(15)`) | 기존 `RotationConfig.max_angle_rad` 유지. 15° 이하는 중심 고정 순수 회전 후보(`PROPOSED`)로 복구 가능성을 평가하고, 15° 초과는 시야 겹침 급감·비선형 왜곡 잔차 증가로 현장 재세팅(`REQUIRES_SETUP`) 대상으로 분류한다. |
| 최소 배경 특징점 매칭 수 (`min_matches`) | `40` | 기존 `RotationConfig.min_matches` 유지. 완전히 다른 장면(바닥·벽)이나 30° 이상 큰 회전으로 겹치는 시야가 사라지면 상호 최근접(mutual Lowe ratio 0.75) SIFT 매칭 수가 40 미만으로 급감한다. |
| 호모그래피 최소 인라이어 비율 (`min_inlier_ratio`) | `0.60` | 기존 `RotationConfig.min_inlier_ratio` 유지. 매칭점 중 60% 미만이 단일 호모그래피에 정합하면 시차(카메라 위치 이동) 또는 배경 붕괴로 판정한다. |
| 순수 회전 잔차 상한 (`max_residual_px`) 및 특이값 비율 (`min_singular_ratio`) | `3.0 px`, `0.98` | 기존 `RotationConfig` 유지. $M = K^{-1} H K$의 특이값 비 $\sigma_3 / \sigma_1 \ge 0.98$ 및 held-out 95분위 재투영 잔차 $\le 3.0\text{ px}$로 순수 회전과 위치 이동(시차)을 구분한다. |
| `REQUIRES_SETUP` 연속 확인 횟수 (`required`) | **새 표본을 포함한 2회 연속 주기** (`CandidateConfirmation.observe_drift`) | 순수 회전 채택(`CandidateConfirmation.observe`, 2회)과 동일한 안전 기준. 순간적인 가림·블러·조명 과도 현상이 1회 주기에 걸려도 1회차에는 `MonitorStatus.SUSPECT`(`last_status = "CONFIRMING"`)로만 두고, 새 배경 표본(`recent_samples_ms[cid]` 증가)이 들어온 다음 주기에도 연속으로 `REQUIRES_SETUP`일 때만 `DRIFTED`로 확정한다. |
| 유효 배경 마스크 최소 비율 (`valid_ratio` 하한) | `0.50` (50%) | 최근 배경 합성 마스크에서 관측·분산 게이트를 통과한 유효 배경 영역이 화면의 50% 미만일 때 매칭 부족이 발생하면 카메라 틀어짐이 아니라 사람·물체에 의한 과반 가림이므로 `REQUIRES_SETUP` 대신 `NO_EVIDENCE`를 반환한다. |
| 정지 상태에서 낮은 커버리지 허용 (`coverage < min_coverage`) | `geom_ok`이고 `angle_rad < 0.5°`이면 `ROTATION`(`STABLE`), `0.5° <= angle_rad <= 15°`이면 `NO_EVIDENCE` | 부분 가림으로 남은 배경 특징점의 bounding box 면적 비가 `0.12` 미만이어도, 인라이어 비율($\ge 0.6$)·특이값 비($\ge 0.98$)·잔차($\le 3\text{ px}$)가 모두 정상이고 회전각이 $0.5^\circ$ 미만이면 카메라가 움직이지 않은 것이므로 `STABLE`로 판정한다. 회전이 의심되나 면적이 좁으면 무리하게 보정하거나 `DRIFTED`로 오탐하지 않고 `NO_EVIDENCE`로 보류한다. |
| 부분 가림 외점(outlier) 필터링 | `inlier_ratio >= 0.60` 이고 held-out의 $\ge 75\%$가 $\le 3.0\text{ px}$일 때 `> 9.0 px`($3 \times \text{max\_residual\_px}$) 외점 제외 | 검출 박스에 완전히 가려지지 않은 사람·물체 경계의 소수 오매칭이 held-out 20% 표본에 들어가 95분위 잔차를 튀기는 오탐을 막는다. 실제 위치 이동(시차)은 전 깊이 대역에 걸쳐 잔차가 분포하므로 75% 인라이어 조건을 통과하지 못한다. |
| 전역 무채색 조명 단차 보정 (`synthesize_samples`) | 공통 관측 픽셀($\ge 25\%$)의 프레임 간 밝기 차 중앙값 스칼라 보정 후 잔차 표준편차 $\le 15.0$, 채널별 편차 $\le 15.0$ | 창 내부에서 조명이 켜지거나 꺼져 전역 노출/밝기 단차가 생겨도 공간적으로 균일하고(표준편차 $\le 15$) 무채색(B·G·R 채널 이동량 편차 $\le 15$)이면 스칼라 오프셋을 정렬해 분산 게이트(`<= 225`)를 통과시킨다. 국소 물체 이동이나 색 변화(`test_synthesis_rejects_chromatic_changes_and_masked_outliers`)는 기각한다. |

---

## 4. Tier 0 융합 제외 및 서버/뷰어 전달 경로

1. **Tier 0 3D 융합 및 R7 감시 제외**:
   - `Perception.exclude_cameras(camera_ids)`는 확정된 `DRIFTED` 및 `STREAM_CHANGED` 카메라를 `Perception._excluded_cameras`에 영구 등록한다.
   - `Perception.step`은 `excluded = tuple(sorted(set(sync_invalid) | self._excluded_cameras))`를 `CrossViewAssociator.associate(..., exclude_cameras=excluded)`와 `WorldTrackManager.update(assoc, self.rig, excluded)`에 전달한다.
   - `Perception.choose_t`는 제외되지 않은 활성 카메라의 시계만으로 공통 융합 시각 `t`를 계산해, 쓰러진 카메라의 지연이 남은 카메라의 융합 시각을 끌어내리지 않게 한다.
   - `JudgmentLoop._monitoring`은 `set(self.perception.lagging) | set(self.perception.excluded_cameras)`를 `CameraMonitor(fusion_excluded=True)`로 넘겨, 해당 카메라 의존 구역에 R7 감시 저하(`CAUTION`)가 발생하고 감시 복구 근거에서 빠지게 한다.
   - 정상 카메라가 2대 이상 남으면 남은 카메라들로만 3D 위치·관절을 삼각측량하고, 1대 이하(`< 2`대)만 남으면 다시점 클러스터가 형성되지 않아 새 3D 트랙을 만들지 않으며 기존 트랙도 보간 상한 이후 `pelvis_xyz = null`, `joints = [null, ...]`이 된다(규칙 1·2).
   - 한 번 제외된 `DRIFTED` 카메라는 이후 우연히 정상 프레임을 보내도 절대 자동으로 복귀하지 않으며, 오직 재세팅(새 `calibration_version` 활성화 → `Perception.replace_rig` 및 `BackgroundRotationRecovery.activate`)을 통해서만 해제된다.
2. **서버 및 3D 뷰어 전달 경로 (I1/I2 계약 변경 없음)**:
   - Tier 0 `HealthReporter`가 I1 `health` (v2.2.0) 메시지에 `calibration_state: "DRIFTED"`와 `re_setup: [{"camera_id", "reason", "detected_at"}]`를 실어 보내고, 상태 전환 시 I1 `calibration_update` (`reason: "DRIFT_DETECTED"`)를 발행한다.
   - EC2 서버 `ViewerHealthService.java`가 이를 받아 I2 `viewer_health` (v1.3.0)의 `calibration_state`와 `re_setup` 배열로 3D 뷰어에 그대로 전달한다.
   - 프론트엔드 `contractAdapter.ts` / `spatialFrame.ts`가 `calibrationState`와 `reSetup`을 상태 스토어에 반영하고, `ReSetupBanner.tsx`가 카메라별 재세팅 안내 배너(`"카메라 위치가 바뀌었습니다. 현장 세팅을 다시 해 주세요"`)를 표시한다.

---

## 5. 판정 주기 및 CPU 비용 측정

- **실행 구조**: 배경 특징점 추출(SIFT)·상호 매칭·호모그래피 적합은 판정 루프(`judge_lock`) 밖의 전용 자식 프로세스(`simai_ai.calibration.rotation_worker`, `cv2.setNumThreads(1)`, BLAS 스레드 1)에서 수행되며, `BackgroundRotationRecovery` 스레드는 기본 주기 `interval_ms = 60_000 ms`(1분), 단일 주기 작업자 CPU 예산 `cpu_budget_ms = 500 ms` 안에서 결과를 받아 확정 시에만 `judge_lock` 안에서 상태 전이·융합 제외를 반영한다.
- **CPU 비용 측정 결과** (4대 카메라, 960×540 원본 → 320px coarse 특징 추출 기준, `rot.last_cpu_ms`):
  - 기준 배경 최초 특징 추출 포함 주기: 총 `~110–160 ms` (`reference_read` ~8 ms, `reference_extract` ~65 ms, `recent_prepare` ~12 ms, `recent_extract` ~45 ms, `matching` ~6 ms, `estimation` ~4 ms)
  - 기준 배경 캐시 적중 이후 주기(평시 감시 및 `REQUIRES_SETUP` 판정): 총 `~45–65 ms` (작업자 프로세스 CPU 시간, 판정 스레드 점유 `< 1 ms`)
  - `test_judgment_tick_never_blocked_and_cpu_budget_defers_cleanly`에서 배경 감시 작업자가 블로킹된 동안에도 12회 판정 tick이 `< 50 ms` 안에 비차단으로 완료됨을 검증했다.

---

## 6. 남은 위험

1. **장시간 전면 가림 시 감지 지연**: 카메라 앞에 큰 물체나 사람이 계속 서서 화면의 50% 이상을 가리고 있는 동안에는 오탐 방지를 위해 `NO_EVIDENCE`로 보류하므로, 가림과 동시에 카메라가 쓰러진 경우 가림이 걷힌 뒤 2주기(새 배경 표본 2회 누적)가 지나야 `DRIFTED`로 확정된다.
2. **무특징 배경 현장**: 처음부터 벽면·바닥 등 텍스처가 거의 없어 기준 배경 자체의 SIFT 특징점이 40개 미만인 카메라는 배경 특징점 기반 비교만으로 자세를 검증할 수 없다(현장 세팅 단계의 텍스처 확보 가이드를 따름).
3. **비선형 국소 조명(강한 그림자 경계 이동)**: 전역 조도 변화는 스칼라 밝기 정렬로 흡수되지만, 직사광선이나 강한 스포트라이트 그림자 경계가 프레임마다 빠르게 이동해 배경 전체의 공간 잔차 표준편차가 15를 넘으면 해당 창에서 유효 픽셀 비율이 낮아져 `NO_EVIDENCE`로 판정 보류될 수 있다.
