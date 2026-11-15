# S15P31A101-745 세팅 「카메라 확인」을 통과한 보정을 CALIBRATED 로 올린다

관련 명세: 4.3 5단계(보정 발행·카메라 확인), 4.4(보정 상태 표, CALIBRATED 기준과 세팅 완료 게이트), 4.5(보정 버전), 7.1(PROVISIONAL_CALIB),
12.2(calibration_update), 3.2(구성 묶음). 앞 문서 `S15P31A101-646-setup-camera-check.md`, `S15P31A101-687-camera-check-before-map.md`,
`S15P31A101-736-replay-test-switch.md`, `site-setup-pipeline.md`(-467 세팅 보정 채택). 계약 묶음 v7.63.0(`config_bundle` 1.4.0). DB 마이그레이션 없음.

## 현상과 근거(EC2 DB 읽기, 2026-11-15)

- `system_health` 11-08~11-15: `calibration_state` 는 PROVISIONAL 30,454행·DRIFTED 6,367행, **CALIBRATED 0행**. `calibration_update_message` 전체에
  CALIBRATED 0건.
- `calibration_version`: 11-11 이후 세팅 보정 9개(`cal-lobby-init-*`, G1_AUTO) 모두 `state = PROVISIONAL`.
- 17차 세팅 `18cf35a9`(완료 11-15 16:14 KST, cfg-79): `camera_check = {status: PASSED, note: "…실제 확인 아님"}` 는 Lead 가 DB 로 넣은 값이고
  **검증 기준선(`baseline`)이 없다**. 14차 세팅 `e719bbc1`(완료 11-12 14:07 KST, cfg-72): `camera_check` 기록이 없다(걷기는 했지만 -646 기록 경로 전).
- DRIFTED: `calibration_update` reason `DRIFT_DETECTED` 4건(11-09 1, 11-12 2, 11-15 1). `cameras[]` 값이 정상 카메라 0.5 px·나쁜 카메라
  `max_residual_px + 1 = 4.0` 또는 회전 잔차 p95(20.7·12.6 px)로, 배경 회전 복구(`rotation_recovery._emit_monitor_report`, `_handle_requires_setup` 의
  REQUIRES_SETUP)가 낸 근거가 `Tier0Service._apply_drift_report(source=BACKGROUND)` → `RuntimeCalibrationStore.report_drift` 로 들어간 것이다.
  사람 관절 감시(`SIMAI_TIER0_HUMAN_CALIBRATION=0`, 기본 꺼짐)는 아니다. DRIFTED 는 그 보정 버전이 끝날 때까지(새 보정 채택) 유지된다. 오탐 여부는
  회전 복구 쪽 티켓(-746 등) 범위이고 이 티켓은 바꾸지 않는다.

## 끊긴 곳

| 단계 | 코드 | 결과 |
|---|---|---|
| 워커 보정 문서 | `ai/services/worker/site_setup.py` `build_world_from_pointcloud_and_poses` 의 `CalibrationJudgement(state=PROVISIONAL)` | 세팅 보정 문서는 늘 PROVISIONAL. 품질 게이트·카메라 확인 결과로 다시 판정하는 곳이 없다 |
| 카메라 확인 | 워커 `_run_camera_check` → `setup_session.camera_check` PASSED + `baseline`, Pod `<state>/camera-check/baselines/` | 결과가 보정 상태로 이어지는 경로가 없다(감시 기준선으로만 쓴다) |
| 서버 묶음 | `ConfigBundleService.build` | 보정 문서 원문만 싣는다 |
| Tier 0 | `RuntimeCalibrationStore` 상태 = 문서 `state` + 전환 이력, `Tier0Service._calibration_state` | 문서가 PROVISIONAL 이라 운영 상태도 PROVISIONAL → DEGRADED·R1 넓힘·R2/R3/R4 확인 필요 |

명세 4.3 5단계는 「품질 게이트 통과 → 새 calibration_version(CALIBRATED) 발행 → 카메라 확인」 순서로 적었지만, 4.4 는 CALIBRATED 를 「현장 세팅
검증 충족」이라 하고 카메라 확인 PASSED 까지 통과해야 세팅 완료를 허용한다. 지금 코드는 어느 쪽도 CALIBRATED 를 만들지 않는다.

## 정한 것

**보정 문서는 그대로 PROVISIONAL 로 발행하고, 카메라 확인을 실제로 통과한 근거가 구성 묶음으로 오면 Tier 0 가 운영 상태를 CALIBRATED 로 올린다.**

