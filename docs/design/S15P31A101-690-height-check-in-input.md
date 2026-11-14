# S15P31A101-690 카메라 높이는 처음 입력 창에서 확인하고 그 자리에서 고친다

관련 명세: 4.3(스케일 근거 MEASURED_CAMERA_HEIGHT, 세팅 보정), 8.1·13.4(설치·보정 화면), 12.5(I5 대여·하트비트). 계약
`messages/site_setup` 1.17.0, `messages/job_lease_status` 2.2.0, `messages/job_heartbeat` 2.16.0(묶음 v7.49.0). DB V72.

## 문제(15차 GPU 세션, 2026-11-13)

- 카메라 높이 입력은 축척 근거다. `world_frame.frame.camera_height_scale` 이 카메라별 (입력 높이 ÷ 모델 높이)의 중앙값을 축척으로
  쓰므로 「복원 높이」는 독립 측정이 아니라 입력과 모델 기하의 상대 일관성이다.
- 세팅 2단계 결과 화면에 "카메라 cam_B 높이가 5.5 cm 안 맞음 — 거치대 확인"(서버 표시 기준 5 cm 고정)이 떠서 사용자가 높이를 바꿔
  다시 넣었다. 경고는 막지 않았지만 재입력을 요구하는 것처럼 보였다. 실측(11-05 A·B 2.44 m)이 맞았고 모델 쪽 잡음이었다.
- 따로 품질 게이트(`joint_ba_v2.evaluate_quality_gate`, 높이 차 10 cm 실패)가 남아 있어, 사람이 맞다고 해도 뒤에서 같은 높이로 막을 수
  있었다.
- 사용자 결정: 높이가 틀린 것도 처음 높이 입력 창 안에서 확인하고 그 자리에서 고친다. 앞에서 확인했으면 뒤 단계에서 같은 높이로 다시
  막지 않는다. 사람이 움직일 일은 모달, 화면은 필요한 정보만.

## 바꾼 흐름

1. 워커(`SiteSetupHandler._review_heights`)가 MapAnything 초기 복원·바닥(World) 정의를 마친 직후(INITIAL_POSE_AND_WORLD 39%) 카메라별
   `relative_diff = (복원 − 입력) ÷ 입력` 을 계산한다(`joint_ba_v2.compute_height_review`). 모두 기준 안이면 PASSED 로 기다리지 않는다.
2. 기준을 넘는 카메라가 있으면 `height_review.status = AWAITING_CONFIRMATION`(+ `wait_s`) 스냅샷을 보내고 기다린다. 이 스냅샷에는
   `height_checks` 를 싣지 않는다(아래 「세션 높이」).
3. EC2 는 이 값을 `setup_session.height_review` 에 두고 세션 조회에 `paused_reason: HEIGHT_CHECK` 를 싣는다. 화면은 1단계와 같은 높이
   입력 칸(`CameraHeightPanel`)을 창(`HeightCheckModal`)으로 띄워 카메라별 「맞음」·「다른 카메라보다 약 n cm 낮게(높게) 보임」을 보인다.
   Esc·바깥 클릭으로 닫히지 않는다.
4. 관리자가 값을 고치면 「고친 값으로 진행」, 그대로면 「실측이 맞음 · 이대로 진행」 단추 하나다. 둘 다 `POST /api/setup-sessions/{id}/resume`
   (`camera_heights_m`)이고, EC2 가 지금 높이와 비교해 CORRECTED·CONFIRMED 를 정해 `height_decision` 에 기록하고(누가·언제·이전 높이)
   보낸 높이를 현장 기본값으로 남긴다(세션 생성과 같은 `site.default_camera_heights_m` 경로).
5. 결정은 다음 하트비트 응답(`job_lease_status` 2.2.0 `height_decision`)으로 워커에 간다. 고친 높이면 워커는 초기 복원의 같은 점구름·
   카메라 포즈로 `build_world_from_pointcloud_and_poses` 만 다시 돌려(바닥·벽·모서리·축척·바닥 기울기 보정, CPU 수 초) CORRECTED 로
   이어 간다. MapAnything 은 다시 돌리지 않는다(`InitialWorldReconstruction.rebuild`). 같은 높이면 CONFIRMED.
