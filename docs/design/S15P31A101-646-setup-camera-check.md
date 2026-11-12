# S15P31A101-646 세팅 보정 품질 게이트·「카메라 확인」·틀어짐 감시 기준선

관련 명세: 4.3(현장 세팅 파이프라인 4단계 검산), 4.4(CALIBRATED 기준, 작업 중 틀어짐 감시·rotfix), 5.2(Cross-view 연결), 3.2(Tier 0 정본·자원 우선), 12.5(I5 site_setup), 12.6(세팅 REST), 13.4(세팅 화면). 앞 문서 `docs/design/site-setup-pipeline.md`, `docs/design/S15P31A101-602-setup-camera-summary.md`(쌍별 홀드아웃 표시), `docs/design/auto-rotation-recovery.md`, `docs/design/camera-fall-drift-detection.md`.

## 문제(14차 로비 세션, 2026-11-12)

- 세팅 「보정 검증 결과」는 카메라별 높이만 보여 "통과"였는데, 관제 중 B–C 쌍이 에피폴라·잔차로 자주 떨어졌다.
- Stage A 합격 조건은 초기 MapAnything 자세 대비 홀드아웃이 +1 px 이상 나빠지지 않았는지(상대)뿐이었다(`joint_ba_v2.run_stage_a_rotation_ba` 의 `worsened`). 초기 12 px 이면 12.9 px 도 통과하고, 쌍 홀드아웃이 3트랙 미만이면 그 검사마저 건너뛰었다. 카메라 높이 차(`height_checks`)는 표시만 했다.
- -540 관절 재투영 감시는 세팅 보정에 사람 관절 기준선이 없어 관제 첫 10초(60표본)에 즉석 학습(LEARNED_IN_OPERATION)했다. 사람이 방 가장자리로 가자 cam_A 를 DRIFTED 로 확정하고 융합에서 빼 재세팅 전까지 돌려놓지 않았다. 남은 3대로 2대만 보이는 사람이 갈라져 3명이 4명이 됐다.
- 세팅 중에는 Tier 0 가 이전 보정으로 돌아 그 기준 "위치 바뀜" 배너가 떴다.

사용자 결정: 관제에서 뜨는 카메라 경고는 전부 세팅 화면에서 같은 검사로 통과해야 다음으로 넘어갈 수 있다(막음).

## 바꾼 흐름

| 세팅 단계 | 전 | 후 |
|---|---|---|
| (e) JOINT_BA_AND_VERIFY | 상대 악화만 봄 | 절대 품질 게이트(아래). FAILED 면 문제 쌍·카메라를 실은 `quality_gate` 스냅샷(60%)을 보내고 `VALIDATION_REJECTED` 로 끝냄(보정 미등록) |
| (f) 보정 등록(65%) | — | 등록 직후 「카메라 확인」 시작. 미리보기·정밀 맵 굽기와 함께 돈다(배경 수집은 (a)에서 끝났고 미리보기·굽기는 그 묶음을 쓰므로 사람이 걸어도 된다) |
| READY_FOR_ZONES ~ BAKING_TEXTURE | 화면 '다음' 은 정밀 맵 완료 뒤 | '다음' 은 정밀 맵 완료 **그리고** 카메라 확인 통과 뒤. 실패면 원인·「다시 보정」 |
| 결과 COMPLETED 제출 | 굽기 끝나면 바로 | 카메라 확인 판정(PASSED·FAILED)을 기다린다. 실행 상한 20 s 전까지 못 하면 INCOMPLETE |
| 구역 목록 저장·채우기, 세팅 완료(C17) | 미리보기 이후면 받음 | 서버가 `camera_check.status == PASSED` 이고 그 확인의 보정이 세션 보정과 같을 때만 받음(409 `SETUP_CAMERA_CHECK_NOT_PASSED`) |
| 관제(C17 뒤) | 첫 10초 즉석 학습 기준선, DRIFTED 면 영구 제외 | 카메라 확인의 검증 기준선으로 감시. 크게 벗어날 때만 제외, 배경 재검증으로 복귀 |

## 1. 보정 절대 품질 게이트(`joint_ba_v2.evaluate_quality_gate`)

