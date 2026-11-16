# S15P31A101-760 세팅 카메라 확인 걷기로 외부 파라미터 다듬기

관련 명세: 4.3(세팅 보정 3단계 배경 BA·5단계 「카메라 확인」), 4.4(CALIBRATED 승격 근거), 5.2(Cross-view 연결). 앞 문서
`docs/design/S15P31A101-646-setup-camera-check.md`(카메라 확인), `docs/design/S15P31A101-694-camera-check-criteria.md`(기준·잡음 바닥).
설계 근거는 Lead 가 받은 분석안(Carbon, 2026-11-16)이고 코드로 확인하며 따랐다. 다르게 한 점은 6절에 적었다.

## 1. 문제

2026-11-16 로비 라이브 세팅에서 걷기 「카메라 확인」이 대각 쌍 때문에 계속 실패했다. 2차 보정 결과: 옆 쌍 1–2 100%/2.4 px,
2–3 98%/6.7 px, 3–4 92%/6.8 px, 1–4 100%/2.5 px, 대각 1–3 0%(98건 전부 에피폴라 밖), 2–4 72%/12.1 px. 배경 홀드아웃은 0.2~0.7 px.

원인(코드 확인):

- 배경 BA Stage A(`joint_ba_v2.run_stage_a_rotation_ba`)는 옆 4쌍(`PAIRS`)의 2-view 점만 묶는다. 대각 배경 매칭은 7~12개라 묶을 수 없다.
- Stage A 는 카메라 중심을 MapAnything 값에 고정하고(`center_sigma=None`, 3 자유도) 회전만 맞춘다. 배경 특징은 대부분 먼 벽(7~9 m)에
  있어서 중심 오차 ΔC 를 회전 Δθ ≈ ΔC/d_wall 로 보상하고 배경 홀드아웃은 0.x px 가 된다. 방 가운데(3.5~4.5 m) 사람에서는
  ΔC·(1/d_person − 1/d_wall) 의 시차가 남는다. 옆 쌍은 에피폴라선이 거의 수평이라 이 오차가 비스듬히만 들어가고, 마주 보는 대각은
  에피폴라선이 거의 수직이라 정면으로 들어간다.
- -759(!713)는 새 맵에서도 대각을 참고 쌍으로 돌린 임시 조치였다. 관제 `CrossViewAssociator` 는 6쌍을 모두 보므로 대각이 틀어진 보정은
  3대 관측·대각 2대 관측에서 사람을 쪼갠다.
- -694 실측: 같은 로비 걷기 절반으로 (R, t) 를 맞추고 나머지 절반으로 재면 6쌍 100%, 잔차 1.3~3.4 px 였다.

## 2. 바꾼 흐름

새 맵(FULL_SETUP) 세팅의 카메라 확인:

1. 막는 쌍을 넘기지 않는다(`blocking_pairs=[]`) → 대각을 포함한 모든 쌍이 판정에 든다(-759 이전 판정). REUSE_SCENE 은 -759 그대로(품질
   게이트의 쌍만 막는다). 새 맵 시도는 4대에 한 사람씩 함께 보인 시각이 `CameraCheckConfig.min_four_view_ticks`(80)만큼 모이고 카메라별
   LOO 표본이 20 을 채울 때까지(또는 max_duration 120 s 까지) 끝내지 않는다. 80 = 다듬기 Holdout(시각의 28%)에서 카메라별 LOO 20(관제
   감시 창 하한·기준선 하한)을 채우는 20 / 0.28 ≈ 72 + 귀속 실패 여유 10%(5 Hz 로 4대 동시 관측 16 s, 실측 약 4 Hz 면 20 s). 보정과
   무관한 관측 개수라 어긋난 보정에서도 쌓이므로 막는 쌍이 이미 실패해도 기다린다(LOO 표본은 막는 쌍이 실패하면 -759 처럼 기다리지 않는다).
   화면은 기존 summary 의 `loo_samples`·`progress_pct` 로 진행이 보인다.