6. PASSED·CONFIRMED·CORRECTED 면 품질 게이트가 높이 차로 카메라를 FAIL 시키지 않는다(`heights_settled`, 차는 기록). 복원 높이를 모르는
   경우(HEIGHT_UNKNOWN)와 쌍별 홀드아웃·바닥 검출·다중 카메라 기하 실패는 그대로 실패다.
7. 결과 화면의 "거치대 확인" 문구와 복원 높이 표는 「자세히 · 카메라 높이」(`<details>`, 닫힘)로 접었다. 확인한 높이면 「카메라 다시
   맞추기」 단추와 높이 불일치 안내를 띄우지 않는다(`showHeightNotice`).

### 세션 높이(워커가 쓰는 높이가 정본)

서버는 `height_checks.input_height_m` 이 세션 `camera_heights_m` 과 다르면 스냅샷을 400 으로 거부하고(워커는 400 을 받으면 작업을 버린다),
워커 작업 입력은 대기열에 넣을 때 고정이다. 그래서 `/resume` 은 세션 높이를 바꾸지 않고 결정만 기록한다. 워커가 결정을 받아 그 높이를 실은
스냅샷(`height_review.camera_heights_m`)을 보낼 때 EC2 가 세션 높이를 그 값으로 바꾸고 같은 스냅샷의 `height_checks` 를 그 값과 맞춘다.
이렇게 하면 결정이 워커 시간 초과와 엇갈려도(워커는 입력 높이로 이미 진행) 세션 높이와 검산이 어긋나지 않는다.

## 워커가 결정을 기다리는 방식

- 통로: 이미 있는 하트비트 응답을 쓴다(-597 `live_work_active` 와 같은 길). 응답에 `height_decision` 이 오면 러너가
  `JobContext.note_height_decision` 으로 넣고(처음 것만), 처리기는 `height_review_poll_s`(1 s)마다 본다. 새 REST 폴링·새 연결을 만들지 않는다.
- 결정은 세션에 한 번만 받고, 결정이 있는 동안 그 세션 작업의 모든 ACTIVE 응답에 싣는다. 재대여된 워커는 확인 지점에 오기 전에 이미
  받으므로 다시 묻지 않고 그 높이를 쓴다.
- 기다리는 동안 EC2 는 site_setup 하트비트 주기를 5 s → 2 s 로 줄여 「진행」 뒤 워커가 이어 가기까지 최대 약 2 s 다.
- GPU: MapAnything 은 자식 프로세스로 돌고 끝났으므로 기다리는 동안 GPU 메모리를 잡지 않는다. 다만 워커는 작업 하나만 돌리므로 그동안
  다른 I5 작업을 받지 않는다(세팅은 LIVE 관제 전 절차라 Tier 0 와 겹치지 않는다).

## 정한 수치와 근거

| 값 | 위치 | 근거 |
|---|---|---|
| 상대 불일치 기준 3% | `joint_ba_v2.HEIGHT_REVIEW_MAX_RELATIVE_DIFF` | 축척 근거 기각 기준(`camera_height_max_spread` 0.03, -277·-321)과 같다. 2.4 m 에서 약 7 cm. 14차 정상 세팅 높이 차 2.5~4.3 cm(1~1.8%), 15차 cam_B 5.5 cm(2.2~2.4%, 실측이 맞았던 잡음)는 기준 안이라 묻지 않는다. 거치대 높이를 바꾸고 값을 안 고치거나(10 cm 이상) 숫자를 잘못 치면(2.48 → 2.84) 3%를 넘는다. |
| 대기 상한 180 s | `SiteSetupHandler(height_review_wait_s)` | 줄자로 한 대를 다시 재고 입력하는 데 1~2분. 넘으면 입력 높이 그대로 진행(TIMED_OUT, 경고 로그·기록). 사람이 답하지 않았으므로 품질 게이트 높이 상한(10 cm)은 그대로 본다. |
| 뒤 단계 몫 360 s | `height_review_reserve_s` | 세팅 작업 실행 상한 900 s(`SetupService.SETUP_TIMEOUT_MS`) 안에서 Stage A·보정 등록·미리보기·카메라 확인(사람 걷기)·굽기가 남는다. 대기 = min(180, 남은 시간 − 360). 남는 시간이 없으면 묻지 않고 TIMED_OUT. 배경 촬영(최대 240 s)과 MapAnything 뒤에도 보통 180 s 를 다 쓸 수 있다. |
| 결정 확인 간격 1 s | `height_review_poll_s` | 결정은 하트비트(2 s)로만 들어오므로 그보다 짧으면 충분하다. |
| 기다리는 동안 하트비트 2 s | `JobMessages.SITE_SETUP_HEIGHT_WAIT_HEARTBEAT_INTERVAL_MS` | 「진행」 뒤 지연을 체감 없게. 기다리는 동안(최대 3분)만이라 요청 수 증가는 작다. 평소는 5 s(-597). |
| 같은 높이 판단 0.1 mm | `SetupHeightReview.SAME_HEIGHT_M`, 화면 `heightsChanged` | 입력은 cm 단위라 그보다 작은 차는 같은 값. 서버와 화면이 같은 기준으로 CONFIRMED·CORRECTED 를 가른다. |
| 화면 「약 n cm」 | `heightCheckNotes` | `difference_cm` 반올림(최소 1). LOW = 복원이 입력보다 낮다 = 「다른 카메라보다 낮게 보임」. |

