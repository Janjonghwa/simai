# S15P31A101-711 Stage A 비유한 삼각측량을 처리기 오류 대신 검증 실패로

관련 명세: 4.3(세팅 1단계 배경), 5.x(보정 Stage A). 설계 선행: -465(Stage A), -646(품질 게이트), -686(세팅 배경 촬영). 계약 변경 없음.

## 문제(16차 GPU 세션, 2026-11-14)

14차 녹화 재생으로 NEW 세팅을 돌리자 배경 촬영(READY) 뒤 Stage A 에서 `HANDLER_ERROR ValueError: Nonfinite training triangulation;
candidate rejected` 로 끝났다. 경로: `site_setup._run` → `run_stage_a_rotation_ba` → `ProjectedProblem.solve` → scipy `least_squares`(TRF)
→ 후보 걸음 `x_new` 의 잔차 → `points_at_poses` 가 비유한 삼각측량에 `ValueError`. 예외가 scipy 밖으로 올라와 처리기를 죽였고, 화면은
실패 코드표에 없는 `HANDLER_ERROR` 라 일반 문구("서버에서 세팅을 끝내지 못했어요")만 보였다.

## 바꾼 것

1. `joint_ba_v2.NonfiniteTriangulationError(ValueError)`: 비유한 값이 나온 카메라 쌍(`cam_B-cam_C`)을 싣는다.
2. `Problem.solve`: 후보 걸음에서 이 예외가 나면 같은 길이의 NaN 잔차를 돌려준다. scipy TRF 는 비유한 잔차를 받으면 그 걸음을 버리고
   신뢰 반경을 1/4 로 줄여 다시 찾는다(`trf_no_bounds`, scipy 1.17). 받아들인 x 는 늘 유한하다. 버린 횟수·쌍은
   `solver_info.nonfinite_candidates`·`nonfinite_pairs` 에 남는다. 끝내 수렴하지 못하면 기존 게이트(`solver_not_converged`)가
   `VALIDATION_REJECTED` 로 거절하고 메시지 끝에 `nonfinite_candidates=n (pairs …)` 를 붙인다.
3. 시작 자세(초기 복원)부터 비유한이면 걸음을 줄여도 소용없다 → `StageAError("VALIDATION_REJECTED", "… nonfinite_pairs=['cam_B-cam_C']")`.
4. 처리기: `StageAError.nonfinite_pairs` 가 있으면 메시지에 배경 원인 후보(`; 배경 표본이 적은 카메라: cam_D READY 12/12`)를 덧붙인다
   (480자 자름). Stage A 밖에서 올라온 `NonfiniteTriangulationError` 도 `VALIDATION_REJECTED` 로 바꾼다. 그 밖의 `ValueError` 는 진짜
   결함이라 그대로 `HANDLER_ERROR` 다.
5. 배경: `CleanBackgroundBundle.missing_cameras` 가 촬영 상태(`capture.cameras[].state`)가 READY 아닌 카메라도 '아직'으로 본다.
   시간 초과 메시지에 카메라별 `상태 n/필요` 를 싣는다.

화면(`setupFailures.ts`)은 그대로다. `VALIDATION_REJECTED` 는 메시지의 `cam_X-cam_Y` 쌍을 뽑아 "보정한 뒤 오히려 어긋난 카메라 쌍이
있어요 (캠2–캠3) / 카메라와 거치대를 단단히 고정하고 위치를 다시 맞춘 뒤 처음부터 다시 하세요."를 보인다. `worsened_pairs=[…]` 가 있으면
그 안만 보므로, 게이트 거절 끝의 비유한 쌍은 화면 쌍을 바꾸지 않는다(진단용).

## 배경 표본 부족 카메라(고칠 것 2)의 판단

- 기존 규칙(-686)이 이미 막고 있다: Tier 0 는 모든 카메라가 READY(하한 뒤 12장 + 못 채운 비율 2% 미만)일 때만 묶음을 만들어 READY 로
  닫고, 워커 `_ready_bundle` 은 묶음의 카메라마다 `ready` 를 확인한다. 모자라면 240 s 까지 기다리고(사람이 나가면 그 카메라만 이어 찍음)
  끝내 못 채우면 재시도 가능한 `BACKGROUND_INCOMPLETE` + 카메라별 상태다. 화면 배경 촬영 창은 "카메라 n 앞에 사람이 보입니다 · 화면 밖으로
  이동하면 촬영을 이어갑니다"·「다시 촬영」을 보인다.