2. 시도가 기하 실패(`PASS_RATE_LOW`·`RESIDUAL_HIGH`·`REPROJECTION_HIGH`·`NO_PASSING_PAIR` 중 하나가 문제에 있음)로 끝나면 그 시도의
   views.jsonl 전체로 다듬기를 한 번 돌린다(`CameraCheckRunner.refine_walk` → `walk_refinement.refine_extrinsics_from_walk`).
   표본 부족(INCOMPLETE)만으로 끝난 시도는 돌리지 않는다(다시 걷기가 답이다).
3. 채택되면 새 `calibration_version` 문서를 Stage A 와 같은 발행 주입점(`ProductionSiteSetupStages.publish`)으로 올린다. 덮어쓰지 않고
   버전별 키(`calibration-{version}.json`)로 올리며, 발행 주입점이 `setup_session_id` 와 `previous_calibration_version`(세팅 시작 때 Tier 0
   활성 보정 = `self._prior`)을 넣는다. Stage A 버전은 Tier 0 에 활성화된 적이 없으므로 계보에 넣지 않는다(`runtime.setup_candidate_problem`
   은 previous == 활성일 때만 채택). 같은 함수가 작업 폴더 `calibration.json` 을 새 문서로 바꾸므로 미리보기·굽기·바닥 감시 범위가 새
   보정으로 계산된다. 두 번째 발행이 계보를 지키는 것은 기존 시험 `test_publish_keeps_prior_document_and_uploads_new_version` 이 본다.
4. 카메라 확인 요약은 새 버전의 Holdout 판정(PASSED, `baseline` 포함)이고, `attempt.calibration_version` 도 새 버전이다. 실행기가 새 버전
   기준선을 `baselines/` 에 쓴다(Tier 0 가 그 보정을 채택할 때 틀어짐 감시 기준선). 스냅샷의 `calibration_version`·`height_checks`(새 카메라
   높이로 다시 계산)·`artifacts.calibration_document` 를 새 버전으로 바꾼 뒤 맵 단계로 간다. EC2 는 스냅샷마다 보정 문서를 등록하므로
   (`SetupArtifactRegistrar.registerCalibration`) 서버 수정은 없다. 확인의 보정 = 세션 보정 = 기준선 보정이라 CALIBRATED 근거
   (`ConfigBundleService.cameraCheckVerification`)도 그대로 성립한다.
5. 채택하지 않으면(거절·표본 부족·오류) 새 문서를 내지 않고, 같은 시도 view 를 -759 규칙(품질 게이트 옆 쌍만 막고 대각은 참고,
   카메라별 재투영 표본 대기)으로 다시 판정한다(`CameraCheckRunner.recheck_walk`, 시연 회귀 방지). PASSED 면 Stage A 버전 그대로 통과로
   끝내고(그 버전 기준선을 씀) 로그와 `walk_refinement.json` 의 `fallback_759` 에 「대각 미해결로 -759 기준 통과」를 남긴다. 그것도 실패면
   지금 실패 흐름(FAILED + `user_wait`, 「확인만 다시」)을 잇는다.

다듬기 기록(전/후 쌍별 통과율·잔차, 카메라별 중심 이동·회전 변화·높이, 부트스트랩, 풀이 비용, 채택 여부·거절 사유)은 워커 로그(한 줄)와
같은 시도 폴더의 `walk_refinement.json` 에 남긴다. 보정 문서에는 카메라별 `reprojection_error_px` 를 Holdout 한 카메라 제외 재투영
중앙값·p95 로 채운다(Stage A 배경 홀드아웃 값은 새 자세의 검증값이 아니다, `rotation_recovery` 와 같은 규칙).

## 3. 다듬기(`ai/src/simai_ai/calibration/walk_refinement.py`)

### 시간 분할

