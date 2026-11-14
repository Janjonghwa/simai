# S15P31A101-687 카메라 확인은 맵 굽기 앞에서, 실패하면 확인만 다시

관련 명세: 4.3(현장 세팅 파이프라인, 세팅 「카메라 확인」), 4.4(틀어짐 감시 기준선), 3.2(Tier 0 자원 우선), 12.5(I5 대여·하트비트),
12.6(세팅 REST), 13.4(세팅 화면). 앞 문서 `S15P31A101-646-setup-camera-check.md`(카메라 확인·기준선), `-686`(새 배경), `-690`(높이 확인),
`-689`(재사용), `-694`(기준값). 계약 묶음 v7.52.0: `messages/site_setup` 1.19.0, `job_lease_status` 2.3.0, `job_heartbeat` 2.18.0,
`job_callback` 2.25.0. DB V74.

## 문제(15차 GPU 세션, 2026-11-13)

- 카메라 확인(-646)은 보정 등록 뒤 별도 스레드로 시작해 정밀 맵 굽기와 함께 돌고, 굽기가 끝날 때(`_finish_camera_check`, 실행 상한 20 s
  전) 판정을 닫았다. 확인 채널을 열지 못해도 맵 생성은 계속했다.
- 세팅 1: 아무도 걷지 않아(views 3181줄 모두 빈 시각, -694) 880 s 뒤 INCOMPLETE → 세팅 완료 409. 화면은 작은 카드 하나였고 3D 미리보기가
  카메라 영상을 덮었으며 마감 표시가 없었다.
- 세팅 2: 31 s 만에 FAILED, 다시 하는 길은 세팅 전체(맵 다시) 재시작뿐이었다.

사용자 원칙: 실패할 수 있는 확인은 맵 굽기 앞. 앞에서 통과했으면 뒤에서 막지 않는다. 비차단화 금지(통과 없이는 기준선이 없어 관제 감시가
DRIFTED 를 낸다). 사람이 움직일 일은 큰 창: 「걷기 시작」을 눌러야 시작, 4대 영상·카메라별 사람 보임·남은 시간(30초 목표)·진행, Esc·바깥
클릭으로 안 닫힘, 통과하면 자동 닫힘, 실패하면 확인만 다시(30초). 표본 부족과 기하 실패를 나눠 안내.

## 바꾼 순서(워커 `SiteSetupHandler._run`)

