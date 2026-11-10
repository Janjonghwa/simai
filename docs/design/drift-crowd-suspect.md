# 카메라 틀어짐 감시 혼잡 오탐 방지 및 최소 융합 카메라 보호 (`S15P31A101-564`)

- **명세 절**: `docs/spec/04-calibration-coordinate.md` 4.4절(캘리브레이션 상태 머신 `CALIBRATED`/`DRIFTED`), 4.5절(운영 중 재투영 오차 및 배경 기반 감시), `docs/spec/06-degraded-modes.md` 6.2·6.5절(카메라 이탈·보정 틀어짐 시 `DEGRADED` 운용)
- **선행 설계**: `docs/design/camera-fall-drift-detection.md` (`S15P31A101-540`)
- **코드 위치**:
  - `ai/src/simai_ai/calibration/rotation_recovery.py` (`RotationConfig`, `Features`, `extract_features`, `fit_rotation`, `make_candidate`)
  - `ai/src/simai_ai/calibration/rotation_worker.py` (`RotationComputation.compute`)
  - `ai/src/simai_ai/calibration/live_safety.py` (`CandidateConfirmation.observe_drift`)
  - `ai/services/tier0/clean_background.py` (`BackgroundCleanBackground._publish_rotation`)
  - `ai/services/tier0/rotation_recovery.py` (`BackgroundRotationRecovery.run_once`)
  - `ai/services/tier0/perception.py` (`Perception.exclude_cameras`)
  - `ai/services/tier0/service.py` (`Tier0Service._record_requires_setup`, `Tier0Service._apply_drift_report`)
- **시험 위치**:
  - `ai/tests/services/tier0/test_rotation_recovery.py` (`test_multi_camera_fall_exclusion_and_under_two_cameras_null_position`, `test_drift_crowd_suspect_and_min_two_cameras_protection`)

---

## 1. 배경: 13차 모의시험(`11-09` 로비) 오탐 원인

13차 모의시험 로그(`lobby-s13.log.gz`, `pod.tgz`) 분석 결과:
1. `08:18:06 UTC`(17:18 KST)에 현장 세팅 보정(`cal-lobby-init-878286ab-jba2-21714bf3`)이 채택되었을 때 로비에 일반인 7명 이상이 밀집해 있었다(`mean_person_cover_ratio` 0.14~0.27, `never_revealed_ratio` 0.03~0.125).
2. `08:23:14 UTC`(채택 5분 뒤): `cam_B`(`mean_person_cover_ratio=0.199`, `never_revealed_ratio=0.044`)에서 사람 박스 경계 밖 그림자·발끝·부분 가림 잔상이 SIFT 대응점에 섞여 호모그래피 순수 회전 검정(`pure`: `inlier_ratio >= 0.6`, `residual_p95_px <= 3.0px`, `coverage >= 0.12`)이 실패했다. 이때 상단 천장·벽면 덕분에 `valid_ratio >= 0.50`은 충족되어 `fit_rotation`이 `NO_EVIDENCE` 대신 `REQUIRES_SETUP`(`"위치 이동/시차 또는 불충분한 정적 배경"`)을 반환했고, 2회 확인 후 `cam_B`가 `DRIFTED` 및 3D 융합 제외(`excluded_cameras`) 처리되었다.
3. `08:30:47 UTC`(채택 12분 뒤): 같은 `run_once` 주기에 나머지 `cam_A`, `cam_C`, `cam_D` 3대가 동시에 2회 연속 실패로 확정되면서 4대 카메라 전원이 `perception.excluded_cameras`에 들어가 활성 카메라가 0대가 되었고, 재세팅(`08:54:36 UTC`) 전까지 3D 추적 작업자 수가 0명으로 떨어졌다.

---

## 2. 해결 설계 및 결정한 수치·기본값

### 2.1 동시·짧은 창 다수 카메라(2대 이상) 실패 → 배경 회전 복구 경로에서 `SUSPECT` 경고만 유지, 융합 제외 차단
- **규칙**:
  - 배경 회전 복구 경로(`BackgroundRotationRecovery` / `CandidateConfirmation.observe_drift`)에서 **같은 판정 주기**에 2대 이상(`len(failed) >= 2`)이 실패하거나, **짧은 시간 창**(`multi_camera_window_ms = max(interval_ms * 2, 1_000)`, 운영 기본 `30,000ms` = 2주기) 안에 2대 이상이 연달아 실패하면 개별 카메라의 물리적 추락·회전이 아니라 기준 배경·조명·혼잡 환경 문제로 판단한다.
  - 반면 사람 관절 재투영 감시(`ReprojectionMonitor._ingest`) 및 `live_calibration` 복구 경로에서는 2대 카메라가 실제로 함께 움직인 경우(앵커 카메라 2대 이상 잔여)가 복구 대상이므로 공용 `ReprojectionMonitor`에서는 2대 동시 `DRIFTED` 판정을 그대로 유지한다.