사람이 처음 보인 시각을 0 으로 Fit 2.4 s → 완충 0.6 s(버림) → Holdout 1.4 s → 완충 0.6 s(주기 5 s, 쓰는 틱 Fit 63% : Holdout 37%).
앞뒤 절반 분할은 걸은 구역이 갈려 Holdout 쌍 표본이 빌 수 있고, 틱 교대는 이웃 틱(0.2 s) 자세·검출 잡음이 비슷해 Holdout 이 독립이 아니다.
완충 0.6 s 는 5 Hz 로 3틱, 보행 0.7~0.9 m 다.

### 관측

카메라 확인과 같은 기준: 그 시각 그 카메라에 view 가 하나(한 사람). 관절은 COCO17 몸통·사지 12개(`EVAL_JOINTS`, 5~16), score ≥ 0.5.
3대 이상에서 보인 (시각, 관절)만 점으로 쓰고, 그런 점이 150개 미만이면 2대 점도 쓴다. K·왜곡·영상 크기는 고정이라 관측은
`RigCamera.undistort_many` 로 한 번 왜곡을 지운 이상적 핀홀 픽셀로 쓴다. σ = 2.5 px × 관절 배율 / sqrt(score), 관절 배율은 기존
`KEYPOINT_NOISE_SCALE`(COCO κ)를 평가 관절 최솟값(손목)으로 나눈 값(어깨 1.27, 팔꿈치 1.16, 손목 1.0, 엉덩이 1.73, 무릎 1.40, 발목 1.44).
-694 의 관절별 LOO(엉덩이 9.7 px 로 가장 큼)와 방향이 맞다.

### 풀이

변수: 카메라마다 ω(3)·ΔC(3) (R = exp([ω]×)·R0, C = C0 + ΔC) + 점마다 X(3). 비용 = Σ ρ(‖(π(X) − u)/σ‖) + 사전항
(ΔC_xy/0.08 m, ΔC_z/0.02 m, ω/4°)². 점 블록(3×3)을 소거한 Schur 보수 LM(카메라 4대 고정 칸이라 밀집 칸으로 계산, 해석 야코비안,
왼쪽 SO(3) 야코비안). 시작 점은 관측 카메라 DLT.

- 1단계: Cauchy, 경계 2.4σ(≈ 6 px), LM 15회. 대각 30~60 px 초기 오차가 기울기를 망치지 않게 한다.
- 18 px 를 넘는 관측(반사·가림 오검출)을 버린다.
- 2단계: Huber, 경계 2.0σ(≈ 5 px), LM 15회.

한 대만 크게 틀어졌으면(그 카메라가 낀 쌍 모두 Fit 통과율 < 0.25, 나머지 세 대 쌍 모두 ≥ 0.70) 그 카메라 회전을 세 대 삼각측량 관절로 먼저
맞춘다(`recalibration.realign_rotation`, 중심 고정 Kabsch). 그 카메라의 회전 사전 중심은 부트스트랩 회전이고, 회전 상한은 15° 다.

수렴 뒤 Stage A 카메라 중심에 대한 수평 강체(ψ, Δx, Δy, 최소제곱)와 평균 Δz 를 빼서 World 바닥 Z=0·축·world_anchor 를 지킨다(축척은
사전항이 잡는다).

### 채택(Holdout 만)

Holdout 평가는 `camera_check.evaluate_views`(평가기와 같은 판정·기준선)를 Holdout 시각에만 돌린다. 기준은 카메라 확인 기준에서 표본
하한만 Holdout 크기에 맞춘다: 쌍 표본 ≥ 15, 카메라 관측 ≥ 10, LOO 표본 ≥ 20, 사람이 보인 시간 ≥ 20 s(통과율 0.75·잔차 8 px·LOO 상한
12 px 는 그대로). 모두 만족해야 채택한다. LOO 표본 하한은 관제 감시 창 하한(`MonitorConfig.min_window_samples` 20)과 같게 둔다. 기준선(`baseline_record`)이
이 하한으로 카메라를 넣으므로, 낮추면 PASS 인데 기준선이 빠진 카메라가 생겨 관제에서 검증되지 않은 채 남는다.