1. EC2 `ConfigBundleService.cameraCheckVerification`: 묶음에 싣는 보정 V 에 대해, `setup_session` 중 `camera_check.status = PASSED` 이고
   `camera_check.calibration_version` 과 `camera_check.baseline.calibration_version` 이 그 세션 보정과 같으며, 세션 보정이 V 이거나 V 를 그대로 다시 낸
   이전 맵 재사용 세션(-714, `reuse_source_session_id`)의 원 세션이면 `components.calibration.verification = {camera_check: PASSED,
   calibration_version: V, setup_session_id}` 를 싣는다. 묶음을 낼 때마다 다시 본다(세팅 완료·구역 저장·작업 시작 묶음 모두).
2. Tier 0 `Tier0Service._promote_verified_calibration`(구성 묶음 적용 직전, 판정 lock 안, `_adopt_setup_calibration` 의 세 갈래 끝):
   `ConfigBundle.calibration_verification`(근거의 보정 = 묶음 보정일 때만) → `RuntimeCalibrationStore.verification_problem` 이 None 이면
   `mark_verified`: 같은 버전의 `STATE_CHANGED` / `CALIBRATED` 전환을 추가한다(`world_frame_id`·`camera_poses` 포함, 문서·content_hash 불변).
   이 전환은 기존 신뢰 대기열로 `calibration_update` 가 되어 EC2 의 현재 보정 상태(`calibration_update_message`)도 CALIBRATED 가 된다.
3. 올리는 조건: 근거 PASSED, 근거의 보정 = 활성 보정, 활성 운영 상태가 PROVISIONAL, 문서 출처가 CALIBRATED 가능 출처(G1_AUTO)이고 세팅 출처
   (`setup_session_id`). CALIBRATED 는 다시 쓰지 않고(재전송 멱등), **DRIFTED 는 올리지 않는다**(틀어짐이 확인된 보정, 재세팅이 답이다. 4.4).
4. GPU 를 새로 켜면 시작 묶음의 문서(PROVISIONAL)로 시작하고, 연결 뒤 EC2 가 다시 보내는 최근 묶음(`ConfigBundleSender`)의 같은 근거로 그 자리에서
   CALIBRATED 가 된다. 같은 상태 폴더로 Tier 0 만 재시작하면 전환 이력에서 CALIBRATED 를 읽는다.

### 재생 시험(SKIPPED_REPLAY)은 올리지 않는다

-736 의 걷기 생략은 「녹화 파일 재생으로 세팅 완료까지 보기」 위한 시험용 예외이고, 녹화에 여러 명이 나와 실제 다중 카메라 매칭을 검산하지 못한
상태다. 4.4 의 CALIBRATED 는 「현장 세팅 검증 충족」이고 -736 3절도 SKIPPED_REPLAY 세션을 재사용 후보에서 빼며 기준선을 만들지 않는다. 그래서 EC2 는
SKIPPED_REPLAY·SKIPPED(개발 플래그)에 근거를 싣지 않고, 그 보정은 PROVISIONAL(R2·R3·R4 확인 필요, DEGRADED)로 남는다. 재생 시험은 판정이 「확인 필요」로
나오는 것이 정직한 표시다. 같은 이유로 기준선 없는 PASSED(17차처럼 손으로 고친 기록)도 싣지 않는다. 17차 `18cf35a9` 보정은 이 변경 뒤에도 PROVISIONAL 이다.

## 명세와 다른 점(보고)

- 4.3 5단계 「품질 게이트를 통과하면 새 calibration_version(CALIBRATED, 65%)을 발행」 → 실제: 문서는 PROVISIONAL 로 발행하고, 카메라 확인 PASSED
  근거가 구성 묶음으로 오면 운영 상태가 CALIBRATED 가 된다(문서 상태 ≠ 운영 상태, Tier 0 전환 이력이 운영 상태의 정본). 명세 정리 MR 에서 4.3 5단계
  문구를 이 순서로 고치기를 제안한다. 4.4 의 상태 의미·게이트는 그대로다.

## 명세에 없어 정한 것과 근거