| 항목 | 기준(초기값) | 근거 |
|---|---|---|
| 쌍별 홀드아웃 중앙값 | ≤ 3.0 px | V2 실험(`ai/experiments/calib_v2_joint_ba/RESULTS.md`) Stage A 뒤 쌍 중앙값 0.20~0.48 px, 근거 적은 C–D 2.23 px(n=18). 보정 전(MapAnything 초기) 쌍 중앙값은 7.0~7.4 px. 명세 4.4 목표(중앙값 2 px)는 C–D 같은 약한 쌍을 떨어뜨려 현장 실측 전에는 1 px 여유를 둔다 |
| 쌍별 홀드아웃 p90 | ≤ 6.0 px | 같은 실험 Stage A 뒤 p90 ≤ 3.21 px, 보정 전 8.4~9.6 px. 명세 4.4 p95 5 px 목표와 같은 크기 |
| 쌍별 최소 홀드아웃 트랙 | 5 | 기존 상대 검사 하한 3. 저장 트랙 실험에서 C–D 홀드아웃 1트랙(n=2)일 때 중앙값 4.09 px 로 의미가 없었다. 모자라면 **NEEDS_CHECK**(통과가 아님): 카메라 확인의 필수 쌍이 되어 사람으로 그 쌍을 확인해야 한다 |
| 카메라 높이 차 | ±10 cm | 14차 높이 차 2.5~4.3 cm(최대의 약 2.3배), V2 Stage A 높이 잔차 ±1.4 cm. 10 cm(높이 2.45 m 의 4%)를 넘으면 입력 오타·바닥 오검출이다. 표시 허용(±5 cm, `within_tolerance`)은 그대로 둔다 |

판정: FAIL 하나라도 → FAILED(세팅 실패, 문제 카메라·쌍 안내), 아니면 NEEDS_CHECK 있으면 NEEDS_CHECK, 모두 PASS 면 PASSED. 배경 2-view 홀드아웃은 카메라 중심 오차를 거의 보지 못한다(Stage A 는 중심 고정, 2-view 재투영은 에피폴라 잔차에 가깝다). 그래서 이 게이트만으로는 14차 B–C 같은 문제를 다 잡지 못하고 아래 카메라 확인이 함께 있어야 한다.

## 2. 「카메라 확인」

### 측정 경로: 관제 입력 그대로, 새 보정 후보로 계산

```
워커(site_setup 처리기)             같은 Pod 파일(<state>/camera-check/)            Tier 0(관제 경로)
  보정 등록 → CameraCheckRunner.start ─ request.json(session, 보정 버전, 만료) ─▶ CameraCheckTap 이 1 s 마다 확인
                                                                                Perception.step: views_at(t)(ROI 거르기 전)
  1 s 마다 poll ◀──────────── sessions/<id>/views.jsonl(최대 5 Hz) ◀────── offer → 보조 스레드가 덧붙임
  CameraCheckEvaluator(새 보정):
    CrossViewAssociator.associate → 쌍 근거 통과율·잔차
    leave_one_out_samples → 카메라별 한 카메라 제외 재투영
  요약을 하트비트 site_setup.camera_check 로 ─▶ EC2(세션 저장·관문) ─▶ 화면
  통과 → baselines/<sha256(보정 버전)>.json 저장, request.json 삭제
```

- **같은 계산**: 입력은 Tier 0 가 관제에 쓰는 것과 같은 공통 시각 view(YOLO 관절, 로컬 트랙, 촬영 시각 보간 `LocalTrackletBuffer.views_at`)다. 2D 원본 픽셀이라 보정과 무관하다. 계산은 관제와 같은 클래스·함수(`CrossViewAssociator` 기본 설정, `monitor.leave_one_out_samples` + `MonitorConfig`)를 새 보정 후보로 돈다. 그래서 세팅 중 Tier 0 가 이 세션 보정을 아직 쓰지 않아도(완료 C17 전) 새 보정으로 관제가 낼 결과를 미리 잰다.
- **계산을 Tier 0 가 아니라 워커에서 하는 이유**: 13·14차에 혼잡·아바타로 Tier 0 지연이 190 → 316~767 ms 로 올랐다. 판정 스레드에 두 번째 연결·삼각측량을 얹지 않으려고 Tier 0 는 view 복사·직렬화만 하고(요청이 있을 때만, 최대 5 Hz, 쓰기는 보조 스레드·대기열 64 넘치면 버림), 계산은 워커 프로세스가 한다. 덤으로 view 파일만 있으면 GPU 없이 다시 평가할 수 있다(아래 녹화 재생 검증).
- **I5 대여 안에서 도는 이유**: 워커는 작업을 하나씩 돈다. 카메라 확인을 별도 작업으로 만들면 굽기(약 5분) 뒤로 밀린다. 세팅 작업이 이미 세션 동안 살아 있으므로 그 하트비트로 보고하고, 결과 제출 전 판정을 기다린다(재사용 모드는 굽기가 없어 이때 기다린다). 새 I1 메시지는 만들지 않았다(I1 LIVE 계약 불변).