| 조건 | 거절 사유 |
|---|---|
| Fit 점 ≥ 240, 카메라마다 Fit 관측 ≥ 120 | `FEW_FIT_POINTS` |
| Holdout 판정이 끝남(표본 부족이면 INCOMPLETE) | `FEW_HOLDOUT` |
| 모든 쌍 PASS(대각 포함) | `HOLDOUT_PAIR_FAILED` |
| 모든 카메라 LOO PASS(표본 ≥ 20) | `HOLDOUT_CAMERA_FAILED` |
| 4대 모두 기준선 있음 | `BASELINE_MISSING` |
| 중심 수평 이동 ≤ 0.20 m, 수직 ≤ 0.06 m | `CENTER_SHIFT_XY`, `CENTER_SHIFT_Z` |
| 새 카메라 높이 − 입력 높이 ≤ 0.10 m | `HEIGHT_DIFF` |
| 회전 변화 ≤ 8°(부트스트랩을 실제로 적용한 카메라만 15°) | `ROTATION_CHANGE` |
| Holdout 최저 쌍 통과율이 Stage A 보다 오름(Stage A 가 이미 1.0 이면 최대 쌍 잔차·최대 LOO 가 모두 줄어듦) | `NO_IMPROVEMENT` |
| 입력이 보정 계약을 지켰으면 새 문서도 지킴 | `SCHEMA_INVALID` |

새 버전 = `{Stage A 버전}-walk-{content_hash 앞 8자리}`(160자 상한 안에서 앞을 줄임), `created_at` = max(Stage A, 마지막 걷기 시각).
같은 입력이면 같은 문서·같은 버전이다.

## 4. 정한 수치와 근거

| 값 | 근거 |
|---|---|
| Fit 2.4 / 완충 0.6 / Holdout 1.4 s | 위 시간 분할. 30 s 걷기면 Holdout 약 8.4 s(5 Hz 약 40틱)로 쌍 표본 15 를 채운다 |
| σ 2.5 px | -694 잡음 바닥 보정의 쌍 잔차 1.3~3.4 px |
| 중심 σ xy 0.08 m, z 0.02 m | MapAnything 수평 중심 오차 15~25 cm 는 풀고 축척·원점 표류는 막는다. 높이는 관리자 확인 값 |
| 회전 σ 4° | 16일 대각 오차 1.5~2.5° 를 2σ 안에 둔다 |
| 손실 6 px → 18 px 버림 → 5 px | 초기 대각 30~60 px 를 받아들이고 수렴 뒤 오검출만 버린다 |
| Fit 점 240·카메라 120 | 12관절 기준 20시각(약 8 s 걷기)·10시각. 그보다 적으면 6 자유도가 사전항으로만 정해진다 |
| Holdout 표본 쌍 15·카메라 관측 10·LOO 20, 20 s | 쌍·관측은 카메라 확인 하한(30·30)의 Holdout 비율 몫. LOO 는 기준선 하한(20)과 같게 두고 시도가 4대 동시 관측 80 시각을 기다려 채운다. 시도는 30 s 넘게 걸어야 끝나고 첫 Holdout 블록은 3 s 뒤 시작한다 |
| 물리 상한 0.20 / 0.06 / 0.10 m, 8° / 15° | 사전 σ 의 2.5~3배. 높이 0.10 m 는 품질 게이트 높이 절대 상한과 같다 |

### 맵 정렬 기준

미리보기·굽기는 MapAnything Run 을 보정 World 로 맞춘다(`reconstruct_tasks.solve_fixed_alignment`, 카메라별 중심 잔차 ≤ 0.10 m·회전 ≤ 5°).
다듬은 보정은 같은 World 에서 카메라를 최대 20 cm·8°(부트스트랩 15°) 옮기므로 이 문서로 맞추면 정렬이 실패할 수 있다(코드 확인: 문턱은
보정 문서 카메라와 MapAnything 카메라의 similarity 잔차에 걸린다). 그래서 발행 주입점이 대여에서 처음 발행한 보정(Stage A, MapAnything
중심 그대로)을 `alignment_reference.json` 으로 남기고 정렬은 그 문서로 한다. 다듬기는 Stage A 카메라 중심으로 게이지를 맞추므로 World 가
같다. calibration.json(바닥 감시 범위·굽기 투영)은 다듬은 보정이다. 문턱은 바꾸지 않았다(Stage A 정렬에는 그대로 맞다).