| 전 | 후 |
|---|---|
| (a) 배경 → (b~d) 초기 복원·높이 확인 → (e) Stage A·품질 게이트 → (f) 보정 등록 + 카메라 확인 스레드 시작 → (g) 미리보기 → (h) 굽기 → 결과 전 확인 마감(20 s) | (a) 새 배경(-686) → (b~d) 초기 복원·높이 확인(-690) → (e) Stage A·품질 게이트 → (f) 보정 등록(65%) → **(f') 카메라 확인(걷기 시작 대기·시도·확인만 다시)** → 통과해야 (g) 미리보기 → (h) 굽기(NEW) / READY_FOR_ZONES → 결과(REUSE) |

- 통과하지 못하면 (g)·(h) 를 한 번도 부르지 않는다: 대기 마감까지 통과 못 함 `CAMERA_CHECK_NOT_PASSED`, 확인을 준비·열지 못하거나 Tier 0
  view 가 한 줄도 안 옴 `CAMERA_CHECK_UNAVAILABLE`(둘 다 retryable=false). 시험 `test_camera_check_without_walk_start_fails_with_zero_bakes`,
  `test_camera_check_channel_failure_fails_with_zero_bakes`.
- 단계는 JOINT_BA_AND_VERIFY 65% 그대로 두고 `camera_check` 만 바꿔 싣는다(새 단계 enum 을 만들지 않음: 서버 단계 순서·진행률 규칙을 그대로 쓴다).
- 보정은 (f) 에서 이미 등록했다(카메라 확인과 서버 관문이 그 보정 버전을 잰다). 실패한 세팅의 보정은 등록만 되고 쓰이지 않는다(Tier 0 는
  세팅 완료 C17 때만 새 보정을 채택한다).
- 재사용(REUSE)도 같은 위치에서 확인한다(-646 은 결과 전에 기다렸다).

## 시도(attempt)와 대기 방식

```
워커                                         EC2                                화면
보정 등록(65%) ──▶ camera_check AWAITING_START + user_wait ──▶ camera_check_phase AWAITING_START, paused_reason CAMERA_CHECK ──▶ 걷기 창
                                                               ◀── POST /camera-check/start {attempt:1}  (「걷기 시작」)
하트비트 응답 camera_check_start{attempt:1} ◀── camera_check_request(attempt, lease_id) 저장
시도 1 열기: attempt_id = 대여8자리-1, request.json 에 attempt_id ──▶ Tier 0 가 sessions/<세션>/<시도>/views.jsonl 에 적음
poll … MEASURING(attempt) ──▶ WALKING(카메라별 사람 보임·n/30·진행)
INCOMPLETE/FAILED + user_wait ──▶ RETRY_WAIT ──▶ 안내 + 「확인만 다시(30초)」
                                                               ◀── POST /camera-check/start {attempt:2}
camera_check_start{attempt:2} → 같은 보정으로 시도 2 … PASSED ──▶ PASSED(창 자동 닫힘) → 미리보기·굽기
```

- **통로**: -690 높이 확인과 같은 하트비트 응답 경로(`job_lease_status` 의 결정 필드). 새 연결·새 폴링을 만들지 않는다. 카메라 확인 중
  (`user_wait` 이 있거나 AWAITING_START·WAITING·MEASURING)에는 EC2 가 하트비트 주기를 5 s → 2 s 로 줄인다.
- **늦은 이전 응답이 지금 시도를 덮지 못하게(세 겹)**: ① 워커 `JobContext.note_camera_check_start` 는 지금까지 받은 가장 큰 번호만 두고,
  처리기는 지금 시도보다 큰 번호일 때만 새 시도를 연다. ② Tier 0 탭은 요청의 `attempt_id` 별 폴더에 적어, 대기열에 남아 늦게 쓰인 이전 시도
  view 가 새 시도 파일에 섞이지 않는다(줄 수 상한도 시도마다). ③ EC2 는 같은 대여 안에서 이미 받은 것보다 작은 시도 번호의 `camera_check`
  를 저장하지 않고(늦은 하트비트), 화면이 보낸 `attempt` 가 다음 번호가 아니면 409(늦게 온 이전 화면의 누름), 이미 보낸 요청을 워커가 아직
  열지 않았으면 같은 요청을 돌려준다(두 번 누름).
- **재대여**: 요청에 `lease_id` 를 함께 두고 그 대여의 하트비트에만 싣는다. 재대여 첫 스냅샷은 앞선 대여의 `camera_check` 를 지운다
  (`background_capture`·`height_review` 와 같은 규칙). 재대여한 워커는 처음부터 다시 「걷기 시작」을 기다린다(사용자가 누르지 않았는데
  걷기를 시작하지 않는다).
- **기록**: 시도 요약 `attempt{no, id, started_at_ms, expires_at_ms, calibration_version}`. 끝난 시도는 `setup_session.camera_check_attempts`
  에 덧붙인다(덮어쓰지 않음, id·번호·대여·상태·보정·시각·문제 사유).
- **"camera epoch"**: 지시의 camera epoch 는 이 구현에서 "그 시도가 받은 Tier 0 view 입력의 묶음"(시도 ID 별 view 폴더 + 대여 ID)으로
  구현했다. I0 카메라별 `stream_epoch` 는 view 에 실려 있지 않아(보간된 view 는 epoch 를 모른다) 시도에 저장하지 않았다(잔여 위험).

### 표본 부족과 기하 실패 구분

- 평가기 `CameraCheckEvaluator.incomplete()` 가 이제 표본 부족 사유만 싣는다: 사람이 하나도 안 보였으면 `NO_PERSON`, 관측이 모자란 카메라마다
  `FEW_SAMPLES`(카메라), 카메라는 채웠는데 필수 쌍의 동시 관측이 모자라면 그 쌍 `FEW_SAMPLES`, 그래도 없으면 `NOT_FINISHED`(걸은 시간 부족).
- 기하 실패(FAILED): `PASS_RATE_LOW`·`RESIDUAL_HIGH`·`REPROJECTION_HIGH`·`NO_PASSING_PAIR`(기존). `NOT_OBSERVED`·`NOT_CONFIRMED`·`FEW_PAIRS`
  (최대 시간 뒤 판정)는 화면이 표본 부족으로 안내한다.
- 화면: 표본 부족 → "카메라 4에서 사람이 충분히 보이지 않았습니다 / 표시된 구간을 지나며 30초만 다시 걸어 주세요"(그 카메라 타일에 테두리),
  기하 실패 → "카메라 2, 카메라 3 위치가 보정과 맞지 않습니다 / 카메라가 흔들리지 않았는지 확인한 뒤 확인만 다시 하세요. 계속 실패하면 현재
  시도를 중단하고 다시 보정해 주세요".
- 카메라별 「사람 보임」: 평가기가 카메라마다 마지막 사람 시각을 두고, 마지막 view 시각 기준 1.5 s 안이면 `person_visible`(표시용).

## 시간 배분(실행 상한 900 s 안, `SetupService.SETUP_TIMEOUT_MS` 그대로)

> S15P31A101-688 이 실행 상한을 사람 대기·기계 몫의 합 1,800 s 로 바꾸고 NEW 맵 몫을 420 → 600 s, 높이 확인 뒤 단계 몫을 540 → 1,260 s 로
> 정리했다(`S15P31A101-688-new-complete-time-budget.md` 2·4절). 아래 표는 -687 당시 값이다.

| 값 | 위치 | 근거 |
|---|---|---|
| 카메라 확인 단계 상한 600 s | `camera_check_wait_s` | 「걷기 시작」을 누르기까지 + 시도 여러 번. 사람이 자리를 비운 채 실행 상한을 다 쓰지 않게 상한을 둔다(다시 하면 배경부터 1~2분이라 이보다 길게 기다릴 이유가 적다). |
| NEW 맵 몫 420 s | `camera_check_reserve_s` | 단계 대기 = min(600, 남은 시간 − 420). 미리보기 약 18 s(15차 07:48:20 → 07:48:38) + 굽기 약 5분(-646 추정, 15차 세팅 2 의 굽기는 208 s 뒤 취소될 때까지 돌았다) + 등록·제출 여유 약 100 s. |
| REUSE 결과 몫 30 s | `camera_check_reuse_reserve_s` | 굽기가 없고 READY_FOR_ZONES·결과 제출만 남는다. |
| 시도 상한 150 s | `camera_check_attempt_s` | 평가기 판정 상한(사람이 보인 시간 120 s, -694 기준 그대로) + 들어서는 시간 30 s. 이보다 짧으면 겹치는 쌍 근거가 끝내 모자란 현장(120 s 뒤 판정으로 통과하던 경우)이 영원히 INCOMPLETE 가 된다. 보통은 30~45 s 에 끝난다(15차 세팅 2: 45 s 만에 판정). |
| 단계 최소 45 s | `camera_check_min_s` | 걷기 30 s + 들어서는 시간. 이만큼도 못 쓰면 확인을 열지 않고 실패하고, 시도가 끝난 뒤 남은 대기가 이보다 짧으면 다시 확인을 받지 않는다. |
| 채널 확인 15 s | `camera_check_channel_s` | Tier 0 탭은 요청 파일을 1 s 마다 보고, 요청이 있는 동안 빈 시각도 최대 5 Hz 로 적는다(-694). 15 s 동안 한 줄도 없으면 Tier 0 가 view 를 넘기지 않는 것이다(맵 굽기 0회). |
| 높이 확인 뒤 단계 몫 360 → 540 s | `height_review_reserve_s` | Stage A·보정 등록 약 30 s + 카메라 확인 최소 몫 90 s + 맵 몫 420 s. 높이 확인 대기가 카메라 확인 시간을 다 먹지 않게 늘렸다. |
| 카메라 확인 중 하트비트 2 s | `JobMessages.SITE_SETUP_CAMERA_CHECK_HEARTBEAT_INTERVAL_MS` | 「걷기 시작」 뒤 워커가 시도를 열기까지 최대 2 s, 걷기 창 진행도 2~3 s 마다(워커 보고 간격 3 s) 바뀐다. |

보통의 타임라인(15차 실측, 높이 확인 없음): 배경 ~20 s + 초기 복원·Stage A·등록 ~60 s → 카메라 확인 대기 min(600, 900 − 80 − 420) ≈ 400 s →
걷기 30~45 s(실패하면 한두 번 더) → 미리보기·굽기. 높이 확인을 다 기다리면(180 s) 카메라 확인 대기는 약 230 s 다.

## 서버(EC2)

- `setup_session.camera_check_request`(V74): 마지막 요청 `{attempt, lease_id, requested_by, requested_at_ms}`. `camera_check_attempts`(V74):
  끝난 시도 기록 배열(최대 64).
- `POST /api/setup-sessions/{id}/camera-check/start`(ADMIN, 선택 `{"attempt": n}`): 워커가 관리자 입력을 기다리는(`user_wait` +
  AWAITING_START·FAILED·INCOMPLETE) 열린 세션만 받는다. 409 `SETUP_CAMERA_CHECK_START_NOT_ALLOWED`. 「걷기 시작」과 「확인만 다시」가 같은
  엔드포인트다(둘 다 "다음 시도 열기").
- 세션 조회: `camera_check_phase`(AWAITING_START·STARTING·WALKING·RETRY_WAIT·PASSED·FAILED), `camera_check_request`, `camera_check_attempts`,
  `paused_reason` CAMERA_CHECK(걷기 시작·확인만 다시 대기, 높이 확인 대기가 먼저).
- 하트비트 응답 `camera_check_start{attempt}`: 이 대여에 한 요청만.
- 완료(C17)·구역 관문은 그대로 "PASSED 이고 그 확인의 보정이 세션 보정과 같다"(`SetupCameraCheck.passed`). 이제 통과가 맵 전에 정해지므로
  NEW 의 READY_FOR_ZONES·COMPLETED 는 늘 통과 뒤에 오고, 뒤에서 처음 막히는 일이 없다(옛 워커 제외).

## 화면

- `CameraCheckModal`(새): 보정이 끝나면 맵 전에 크게 띄운다. 4대 영상(아래 칸 영상은 내림, 영상 연결은 한 번에 하나), 카메라별 「사람 보임」·
  「사람 보인 횟수 n/30」·진행 막대, 남은 시간(브라우저 시계 30초, 0 이 돼도 결과까지 열림), 전체 진행. Esc·바깥 클릭으로 안 닫힘, PASSED
  면 자동 닫힘.
- 단추: AWAITING_START 「걷기 시작」(주), STARTING 「시작하는 중」, RETRY_WAIT 「확인만 다시(30초)」(주), 늘 「현재 시도 중단」(보조, 확인 뒤
  세션 취소 → 카메라 연결 관리). 대기 마감은 「약 n분 안에 시작하지 않으면 맵을 만들지 않고 세팅이 멈춥니다」(대략값).
- 기존 작은 「카메라 확인」 카드(`CameraCheckCard`)와 「다시 보정」 단추를 지웠다. 쌍별 검증 표는 남는다. 카메라 확인 대기(paused_reason
  CAMERA_CHECK)는 보정 멈춤 안내·높이 이어 가기로 보지 않는다(`pausedForHeights`).
- 3D 미리보기는 이제 카메라 확인 통과 뒤에 생기므로 걷기 동안 카메라 영상을 덮지 않는다(걷기 창 안 4대 영상).
- 개발 서버 목업(`cameraCheckWalk`): 보정 뒤 9 s 에 멈추고, 「걷기 시작」 1 s 뒤 걷는 중, 12 s 뒤 통과(옵션으로 처음 n번 표본 부족), 멈춘
  만큼 시작 시각을 미뤄 미리보기·굽기로 이어 간다.

## 계약·마이그레이션·배포

- `site_setup` 1.19.0 `$defs/camera_check`: 상태 `AWAITING_START`, 선택 `attempt`·`user_wait`·`cameras[].person_visible`. 규칙: AWAITING_START
  는 `user_wait` 필수·`attempt` 금지, WAITING·MEASURING·PASSED 는 `user_wait` 금지. 예시 `job_heartbeat.site_setup_camera_check_awaiting`·
  `…_retry`, 반례 `invalid/job_heartbeat.site_setup_camera_check_awaiting_without_wait`·`…_measuring_with_wait`.
- `job_lease_status` 2.3.0: ACTIVE 선택 `camera_check_start{attempt}`. 예시 `job_lease_status.active_camera_check_start`, 반례
  `invalid/job_lease_status.accepted_with_camera_check_start`.
- Tier 0 ↔ 워커 `request.json` 의 `attempt_id`·시도별 view 폴더는 Pod 안 파일 규약(계약 밖).
- DB V74. 배포 순서: **EC2·화면 먼저, 워커·Tier 0 이미지 나중**. 옛 EC2 는 새 상태·필드가 든 하트비트를 계약 검사로 거부한다. 새 EC2 + 옛
  워커는 지금처럼 굽기와 함께 확인한다(화면은 `camera_check_phase` WALKING 으로 걷기 창을 띄우지만 「걷기 시작」은 없다, 서버 관문은 그대로).
  새 워커 + 옛 화면이면 걷기 창이 없어 대기 마감 뒤 맵 없이 실패한다(그래서 화면을 먼저 배포한다).

## 버린 대안

- **카메라 확인 비차단화·85% 완료·기준만 올리기**: 사용자 금지(관제 감시 기준선 부재 → DRIFTED·트랙 분열).
- **카메라 확인을 별도 I5 작업으로**: 워커가 하나라 대기열 순서·작업 전이가 늘고, 걷기 대기 중 대여·실행 상한을 다시 짜야 한다. 같은 대여
  안에서 기다리는 편이 -690 과 같은 길이다.
- **새 단계 enum(CAMERA_CHECK)**: 서버 단계 순서·진행률·화면 단계표를 모두 바꿔야 한다. 65% JOINT_BA_AND_VERIFY 에 `camera_check` 로 충분하다.
- **확인만 다시를 서버 retry(새 세션)로**: 배경·MapAnything·Stage A 를 다시 돌리고 새 보정이 생긴다. 사용자 원칙은 "보정은 그대로, 걷기만".
- **보정을 통과 뒤에 등록**: 확인과 서버 관문이 잴 보정 버전이 없고, 실패해도 Tier 0 는 완료 전에는 새 보정을 쓰지 않아 얻는 것이 없다.
- **실행 상한을 이 티켓에서 늘리기**: 시간 제한 전반 정리는 -688 범위다. 여기서는 900 s 안에서 몫을 나눴다.
- **시도 상한을 30~60 s 로 짧게**: -694 판정 상한(120 s)과 어긋나 근거가 끝내 모자란 겹치는 쌍이 있는 현장에서 영원히 통과하지 못한다.
- **I0 stream_epoch 를 시도에 싣기**: 보간 view 에 epoch 가 없어 Tier 0 판정 경로를 고쳐야 한다(이번 범위 밖).

## 잔여 위험

- 맵 몫 420 s 는 15차 한 번의 관측과 -646 추정에 기댄다. 굽기가 더 오래 걸리는 현장이면 통과 뒤 굽기가 실행 상한에 걸려 TIMEOUT 이 된다.
  -688 이 시간 제한을 사람 대기·보정·굽기로 나눠 늘리면 이 값을 다시 정한다.
- 높이 확인(180 s)과 카메라 확인을 모두 오래 기다리면 카메라 확인 대기가 약 230 s 로 준다(그래도 시도 한두 번은 된다).
- 카메라 확인 중에 카메라가 끊겼다 다시 붙어도(I0 stream_epoch 변화) 시도는 그대로 이어진다. 끊긴 동안은 그 카메라 관측이 모자라 표본 부족
  으로 안내되고, 통과 뒤 실제 이동·끊김은 관제 틀어짐 감시(-540·-646)가 맡는다.
- 화면의 남은 시간·대기 마감은 브라우저 시계와 스냅샷 값으로 센 대략값이다(판정은 워커가 사람이 보인 시간으로 한다).
- 「걷기 시작」 뒤 워커가 시도를 열기까지 하트비트 주기(2 s) + 처리기 확인 간격(1 s)만큼 늦다. 그 사이 걷기 시작한 사람은 몇 초 덜 잡힌다.
- 확인을 통과하지 못해 끝난 세팅의 보정 문서는 등록된 채 남는다(덮어쓰지 않는 규칙, 쓰이지 않음).

## 넘긴 일

- 16차 GPU 세션: 보정 뒤 걷기 창 → 「걷기 시작」 → 30~45 s 통과 → 미리보기·굽기가 실행 상한 안에 끝나는지(굽기 실측으로 맵 몫 420 s 확인),
  한 카메라 앞을 피해 걸어 표본 부족 안내·확인만 다시가 같은 보정으로 통과하는지, Tier 0 를 멈춘 채 걷기 시작하면 15 s 뒤
  CAMERA_CHECK_UNAVAILABLE 로 맵 없이 끝나는지, 걷는 동안 Tier 0 지연.
- -688: NEW 완료 조건·시간 제한 전반(사람 대기 시간을 실행 상한에서 빼기 등). -691: 세팅 화면 전반 정리(쌍별 표를 「자세히」로 접을지 등).
- 명세 4.3(카메라 확인 단계 위치·시도·확인만 다시)·13.4(걷기 창) 반영은 명세 정리 MR 에서.
- `S15P31A101-646-setup-camera-check.md` 의 "넘긴 일: 카메라 확인 다시 확인"은 이 티켓이 했다. `-690` 문서의 뒤 단계 몫 360 s 는 540 s 가 됐다.

## 코드·시험 위치

- 워커: `ai/services/worker/site_setup.py`(`_run_camera_check`·`_close_camera_check`, 상수 `CAMERA_CHECK_NOT_PASSED`·`CAMERA_CHECK_UNAVAILABLE`),
  `ai/services/worker/camera_check.py`(`idle`, `start(attempt_id)`, `lines_read`), `handlers.py`(`note_camera_check_start`), `client.py`
  (`LeaseStatus.camera_check_start`), `runner.py`(`_note_camera_check_start`).
- 평가기: `ai/src/simai_ai/calibration/camera_check.py`(`incomplete` 표본 부족 사유, `person_visible`, `SAMPLE_SHORTAGE_REASONS`).
- Tier 0: `ai/services/tier0/camera_check_tap.py`(시도별 view 폴더).
- 서버: `SetupCameraCheck`(`Phase`·`phase`·`awaitingUser`·`attemptRecord`), `SetupService`(`startCameraCheck`, 스냅샷 늦은 시도 막기·재대여 지움·
  시도 기록, 조회 필드), `SetupController`(`POST /{id}/camera-check/start`), `JobLeaseService`·`LeaseStatus`·`JobMessages`(2.3.0, 2 s),
  `ErrorCode.Setup.SETUP_CAMERA_CHECK_START_NOT_ALLOWED`, `V74__setup_session_camera_check_attempts.sql`.
- 화면: `features/setup/CameraCheckModal.tsx`·`useCameraWalk.ts`·`cameraCheck.ts`(`deriveCameraWalkView`·`walkFailure`)·`SetupPage.tsx`·
  `SetupCalibrateStep.tsx`(카드 삭제)·`setup.ts`(`pausedForHeights`)·`api.ts`·`useSetupStep1.ts`(`useStartCameraCheck`)·`types.ts`, 목업
  `mocks/setup.ts`(`cameraCheckWalk`).
- 시험: `ai/tests/services/worker/test_site_setup.py`(걷기 시작 대기 뒤 통과·맵 순서, 확인만 다시·늦은 요청 무시, 시도 상한 INCOMPLETE,
  걷기 없음·채널 실패 굽기 0회, 맵 몫 시간 배분, REUSE), `test_camera_check_runner.py`(시도 폴더), `test_client.py`·`test_runner.py`(시도 요청),
  `tests/calibration/test_camera_check.py`(표본 부족 사유·사람 보임), 서버 `SetupSessionIntegrationTest`(걷기 시작·하트비트·늦은 시도·통과)·
  `SetupCameraCheckTest`, 화면 `cameraCheck.test.ts`·`SetupCalibrateStep.test.tsx`·`SetupPage.test.tsx`(걷기 창)·`mocks/setupSupplement.test.ts`.