## 계약·마이그레이션

- `site_setup` 1.17.0: progress 선택 `height_review`(`status`, `max_relative_diff`, `wait_s`(AWAITING 필수), `camera_heights_m`,
  `cameras{restored_height_m, difference_cm, relative_diff, verdict}`). `job_heartbeat` 2.16.0.
- `job_lease_status` 2.2.0: ACTIVE 선택 `height_decision{decision, camera_heights_m}`(ACTIVE 가 아니면 금지).
- `quality_gate` 모양은 그대로다(확인한 높이 카메라는 PASS·reason null, 차는 `height_diff_cm` 에 남는다). 확인 여부는 `height_review` 에 있다.
- DB V72: `setup_session.height_review jsonb`(같은 대여 안에서 값 없는 스냅샷은 이전 값을 두고 재대여는 지움, `background_capture` 와 같은
  규칙), `setup_session.height_decision jsonb`(덮어쓰지 않음, 기다리는 동안 한 번).
- 배포 순서: EC2·화면 먼저, 워커 이미지 나중. 옛 EC2 는 `height_review` 가 든 하트비트를 계약 검사로 거부한다. 옛 워커 + 새 EC2 는 지금처럼
  묻지 않는다. 새 워커 + 옛 화면이면 창이 없어 대기 상한 뒤 입력 높이로 진행한다.

## 버린 대안

- **결과 화면 경고 문구만 고치기**: 품질 게이트가 같은 높이로 다시 막는 문제와 「앞에서 확인」 흐름이 남는다.
- **고친 높이로 세팅 다시 시작(retry)**: 배경 촬영·MapAnything 을 다시 돌려 수 분이 더 든다. 축척·바닥 맞춤은 같은 점구름으로 다시 하면 된다.
- **고친 높이를 축척 배수로만 곱하기(문서 직접 수정)**: 3대 이상이면 높이가 바닥 기울기 보정(-542)에도 들어가 회전이 바뀐다.
  `build_world_from_pointcloud_and_poses` 를 같은 입력으로 다시 돌리는 편이 정확하고 CPU 수 초다.
- **`/resume` 이 세션 높이·작업 입력을 바로 바꾸기**: 워커가 시간 초과로 이미 입력 높이로 진행했으면 그 뒤 `height_checks` 가 세션과 달라
  400 → 작업이 버려진다. 워커가 쓰는 높이를 정본으로 두었다.
- **새 REST(워커가 세션 결정을 따로 조회)**: 워커 → EC2 새 경로·인증·계약이 늘어난다. 하트비트 응답에 이미 site_setup 신호(-597)가 있다.
- **기다리는 동안 특징점 트랙 추출(Stage A 앞부분)을 미리 돌리기**: 트랙은 초기 보정 문서(축척 포함)로 뽑으므로 고친 높이면 버려야 하고,
  기다림 대부분은 사람 몫이라 얻는 시간이 작다. 단순하게 두었다.
- **TIMED_OUT 이면 실패로 끝내기**: 시연·현장에서 사람이 자리를 비우면 세팅이 통째로 실패한다. 진행하되 확인하지 않았으므로 게이트 높이
  상한은 그대로 둔다.