- 택한 쪽: **시한 안에서 기다리고, 못 채우면 그 카메라로 멈춘다(기존 규칙 유지)**. 근거: 표본이 모자란 채 진행하면 그 카메라 배경이
  비어 Stage A·맵이 뒤에서 더 늦게(약 1~2분 뒤) 실패한다. 240 s 는 -688 시간 예산의 배경 몫이라 늘리면 맵 몫을 깎는다.
- 더한 것: 응답과 묶음이 어긋나도(READY 묶음인데 촬영 상태에 INSUFFICIENT_SAMPLES 가 남음) Stage A 로 가지 않게 `missing_cameras` 에서
  한 번 더 막는다.
- 16차 로그 사본(`tier0.log`)에서는 두 번째 세팅의 촬영이 READY 로 닫혔다(15:11:36). cam_D 의 표본 창은 하한보다 약 190 s 앞에서 시작해
  다른 카메라(약 62 s)보다 훨씬 성겼다 → 하한 뒤 표본이 최소 수 근처였을 수 있다. 그래서 최소 수의 2배 아래 카메라를 비유한 실패의 원인
  후보로 메시지에 싣는다(`BACKGROUND_THIN_FACTOR = 2`: 12장 근처면 마스크 과반 규칙 ⌈n/2⌉ 이 겨우 성립해 배경이 흐리거나 빌 수 있다).
  보고에 적힌 `INSUFFICIENT_SAMPLES 11/23` 은 사본 로그에서 찾지 못했다(화면 표시였을 수 있다).

## 버린 대안

- **비유한이면 곧바로 거절**: 한 후보 걸음이 평행 광선을 만들었을 뿐 시작점·해가 멀쩡해도 세팅 전체를 버린다. TRF 의 걸음 거절이
  수학적으로 맞고, 거절 뒤 결과는 기존 검증 게이트(홀드아웃 악화·깊이·시간·품질 게이트)를 그대로 거친다.
- **새 실패 코드(`NONFINITE_TRIANGULATION`)**: 서버·화면·계약 enum 을 함께 바꿔야 하고 사용자에게 할 일은 `VALIDATION_REJECTED` 와 같다.
- **잔차를 큰 유한 값으로 채움**: 비용이 커질 뿐 TRF 가 걸음을 '받아들이지 않음'을 보장하지 못하고 야코비안이 망가질 수 있다.

## 잔여 위험

- 비유한이 시작점 바로 옆에서 계속 나면 TRF 가 반경만 줄이다 `max_nfev` 에 닿아 `solver_not_converged` 로 거절된다(지금보다 수 초 늦게
  같은 실패 코드).
- 화면은 비유한 쌍을 "보정한 뒤 오히려 어긋난 카메라 쌍"으로 보인다(할 일은 같다). 배경 원인 후보는 실패 메시지에만 있고 화면 문구는 없다.
- 16차와 같은 입력의 실제 재현은 GPU 세션에서 확인해야 한다(사본에 트랙·초기 보정이 없다).

## 코드·시험 위치

- `ai/src/simai_ai/calibration/extrinsics/joint_ba_v2.py`(`NonfiniteTriangulationError`, `Problem.points_at_poses`·`solve`, `run_stage_a_rotation_ba`)
- `ai/services/worker/site_setup.py`(`CleanBackgroundBundle.missing_cameras`, `_background_hint`, `_run` Stage A 예외 처리, 배경 시간 초과 메시지)
- 시험: `ai/tests/calibration/test_joint_ba_v2.py`(후보 걸음 비유한 1회 → 걸음 거절·수렴, 시작 자세 비유한 → `VALIDATION_REJECTED`),
  `ai/tests/services/worker/test_site_setup.py`(Stage A 비유한 → `VALIDATION_REJECTED`+쌍+배경 후보, READY 아닌 배경 카메라 → Stage A 없이
  `BACKGROUND_INCOMPLETE 11/12`)