### 판정 기준(초기값, `CameraCheckConfig`)

| 항목 | 기준 | 근거 |
|---|---|---|
| 걷기 시간 | 사람이 보인 시간 ≥ 30 s, 필수 쌍을 못 채워도 120 s 뒤 판정 | 안내 30~60 s. 관제 감시 창(30 s)과 같은 길이 |
| 쌍 근거 | 그 시각 두 카메라에 view 가 하나씩일 때만, ≥ 30개 | 다른 사람끼리 짝지은 근거(EPIPOLAR 탈락)는 보정과 무관하다. 14차 3명 보행 로그에서 쌍 탈락의 대부분이 EPIPOLAR 였다. 5 Hz 로 두 카메라 동시 관측 약 6 s |
| 쌍 통과율 | ≥ 0.75(PASS+PASS_MARGINAL / 기하 근거, FEW_COMMON_JOINTS·NO_FOV_OVERLAP 제외) | 14차 재시작 뒤(정상으로 본) 30 s 창 57개에서 EPIPOLAR 를 뺀 쌍 통과율: 중앙값 0.85~0.99, p10 0.77~0.90. 문제였던 B–C 는 나쁜 창에서 0.55~0.65(`pod-logs.tgz` lobby-s14.log 추적 계측). 한 사람이면 EPIPOLAR 도 보정 문제라 분모에 넣는다 |
| 쌍 잔차 중앙값 | ≤ 8 px | 관제 연결의 경계 잔차(`CrossViewConfig.residual_marginal_px`). 넘으면 관제가 새로 합치지 않고 IDENTITY_UNCERTAIN 으로 둔다(갈라짐의 직접 원인) |
| 카메라 관측 | 사람이 보인 시각 ≥ 30 | "4대 관측". 못 보면 NOT_OBSERVED → 판정 때 실패 |
| 카메라 한 카메라 제외 재투영 | 표본 ≥ 20 이면 중앙값 ≤ 8 px | 관제 감시의 판정 창 표본 하한·절대 상한(`MonitorConfig.min_window_samples`·`absolute_max_median_px`)과 같다. 이 값을 넘는 보정은 관제가 기준선과 무관하게 틀어짐으로 본다 |
| 쌍 수 | 판정(PASS·FAIL)한 쌍 ≥ 3, 카메라마다 통과 쌍 ≥ 1 | 4대 고리면 옆 4쌍 중 3쌍 |

필수 쌍: 새 보정 기하로 시야가 겹치는 쌍(관제 연결이 건너뛰지 않는 쌍, `geometry.overlap.disjoint_camera_pairs` 밖)과 품질 게이트 NEEDS_CHECK 쌍. 겹치는 쌍이 끝내 근거를 못 채우면 INSUFFICIENT 로 두고 막지 않는다(관제도 그 쌍으로 같은 사람을 묶을 일이 드물다). NEEDS_CHECK 쌍은 못 채우면 실패다(근거 없음 ≠ 통과).

합성 감도(시험 `ai/tests/calibration/test_camera_check.py` 의 방 9×7.5 m, 카메라 4대 높이 2.7 m, 관절 잡음 0.5 px, 24 s 원 걷기): cam2 를 0.5° 돌린 후보는 cam2 한 카메라 제외 재투영 7.5 px(통과 경계), 1° 15.7 px 실패, 1.5° 에서 cam2–cam4 쌍 통과율 0.57 로 실패, 3° 는 cam2 가 클러스터에서 떨어져 쌍 실패. 실제 관절 잡음·시각 오차로 정상 보정의 값이 더 크므로 GPU 세션에서 확인해야 한다(아래).

### 화면

- 2단계(보정·생성)에 「카메라 확인」 카드: 상태(사람 기다리는 중·확인 중·통과·실패·못 마침), 안내 문구, 진행률, 카메라별 관측 수·재투영, 실패 원인 문장, 「다시 보정」.
- 「쌍별 검증」 표(6쌍): 보정 홀드아웃 중앙값/p90 px, 사람 통과율, 판정(통과·실패·확인 중·—).
- '다음' 은 정밀 맵 완료와 카메라 확인 통과(서버 `camera_check_passed`)가 모두 있어야 켜진다. 관문을 모르는 이전 서버(필드 없음)면 막지 않는다(서버도 막지 않는다).
- 「현장 세팅 다시 하기」 배너는 -647 이 관제 화면에서 지웠다(세팅 중 배너 문제는 그쪽에서 해소됨). 이 티켓은 배너를 다루지 않는다.