## 5. 시험(합성, `ai/tests/calibration/test_walk_refinement.py`)

test_camera_check 의 합성 리그(9 × 7.5 m 네 모서리 2.7 m, 105°)와 투영 헬퍼를 쓰고, 한 사람이 타원을 두 바퀴(약 0.9 m/s, 36 s, 5 Hz,
관절 잡음 2 px) 걷는다. Holdout 쌍 잔차 중앙값(px)·통과율:

| 경우 | Stage A(Holdout) | 다듬은 뒤(Holdout) | 결과 |
|---|---|---|---|
| cam2 +3° | 2–4 0%/15.9, 1–2 61%/7.0, cam2 LOO 44.8 | 6쌍 100%, 1.10~1.53, LOO 2.2~2.5 | 채택, 회전 오차(정답 대비) ≤ 0.05° |
| cam3 중심 +20 cm + 8 m 벽 역회전 보상(cam3 가까이 걷기) | 1–3 71%/5.5, 2–3·3–4 84%, 1–2·1–4·2–4 100%, cam3 LOO 10.8 | 6쌍 100%, 1.10~1.49, LOO 1.9~2.8 | 채택(중심 수평 이동 최대 0.10 m) |
| 같은 경우 회전만(중심 묶음) | 위와 같음 | 6쌍 100%, 1.42~3.29, cam3 LOO 4.6 | 6 자유도보다 잔차가 크다(비교용) |
| cam2 +10° | cam2 가 낀 쌍 모두 0%, LOO 표본 없음 | 6쌍 100%, 1.10~1.53 | 부트스트랩 후 채택, 회전 오차 ≤ 0.05° |
| cam2 초점 +25% | 1–2 12%, 2–3 37%, 2–4 12% | Holdout 은 통과(2.6~7.6 px) | 중심 0.37~0.48 m 이동 → `CENTER_SHIFT_XY` 거절 |
| cam2 시각 500 ms 밀림 | 1–2 49%, 2–3 53%, 2–4 29% | Holdout 은 통과(1.5~2.6 px) | 중심 0.24~0.43 m·cam2 회전 7.1° → `CENTER_SHIFT_XY` 거절 |
| 8틱만 | — | — | `FEW_FIT_POINTS` |

워커 시험(`ai/tests/services/worker/test_site_setup.py`): 기하 실패 → 다듬기 채택 → 두 번째 발행(Stage A 계보를 싣지 않음)·결과·카메라
확인·기준선·높이 확인·보정 문서 artifacts·미리보기·굽기·바닥 감시 범위가 모두 새 버전, 「확인만 다시」 없음 / 채택 실패 → 발행 없음,
시도 2 로 기존 흐름 / Stage A 바로 통과·INCOMPLETE → 다듬기 안 돌림 / REUSE_SCENE → 품질 게이트 쌍만 막고 다듬지 않음. 실행기 시험
(`test_camera_check_runner.py`): 실패한 시도 views.jsonl 로 다듬기·기록 파일·새 버전 기준선.

## 6. 분석안과 다르게 한 점

- 풀이기: `bundle/solver.py`(Schur LM)는 카메라별 중심·회전 사전항, 손실 종류·경계, 일반 K(fx≠fy·skew)와 5계수 왜곡을 지원하지 않고,
  `joint_ba_v2.Problem` 은 2-view 트랙 전용(scipy TRF)이라 그대로 쓰지 못했다. 공용 풀이기를 고치면 adjust·session 경로에 영향이 가서,
  이 모듈 안에 같은 방식(점 블록 소거 Schur LM)의 작은 풀이기를 두었다. 재사용: `RigCamera` 왜곡 제거, `KEYPOINT_NOISE_SCALE`,
  `recalibration.realign_rotation`, `camera_check` 평가기(`evaluate_views` 로 분리).