- **확인 창에 「모델이 본 높이」를 채워 넣기**: 복원 높이는 입력 중앙값 축척이라 독립 측정이 아니다. 줄자 실측을 대신하지 않는다.

## 잔여 위험

- 4대를 같은 배수로 틀리면(예: 모두 10% 높게) 축척이 함께 바뀌어 모두 OK 다. 절대 높이는 라바콘·안내판 교차 확인(-462)이 맡는다.
- 2대 중 1대만 틀린 경우 중앙값이 둘의 평균이라 두 카메라가 반씩 어긋나 보인다(4대 운영에서는 한 대 오입력이 정확히 그 카메라로 보인다).
- 기준 3%는 14·15차 몇 번의 관측에 기댄다. 모델 잡음이 3%를 넘는 현장이면 맞는 높이에도 창이 뜬다(「실측이 맞음」 한 번으로 지나간다).
- 결정이 워커 시간 초과와 엇갈리면 결정은 기록되지만 이 대여에는 쓰이지 않는다(화면은 TIMED_OUT 을 본다). 재대여되면 그 결정을 쓴다.
- 재대여된 워커가 첫 하트비트를 모두 놓쳐 결정을 모른 채 확인 지점에 오면 입력 높이로 다시 묻고, 다음 하트비트로 기존 결정을 받아 이어 간다.
- 화면의 「약 n분 안에 답하지 않으면」은 대략값이다(서버 updated_at 이 하트비트마다 바뀌어 남은 초를 세지 않는다).

## 넘긴 일

- 16차 GPU 세션: 한 카메라 높이를 일부러 10 cm 틀리게 넣어 창이 뜨는지, 고친 값으로 다시 맞추기가 수 초 안에 끝나는지, 「실측이 맞음」
  뒤 품질 게이트가 높이로 막지 않는지, 하트비트 2 s 동안 EC2 부하.
- 명세 4.3(높이 빠른 확인·확인한 높이는 게이트에서 다시 거절하지 않음)·8.1 화면 흐름을 명세 정리 MR 에서 반영.
- 옛 `paused_reason` QUALITY_GATE 멈춤 안내(`PausedNotice`, 서버가 낸 적 없는 요청 계약)는 화면 정리(-691)에서 지울지 정한다. 이 티켓은
  HEIGHT_CHECK 일 때 그 안내를 숨기고 목업 멈춤을 높이 확인으로 바꿨다.

## 코드·시험 위치

- 판정: `ai/src/simai_ai/calibration/extrinsics/joint_ba_v2.py`(`compute_height_review`, `evaluate_quality_gate(heights_settled)`).
- 워커: `ai/services/worker/site_setup.py`(`_review_heights`·`_wait_height_decision`, `InitialWorldReconstruction.rebuild`),
  `handlers.py`(`JobContext.note_height_decision`), `client.py`(`LeaseStatus.height_decision`), `runner.py`(`_note_height_decision`).
- 서버: `SetupHeightReview`, `SetupService`(`resume`, 스냅샷 반영·조회), `SetupController`(`POST /{id}/resume`), `JobLeaseService`
  (`heightSignal`), `LeaseStatus`·`JobMessages`, `V72__setup_session_height_review.sql`, `ErrorCode.Setup.SETUP_RESUME_NOT_ALLOWED`.
- 화면: `front/apps/admin/src/features/setup/HeightCheckModal.tsx`·`heightCheck.ts`, `SetupPanels.tsx`(`fieldNotes`), `SetupPage.tsx`,
  `SetupCalibrateStep.tsx`(「자세히」), `setup.ts`(`showHeightNotice`·`heightCheckPending`), 목업 `mocks/setup.ts`.
- 시험: `ai/tests/services/worker/test_site_setup.py`(한 대 오입력·고친 값 다시 맞추기·실측 확인 뒤 게이트·시간 초과·예산 상한·취소·같은
  배수 오입력·재대여 결정), `test_runner.py`·`test_client.py`(하트비트 결정), `SetupSessionIntegrationTest`(멈춤·결정·하트비트·세션 높이),
  `heightCheck.test.ts`·`SetupPage.test.tsx`(창·고친 값·실측이 맞음·자세히).