| 값 | 근거 |
|---|---|
| 근거는 문서가 아니라 묶음 `components.calibration.verification` | 문서는 동결(content_hash 에 state 포함)이고 카메라 확인은 등록 뒤에 끝난다. 구성 요소 참조는 열린 객체라 1.3.0 Tier 0 도 그대로 받는다 |
| 근거에 `baseline` 필수 | 워커는 PASSED 에만 기준선을 남긴다(계약 site_setup: baseline 이 있으면 PASSED). 손으로 넣은 PASSED 를 걸러낸다 |
| 재사용 보정은 원 세션의 근거를 이어받는다 | -714 재사용은 원 보정을 한 글자도 바꾸지 않고(content_hash 같음) 다시 낸 것이고, 시점 확인으로 카메라가 그대로인지 본다 |
| DRIFTED 는 근거가 와도 그대로 | 4.4: DRIFTED 는 회전 복구(rotfix) 또는 재세팅으로만 풀린다 |
| 올림 저장 실패는 묶음을 거부하지 않는다 | 판정 정본을 막지 않는다. PROVISIONAL(확인 필요)로 남아 안전 쪽이다 |

## 버린 대안

- **워커가 품질 게이트 통과 때 CALIBRATED 문서 발행(4.3 문구 그대로)**: 걷기 확인 전에 동결 문서가 CALIBRATED 가 되고, 나중에 SKIPPED_REPLAY·실패로
  끝나도 되돌릴 수 없다. 재생 시험 보정도 CALIBRATED 가 된다.
- **카메라 확인 통과 뒤 CALIBRATED 새 버전 재발행**: 미리보기·굽기·카메라 확인 기준선·세션 관문이 모두 첫 버전에 묶여 있어 계보가 갈린다.
- **Pod 의 카메라 확인 기준선 파일로 Tier 0 가 스스로 판단**: GPU 를 새로 켜면(새 상태 폴더) 파일이 없어 다시 PROVISIONAL 이 된다.
- **새 구성 요소(`calibration_verification`) 추가**: components 는 닫힌 객체라 1.3.0 Tier 0 가 묶음 전체를 거부한다.

## 시험

- ai `tests/services/tier0/test_setup_calibration_adoption.py`: 근거 실은 세팅 묶음 → 채택(RECALIBRATED, 문서 PROVISIONAL) 뒤 STATE_CHANGED
  CALIBRATED·`_calibration_state`·판정 문맥·health `calibration_state` CALIBRATED·NOMINAL 매핑·확인 필요 규칙 없음, calibration_update 계약, 재시작 유지,
  재전송 멱등 / 근거 없음·다른 버전·SIM 출처는 PROVISIONAL / 이미 아는 보정에 나중 묶음 근거(GPU 재기동 경로) / DRIFTED 는 그대로.
- server `ConfigBundleIntegrationTest.카메라_확인을_통과한_세팅_보정만_묶음에_통과_근거를_싣는다`: PASSED+기준선·재사용은 싣고 SKIPPED_REPLAY·기준선 없는
  PASSED·기록 없음은 안 싣는다. Tier 0 가 받는 알림 1.4.0 계약·매니페스트 components 일치.
- 계약: `validate_examples`(예시 `config_bundle.calibration_verified`, 반례 2), Python 생성 모델 `--check`.

## 잔여 위험

- 실제 GPU 에서 종단 확인 전이다(다음 GPU 세션: 라이브 세팅 걷기 PASSED → cfg 발행 → `calibration_update` STATE_CHANGED → health CALIBRATED·NOMINAL).
- CALIBRATED 가 되면 EC2 `FloorPlanScheduler` 가 BEV 평면도 작업을 처음으로 넣는다(명세 4.1 동작). 워커가 그 작업을 처리하는지 GPU 세션에서 본다.
- CALIBRATED 는 R1 보정 여유·R2/R3/R4 확인 필요를 끈다. 배경 회전 복구가 틀어짐을 놓치면 확인 필요 없이 판정한다(4.4 가 받아들인 운영). 회전 복구
  DRIFTED 오탐이 잦으면(11-12·11-15) 시연 중 DEGRADED 로 돌아간다. 그 판정은 -746 범위.
- `SIMAI_TIER0_HUMAN_CALIBRATION=1` 로 사람 관절 감시를 켜면 `_calibration_state` 가 감시 스냅샷 상태를 먼저 쓴다(지금 기본 꺼짐, 이 티켓은 바꾸지 않음).
- 배포: EC2 만 먼저면 근거를 실어도 옛 Tier 0 는 무시(지금과 같음), Tier 0 만 먼저면 근거가 없어 지금과 같다. 둘 다 바뀌어야 CALIBRATED.