## 3. 틀어짐 감시 기준선 전달과 관제 정책

### 전달: 같은 Pod 파일

통과하면 워커가 `<state>/camera-check/baselines/<sha256(보정 버전)[:32]>.json`(계약 `camera_check.baseline` 과 같은 모양, 출처 `SETUP_CAMERA_CHECK`)을 쓴다. Tier 0 의 `BackgroundDriftWatch` 는 그 보정 버전의 감시기를 만들 때 저장한 기준선에 검증된 카메라가 없으면 이 파일을 읽어 검증된 기준선(`BaselineRecord.verified_camera_ids`)으로 쓴다. C17 이 이 세션 보정을 실은 구성 묶음을 내면 Tier 0 가 채택하고 첫 측정에서 읽는다. 이후 기준선은 기존 `calibration_baselines.json` 저장으로 재시작에도 이어진다. EC2 에도 세션 `camera_check.baseline` 으로 남는다(기록).

구성 묶음·보정 문서로 보내지 않은 이유:
- 구성 묶음 `components` 는 닫힌 I1 LIVE 계약이다. 새 구성 요소는 Lead 승인이 필요해 이번 범위에서 바꾸지 않았다(보고).
- 보정 문서는 (f) 65% 에 이미 등록됐다. 기준선을 넣으면 새 `calibration_version` 이 생기고, 미리보기·정식 Scene 은 이전 버전으로 구워져 Scene 계보·호환 판정이 어긋난다.
- 남는 위험: Pod 가 바뀌거나(디스크 없음) 파일이 지워지면 기준선이 없다. 그때는 지금처럼 즉석 학습하고, 아래 정책 때문에 학습 기준선만으로는 융합에서 빼지 않는다.

### 관제 정책(Tier 0 `services/tier0/service.py`)

- 관절 재투영 감시(source REPROJECTION)의 DRIFTED: **검증된 기준선이 있고** 창 중앙값 ≥ max(기준선 × 2.5, 기준선 + 5 px) 일 때만 융합에서 뺀다. 아니면 융합에 남기고 의심(`suspect_cameras`)으로만 둔다. 보정 상태 DRIFTED(R2~R4 '확인 필요')는 지금처럼 낸다.
- 배경 회전 복구(source BACKGROUND)의 DRIFTED·재세팅 요청은 지금처럼 바로 뺀다(실제 위치 이동·큰 회전 감지 유지).
- 복귀: 관절 재투영으로 뺀 카메라는 회전 복구 주기마다 배경 비교로 재검증한다. 그 카메라의 배경이 세팅 기준과 순수 회전으로 맞고 최소 각도(0.5°) 미만인 주기가 연속 3번(약 45 s, 주기 15 s)이면 융합에 되돌리고 그 카메라의 감시 확정을 푼다(`ReprojectionMonitor.reset_camera`, 기준선은 유지해 다시 넘으면 20 s 지속을 다시 쌓는다). 같은 보정에서 카메라마다 2번까지만 되돌리고, 그 뒤 다시 빠지면 재세팅 안내(`health.re_setup`)로 둔다.
- `rotation_recovery.make_candidate`: 순수 회전 적합은 정상인데 각도 ≤ 1.5° 이고 인접 쌍이 나아지지 않으면(`pairs_improve` 불충족) REQUIRES_SETUP 대신 NO_EVIDENCE(배경·보정 잡음). 큰 회전(> 1.5°)·위치 이동(시차, 순수 회전 적합 실패)은 지금처럼 REQUIRES_SETUP.

## 계약·마이그레이션