- 관절 배율: 분석안 숫자(1.0/1.15/1.25/1.4)는 "기존 `KEYPOINT_NOISE_SCALE` 재사용"이라 했으나 실제 표와 달라 표를 그대로 정규화했다.
  분석안이 말한 `camera_check.EVAL_JOINTS` 는 없어 이 모듈에 정의했다.
- 개선 조건: Stage A 의 Holdout 최저 통과율이 이미 1.0 이면(재투영·잔차로만 실패) "통과율 상승"이 불가능해 최대 쌍 잔차·최대 LOO 감소로
  대신했다.
- 기록 위치: 분석안·티켓은 "보정 문서 validation 의 자유 필드"를 말했으나 계약 `validation` 은 `additionalProperties: false` 이고 자유
  필드가 없다. 계약을 바꾸지 않고 워커 로그와 시도 폴더 `walk_refinement.json` 에 남겼다. 문서에는 기존 필드 `reprojection_error_px`
  만 Holdout LOO 로 채운다.
- 기준선이 비면 채택하지 않는다(`BASELINE_MISSING`, 분석안에 없음): CALIBRATED 근거가 기준선 버전 일치를 본다.
- 하지 않은 것: 표본이 모자랄 때 걷기를 15~20 s 더 받아 재평가(지금은 「확인만 다시」), 화면 가장자리 5% 관측 σ 1.5배.

## 7. 잔여 위험

- Holdout 은 외부 파라미터로 흡수되는 오차를 가려내지 못한다. 합성에서 초점 +25%·시각 500 ms 는 Holdout 을 통과했고 물리 상한으로만
  거절됐다. 더 작은 내부 파라미터 오차·일정한 시각 밀림(느린 등속 원 보행이면 카메라를 방 중심 둘레로 돌린 것과 같다)은 상한 안의
  외부 파라미터 변화로 흡수되어 채택될 수 있다.
- World 는 Stage A 카메라 중심으로 정한다. Stage A 중심이 틀린 만큼은 수평 강체 맞춤이 카메라들에 나눠 담는다(합성 20 cm 경우 World 가
  정답 대비 약 0.5° 돌고 카메라 중심이 최대 0.1 m 어긋남, 카메라끼리는 맞음). 바닥 Z=0 은 평균 높이로, 기울기는 높이 사전항(2 cm)으로
  지킨다.
- 실측 리플레이(14·15·16차 로비 `views.jsonl` + 그때 Stage A 문서)로는 돌려 보지 못했다(저장소에 없음). 다음 GPU·재생 세션에서
  `walk_refinement.json` 으로 확인해야 한다.
- 다듬기가 거절되고 -759 기준으로 통과하면 대각이 틀어진 Stage A 보정이 그대로 채택된다(-759 와 같은 관제 위험: 3대·대각 2대 관측에서
  사람이 쪼개질 수 있음). 기록(`fallback_759`)으로 구분해 다음 세팅에서 다시 다듬어야 한다.
- Stage A 버전도 같은 세션에서 등록된 채 남는다(쓰이지 않는 후보). Tier 0 C17 채택·CALIBRATED 승격까지 종단으로는 시험하지 않았다
  (계보는 발행 시험, 근거 조건은 서버 코드로 확인).

## 8. 넘긴 일

- 실측 리플레이 검증(14·15·16차 로비 views + Stage A 문서)과 수치 재조정.
- 다듬기 전/후 비교를 계약(`site_setup`)·관리자 화면에 싣기(지금은 로그·Pod 파일).
- REUSE_SCENE 의 다듬기와 자동 재굽기, 공유 내부 파라미터(f, k1, k2) 다듬기, Stage A 배경 트랙과 걷기 관절의 단일 목적함수 BA.