- **효과**:
  - `DRIFTED` 전환(`calibration.report_drift`), `health.re_setup` 배너 등재, `perception.exclude_cameras` 3D 융합 제외를 수행하지 않는다.
  - 대신 `MonitorStatus.SUSPECT` 보고를 통해 `Tier0Service.calibration_status["suspect_cameras"]`를 기록하고 `HealthReporter`의 `calibration_issue` 게이트를 거쳐 `health.system_health = "DEGRADED"` 경고를 낸다.

### 2.2 정적 배경 부족 시 판정 보류(`NO_EVIDENCE`)
- **`clean_background` 지표 연계**:
  - `BackgroundCleanBackground._publish_rotation`이 최근 회전 합성 창(최대 10장)의 `mean_person_cover_ratio`와 `never_revealed_ratio`를 함께 발행하고, `rotation_worker`가 이를 `Features` 및 `fit_rotation`에 전달한다.
- **`RotationConfig` 및 `fit_rotation` 보류 기준**:
  - `min_valid_ratio = 0.50` (유효 정적 마스크 비율 하한)
  - `max_person_cover_ratio = 0.10` (회전 창 평균 사람 가림 비율 상한 10%: 13차 정상 구간은 1~3%, 혼잡 오탐 구간은 11.5~25.5%)
  - `max_unrevealed_ratio = 0.02` (`CleanBackgroundConfig.max_unrevealed_ratio`와 동일한 미노출 비율 2%)
  - 정지 앵커 보호: 호모그래피 순수 회전 검정(`pure`)이 실패했더라도 매칭점 중 제자리(`disp <= max_residual_px = 3.0px`)에 머문 정적 랜드마크가 **12개 이상이면서 전체 대응의 35% 이상**이면 카메라 자체가 이동·회전한 것이 아니라 전경 군중·그림자 외점이 섞인 것이므로 `NO_EVIDENCE`로 보류한다(실제 카메라 추락·30° 회전·카메라 중심 이동 시에는 화면 전체 대응점이 이동하므로 제자리 랜드마크 비율이 0에 수렴한다).
  - `NO_EVIDENCE` 상태에서는 해당 카메라의 `drift_counts` 누적을 초기화하여 간헐적 혼잡이 `DRIFTED`로 승격되지 않게 한다.

### 2.3 최소 융합 카메라 수(2대) 보호
- **규칙**:
  - 3D 삼각측량은 최소 2대 카메라가 필요하므로, `Perception.exclude_cameras(..., min_remaining=2)`에서 카메라를 제외했을 때 남는 활성 카메라 수(`len(rig.cameras) - len(excluded)`)가 **2대 미만(`< 2`)**이 되면 해당 카메라는 `excluded_cameras`에 넣지 않고 보호 목록(`protected`)으로 반환한다.
  - `Tier0Service._record_requires_setup` 및 `_apply_drift_report`는 보호된 카메라가 발생하면 융합 제외를 보류하는 대신 `calibration_status["monitoring_degraded_reason"]` 및 `health.re_setup[].reason`에 `"최소 카메라 2대 유지로 융합 제외 보류·감시 저하"` 사유를 기록하고 `health.system_health`를 `"DEGRADED"`로 내린다.
  - 단독 1대 카메라의 실제 큰 회전·추락은 기존(`-540`)과 동일하게 2회 연속 확인 후 `DRIFTED` 확정, 3D 융합 제외(4대 중 1대 제외 → 3대 활성 유지), `health.re_setup` 배너 표시를 유지한다.

---

## 3. 버린 대안

1. **연속 확인 횟수(`required`)만 2회에서 5회 이상으로 늘리는 방안**:
   - 로비 혼잡이 5분 이상 지속되면 결국 동일하게 4대 모두 `DRIFTED`로 빠지며, 반대로 실제 카메라 1대가 쓰러졌을 때 잘못된 관측이 3D 융합을 오염시키는 시간이 2배 이상 길어져 기각했다.
2. **동시 다수 실패 시 가장 잔차가 큰 1대만 제외하는 방안**:
   - 군중이나 기준 배경 불일치로 여러 카메라가 동시에 흔들릴 때 임의로 1대씩 순차 제외하면 결국 2대까지 잘못 제외되어 시야 사각이 커지므로, 다수 동시 실패는 기준·환경 문제(`SUSPECT`)로 분류해 제외하지 않는 쪽을 택했다.

---

## 4. 남은 위험

- 카메라 2대가 순차적으로(짧은 창 30초보다 긴 시간차를 두고) 이미 제외된 상태에서 3번째 카메라가 실제로 물리적 추락을 겪으면, 최소 카메라 2대 보호 규칙에 의해 3번째 카메라는 융합에서 자동 제외되지 않고 `health` 감시 저하 경고만 나간다. 이 경우 운영자가 `health.re_setup` 배너를 보고 현장 재세팅을 수행해야 한다.
- 카메라 바로 앞을 가리지 않고 배경 구조물 자체가 대거 변경(예: 대형 가림막·적재물 이동)되어 정지 랜드마크가 12개 미만으로 떨어지면서 1대 카메라 시야에만 나타나면 실제 카메라 틀어짐과 구분하기 어려워 `REQUIRES_SETUP`이 발생할 수 있다.