- 계약 v7.39.0(마이너, v7.38.0 -654 다음): I5 `messages/site_setup` 1.14.0(1.13.0 은 -652) — progress·result 선택 `quality_gate`(`$defs/quality_gate`)·`camera_check`(`$defs/camera_check`), 공용 `$defs/check_problem`. `job_callback` 2.21.0·`job_heartbeat` 2.13.0(제목만). 예시 `job_heartbeat.site_setup_camera_check`·`job_heartbeat.site_setup_quality_failed`·`job_callback.site_setup_camera_check`, 반례 `invalid/job_callback.site_setup_camera_check_failed_with_baseline`·`invalid/job_heartbeat.site_setup_camera_check_incomplete_without_problem`·`invalid/job_heartbeat.site_setup_quality_gate_unknown_verdict`. Python 생성 모델 갱신. I1(world_state·health·config_bundle 등)·providers/calibration 변경 없음.
- 마이그레이션 V62(V61 은 -654): `setup_session.quality_gate jsonb`, `setup_session.camera_check jsonb`(마지막 값, 없는 스냅샷은 이전 값 유지).
- REST: `GET /api/setup-sessions/{id}` 에 `quality_gate`·`camera_check`(원문)·`camera_check_passed`(관문 판정). 409 `SETUP_CAMERA_CHECK_NOT_PASSED`: `PUT /api/setup-sessions/{id}/zones`, `POST .../zones/seed`, `POST .../complete`.
- 배포 순서: **EC2 먼저, 워커 이미지 나중, 그리고 둘 다 있어야 세팅을 끝낼 수 있다.** 이전 EC2 는 결과(job_callback)를 계약으로 검사해 새 필드가 든 결과를 닫힌 객체로 거부할 수 있다(-597 과 같음). 반대로 새 EC2 + 이전 워커는 `camera_check` 가 오지 않아 관문이 구역 단계·세팅 완료를 막는다(의도된 막음). 화면은 `camera_check_passed` 가 없는 이전 서버면 막지 않는다.

## 명세와 다른 점(보고)

- 명세 4.4 는 "관제 중 사람 관절(LOO 재투영)로 틀어짐을 감시하는 경로(`monitor` 사람 LOO …)는 두지 않는다" 고 적었지만, 코드에는 -540 이후 관절 재투영 감시(`BackgroundDriftWatch` + `ReprojectionMonitor`)가 살아 있다. 이번 티켓은 사용자 지시대로 그 감시의 기준선·제외 조건을 고쳤다. 명세 정리 MR 에서 4.4 를 코드에 맞추거나 감시를 끌지 정해야 한다.
- 명세 4.4 CALIBRATED 기준(홀드아웃 중앙값 2 px·p95 5 px, 높이 ±5 cm)보다 이번 게이트(3 px·p90 6 px, ±10 cm)가 느슨하다. 근거는 위 표. 사람 걷기 확인이 더 엄격한 실제 기준 역할을 한다.
- 명세 4.3 세팅 흐름에 「카메라 확인」 단계(사람 30~60 s 걷기)가 새로 생겼다(무보행 보정 원칙과 별개: 보정에는 쓰지 않고 검증·기준선에만 쓴다).

## 녹화 재생 검증(GPU 세션에서 Lead 가)

YOLO 검출이 GPU 라 view 만들기는 GPU 가 필요하고, 평가는 노트북 CPU 로 된다.

1. 14차 녹화 `C:\simai-local\recordings\live-20261112-135633` 을 Tier 0 파일 재생(`docs/design/tier0-recording-replay.md`)으로 돌린다. 재생 상태 폴더는 `state_dir/replay/run-…/` 이다.
2. 재생이 뜬 뒤 `state_dir/replay/run-…/camera-check/request.json` 에 `{"setup_session_id": "<아무 UUID>", "calibration_version": "cal-lobby-init-c85f2a97-jba2-59e38a25", "expires_at_ms": <충분히 뒤>}` 를 쓴다. Tier 0 가 `sessions/<UUID>/views.jsonl` 을 쓴다.
3. 노트북으로 가져와 `cd ai && uv run python -m services.worker.camera_check --views views.jsonl --calibration <14차 보정 문서>`(상태 `state/calibrations/0962…json`). 종료 코드 0 = PASSED. 14차 보정에서 B–C 쌍 통과율·재투영 값과 PASSED/FAILED 를 기록해 기준을 맞춘다.
4. 같은 views 로 3명 보행 구간도 넣어 본다(한 사람 규칙 때문에 쌍 근거가 거의 안 쌓이는 것이 정상).

## GPU 세션에서 확인할 것

- 실제 세팅에서 카메라 확인이 몇 초에 끝나는지, 정상 보정의 한 카메라 제외 재투영 중앙값(8 px 상한 대비 여유)과 쌍 통과율 분포. 정상 보정이 8 px 근처면 상한을 다시 정한다.
- 굽기 중 Tier 0 지연(view 내보내기 5 Hz 가 지연을 늘리지 않는지, 상태 파일 `camera_check_tap` 은 없음 → 로그 "카메라 확인 입력 내보내기 시작"으로 확인).
- C17 뒤 Tier 0 로그 "세팅 카메라 확인 기준선으로 감시" 가 뜨는지, 3명 10분 보행에서 `excluded_cameras == []`·새 ID 0 인지.
- 카메라를 실제로 3° 돌리면 회전 복구 rotfix, 15 cm 옮기면 재세팅 안내가 뜨는지(배경 경로 유지 확인). 1° 이하 흔들림에서 재세팅 요청이 없는지.

## 버린 대안

- Tier 0 안에서 새 보정으로 연관·재투영을 계산: 판정 스레드 GIL·지연 위험(13·14차). 보조 스레드여도 같은 프로세스 GIL 을 쓴다.
- 카메라 확인을 별도 I5 작업(`camera_check`)으로: 워커가 하나라 굽기 뒤로 밀리고, 다시 확인하려면 작업 상태 전이가 늘어난다.
- 새 I1 메시지(EC2 ↔ Tier 0 직접 요청·보고): I1 계약 변경이라 Lead 승인 사항. 같은 Pod 파일 + I5 하트비트로 충분하다.
- 기준선을 새 보정 버전으로 등록: Scene 계보가 어긋난다(위).
- 관제 자동 융합 제외 전면 끄기(Carbon 방안 B): 실제로 틀어진 카메라가 융합에 섞인다. 검증 기준선 + 큰 벗어남 + 재검증 복귀로 대신했다.
- 2명 이상일 때 쌍마다 가장 잘 맞는 짝으로 세기: 상대 카메라에 그 사람이 없을 때 다른 사람과의 EPIPOLAR 를 실패로 세어 정상 보정을 떨어뜨린다. 안내를 "한 사람"으로 하고 한 사람 시각만 센다.

## 코드·시험 위치

- AI: `ai/src/simai_ai/calibration/camera_check.py`(평가기·직렬화·기준선 파일), `ai/src/simai_ai/calibration/extrinsics/joint_ba_v2.py`(`evaluate_quality_gate`), `ai/src/simai_ai/calibration/monitor.py`(`reset_camera`, 출처 상수), `ai/src/simai_ai/calibration/rotation_recovery.py`(작은 회전 잡음), `ai/services/worker/camera_check.py`(실행기·오프라인 평가 CLI), `ai/services/worker/site_setup.py`·`site_setup_stages.py`(조립), `ai/services/tier0/camera_check_tap.py`, `perception.py`(`observe_views`·`include_cameras`), `drift_watch.py`(기준선 조회·카메라 풀기), `rotation_recovery.py`(`on_report`), `service.py`(제외 조건·재검증), `settings.py`(`camera_check_enabled`, `camera_check_dir`).
- 서버: `server/src/main/java/com/ssafy/simai/domain/setup/SetupCameraCheck.java`, `SetupService.java`(저장·조회), `SetupCompletionService.java`(C17 관문), `domain/zone/SetupZonePlanService.java`(구역 목록 관문), `global/exception/ErrorCode.java`, `db/migration/V62__setup_session_camera_check.sql`.
- 화면: `front/apps/admin/src/features/setup/cameraCheck.ts`, `SetupCalibrateStep.tsx`(카드·표), `setupActions.ts`, `SetupPage.tsx`, `types.ts`, `mocks/setup.ts`(autoProgress 카메라 확인·관문).
- 시험: `ai/tests/calibration/test_camera_check.py`, `ai/tests/services/worker/test_camera_check_runner.py`, `ai/tests/services/worker/test_site_setup.py`(게이트·카메라 확인 조립), `ai/tests/services/tier0/test_camera_check_policy.py`, 서버 `SetupCompleteIntegrationTest`·`SetupZonePlanIntegrationTest`(관문), 화면 `__tests__/features/setup/cameraCheck.test.ts`·`SetupCalibrateStep.test.tsx`.

## 넘긴 일

- 명세 4.3·4.4 갱신(카메라 확인 단계, 관절 재투영 감시의 존재와 기준선·제외 조건, 게이트 수치).
- 기준선을 Pod 밖(EC2)에서 다시 받는 경로가 필요하면 구성 묶음 구성 요소 추가(I1, Lead 승인).
- 카메라 확인 "다시 확인"(같은 보정으로 다시 재기): 지금은 실패면 다시 보정한다. 걷기가 모자라 INCOMPLETE 가 잦으면 서버 → 워커 신호를 더한다.
