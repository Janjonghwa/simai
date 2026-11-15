# S15P31A101-736 녹화 재생 시험 스위치: 실제 알림 켜기·걷기 확인 생략(시험 전용, 기본 꺼짐)

관련 명세·규칙: AGENTS 규칙 5(LIVE 만 실제 알림), 규칙 7(관측과 추정 구분), 3.2(이중 감시·하트비트), 3.10(현장 경보), 7.3(알림 채널),
부록 C.6(워치), 세팅 카메라 확인(-646·-687). 계약 묶음 v7.60.0(`viewer_health` 1.7.0, `job_lease_status` 2.4.0, `site_setup` 1.20.0 ·
`job_heartbeat` 2.19.0 · `job_callback` 2.26.0). DB 마이그레이션 없음.
앞 문서: `S15P31A101-724-replay-no-live-alerts.md`(재생 입력이면 실제 알림 끔), `S15P31A101-721-replay-test-path.md` 3절(걷기 확인 건너뛰기 제안),
`S15P31A101-731-live-alert-cards.md` 1.5(관제 화면 재생 표시 `laptop.test_input`).

## 배경(사용자 결정 2026-11-15)

본 시연은 라이브 카메라다. 녹화 재생은 리허설·시험용이다. -724 이후 재생 입력이면 현장 경보 소리·워치 진동이 꺼지고, 녹화에 여러 명이
나오면 세팅 걷기 확인을 통과하지 못한다(17차는 Pod 코드 수정과 DB 수동 PASSED 로 넘겼다). 재생 시험에서 **실제 알림 흐름과 세팅 완료까지**
보려면 두 기능이 필요하다. **화면 버튼은 만들지 않는다.** 설정 파일·환경 변수로만 켜고 기본은 꺼짐이다.

## 1. 규칙 5 예외의 범위

| 스위치 | 켜는 곳 | 켜면 | 끄면(기본) |
|---|---|---|---|
| `SIMAI_REPLAY_TEST_ALERTS` | EC2 `/opt/simai/.env`(예시 `deploy/ec2/.env.example`) → compose `server` | 노트북이 녹화 파일·합성 영상 입력(-724 시험 입력)을 알리는 동안에도 **워치 진동**을 라이브처럼 보낸다 | -724 그대로 막고 그 이벤트를 기억한다 |
| `SIMAI_REPLAY_TEST_ALERTS` | 현장 노트북 저장소 루트 `.env`(현장 경보 단말 `simai-site-alert`) | 시험 입력이어도 **현장 경보 소리**를 낸다(하트비트 `alarm.sounding_event_ids` 에도 싣는다) | -724 그대로 소리 끔 |
| `SIMAI_REPLAY_TEST_SKIP_CAMERA_CHECK` | EC2 `/opt/simai/.env` → compose `server` | 노트북 카메라 입력이 **모두 FILE** 인 동안 세팅 카메라 확인 걷기를 건너뛴다 | 걷기 확인 그대로 |

- 예외는 「시험 입력의 실제 알림 차단을 푸는 것」과 「재생 입력의 걷기 확인 생략」뿐이다. 이벤트 기록·판정·Tier 0 는 바꾸지 않는다.
  이벤트 `source` 는 지금처럼 LIVE 다(-724 남은 일 1 그대로, 규칙 7 의 출처 표시는 여전히 넘긴 일).
- 실제 카메라(RTSP) 입력이면 두 스위치 모두 동작에 아무 영향이 없다. 알림은 원래 나가고, 걷기 확인은 그대로 한다.
- 알림 스위치는 단말·EC2 가 **따로** 본다(현장 경보는 Tier 0 → 노트북, 워치는 EC2 → 워치 경로라 막는 곳이 둘이다, -724). 한쪽만 켜면 그쪽만
  나간다. 같은 이름으로 둔 것은 운영자가 「둘 다 켰는지」 한눈에 맞춰 보게 하려는 것이다.
- 걷기 확인 생략은 EC2 한 곳에서 판단한다(워커 설정은 바꾸지 않는다, 아래 3절).

## 2. 표시 의무

켜진 효과가 실제로 날 때(재생 입력 + 스위치 켜짐)만 크게 표시한다. 조작은 없다.

| 자리 | 표시 | 근거 |
|---|---|---|
| 현장 경보 단말 | 「재생 시험 · 실제 알림 켜짐 (녹화 파일 재생 중이지만 경보 소리를 실제처럼 냅니다. 본 시연 전에 SIMAI_REPLAY_TEST_ALERTS 를 끄세요)」 빨간 띠(26pt) | 단말 스위치 + 중계 보고 시험 입력 |
| 관제 화면 작업 세션 바(-731 의 「녹화 재생 중 · 실제 알림 꺼짐」 자리) | 「재생 시험 · 실제 알림 켜짐」 빨간 배지 | `viewer_health` 1.7.0 `laptop.replay_test_alerts` + `laptop.test_input` |
| 작업 관리 관제 적용 상태 맨 위 줄 | 같은 문구, 경고(bad) 줄 | 같음 |
| 세팅 2단계(보정 대기) 화면 | 「재생 시험: 걷기 확인 생략」(설명 한 줄) | 세션 `camera_check.status = SKIPPED_REPLAY` |

- 스위치가 켜져 있어도 RTSP 입력이면 띠·배지를 띄우지 않는다(동작이 라이브와 같아 표시가 실제와 어긋난다). 켠 채 잊는 위험은 4절.
- 관제 화면의 표시는 **EC2 스위치** 값이다. 단말 스위치 값은 EC2 가 모른다(노트북 하트비트에 싣지 않았다, 4절).

## 3. 걷기 확인 생략 경로

워커가 입력 종류를 아는 방법으로 **세팅 대여 하트비트 응답**(`job_lease_status` 2.4.0 `camera_check_skip`)을 골랐다.

```
relay(source_kind) → 단말 → laptop_heartbeat cameras[].source_kind ─▶ EC2 HealthState.replayFileOnly()
EC2 ReplayTestSwitch.cameraCheckSkipAllowed() = SKIP 스위치 && 살아 있는(60초 안) 노트북 모두 cameras[] 가 비지 않고 전부 FILE
  ├─ JobLeaseService: site_setup 하트비트 ACTIVE 응답에 camera_check_skip {reason: REPLAY_TEST} (허용인 동안만, 응답마다 다시 판단)
  │     └─ 워커 ctx.camera_check_skip(마지막 ACTIVE 응답 값) ─▶ SiteSetupHandler._run_camera_check:
  │          단계에 들어설 때 또는 「걷기 시작」·「확인만 다시」를 기다리는 동안 값이 있으면 걷지 않고 camera_check SKIPPED_REPLAY 를 싣고 맵으로
  └─ 관문(SetupCameraCheck.satisfied): PASSED, 또는 SKIPPED_REPLAY + 지금도 허용 + 그 보정 = 세션 보정
        ├─ 세팅 완료(SetupCompletionService·SetupCompletionGate) ├─ 세션 조회 camera_check_passed·complete_blocked_reason
        └─ 구역 단계 저장(SetupZonePlanService)
```

- **왜 하트비트 응답인가**: 입력 종류는 EC2 가 이미 하트비트로 알고 있고(-724), 워커가 그 순간 값을 받는다. 세팅 작업(job_request)에 실으면
  작업을 만든 뒤 RTSP 로 바꾸는 경우를 놓치고, Tier 0 상태 파일로 알리면 같은 Pod 의 Tier 0 경로·I0 를 고쳐야 한다. 응답마다 다시 판단하므로
  실제 카메라로 돌아오면 다음 응답에서 빠진다.
- 워커는 EC2 신호만 따른다(워커 `camera_check_enabled` 는 그대로, 스위치를 워커에 두지 않는다). 걷는 중(시도 중)에는 보지 않는다.
- SKIPPED_REPLAY 요약: 빈 표본·판정(`idle` 모양), `problems` 빈 배열, `user_wait` 없음(계약이 막는다), 그 전 시도가 있으면 `attempt` 를 남긴다.
  검증 기준선(`baseline`)은 만들지 않는다.
- **관문은 완료 시점에 다시 본다**: SKIPPED_REPLAY 는 그 순간에도 스위치가 켜져 있고 입력이 녹화 파일일 때만 통과다. 스위치를 끄거나 RTSP 로
  돌아오면 같은 세션은 `SETUP_CAMERA_CHECK_NOT_PASSED` 로 다시 막힌다(라이브 운영이 재생 시험 보정으로 완료하지 않게).
- `SetupCameraCheck.passed` 는 그대로 PASSED 만 본다. 재사용 후보(`SetupReuseCandidates`)는 이 판정을 쓰므로 SKIPPED_REPLAY 세션은 후보에서 빠진다
  (-721 제안 4).
- 세션 조회 `camera_check_phase` 에 `SKIPPED_REPLAY` 를 더했다(걷기 창을 열지 않음, `paused_reason` 없음).
- 녹화 파일과 실제 카메라가 섞이면(한 대만 RTSP) 건너뛰지 않는다. 합성 영상(SYNTHETIC)·입력 종류 모름(이전 중계)·단말 꺼짐(cameras 없음)도
  건너뛰지 않는다(확실한 값만 센다, -724 와 같은 원칙의 반대 방향).

## 4. 운영 절차

### 켜기(리허설·재생 시험)

1. EC2: `/opt/simai/.env`(deploy.sh 가 쓰는 파일) 에 필요한 것만 `true` 로 적는다.
   ```
   SIMAI_REPLAY_TEST_ALERTS=true            # 재생 중 워치 진동
   SIMAI_REPLAY_TEST_SKIP_CAMERA_CHECK=true # 재생 입력이면 세팅 걷기 확인 생략
   ```
   `docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml up -d server` 로 서버만 다시 띄운다(기동 로그에
   `ReplayTestSwitch` WARN 이 남는다).
2. 현장 노트북: 저장소 루트 `.env` 에 `SIMAI_REPLAY_TEST_ALERTS=true` 를 적고 현장 경보 단말을 다시 띄운다(기동 로그 WARN). 걷기 생략은 단말
   설정이 필요 없다. 단, 단말이 돌아야 EC2 가 입력 종류를 안다(하트비트).
3. 영상 중계를 녹화 파일(`kind = "file"`)로 띄운다. 관제 화면 세션 바에 「재생 시험 · 실제 알림 켜짐」, 단말에 빨간 띠가 뜨는지 본다.
4. 세팅을 시작하면 보정 뒤 걷기 창 없이 맵 단계로 가고, 세팅 화면에 「재생 시험: 걷기 확인 생략」이 보인다.

### 끄기(본 시연 전 필수)

1. EC2 `/opt/simai/.env` 의 두 값을 `false`(또는 줄 삭제)로 하고 같은 `up -d server`. 기동 로그에 `ReplayTestSwitch` WARN 이 없어야 한다.
2. 노트북 `.env` 의 `SIMAI_REPLAY_TEST_ALERTS` 를 지우거나 `false`, 단말을 다시 띄운다.
3. 재생 시험으로 걷기를 건너뛴 세팅은 끈 뒤에는 완료할 수 없다. 본 시연은 라이브 카메라로 새 세팅(걷기 확인)을 한다.

## 명세에 없어 정한 수치와 근거

| 값 | 근거 |
|---|---|
| 기본 꺼짐, 값은 `true/false`(단말은 `1/0/yes/no/on/off` 도, 모르는 값이면 기동 거부) | 오타로 켠 줄 알고 시험하거나 켠 줄 모르고 운영하지 않게 |
| 「녹화 파일뿐」 판단에 하트비트 60초 유지(`TEST_INPUT_HOLD_MS`) 재사용 | -724 와 같은 창. 실제 카메라로 바꾸면 다음 하트비트(1초)가 바로 바꾼다 |
| 표시는 재생 입력 + 스위치일 때만 | 표시가 실제 동작과 같게(-731 원칙) |

## 버린 대안

- **세팅 작업(job_request)에 입력 종류를 실어 보냄**: 작업을 만든 뒤 입력을 바꾸면 틀린 값이 남는다. 계약도 I5 입력 메이저 계열을 건드린다.
- **워커 환경 변수로 걷기 확인 끄기**(17차 임시 수정과 같음): Pod 템플릿을 바꿔야 하고 RTSP 여부를 모른다. 서버 관문은 여전히 막는다.
- **관리자 화면 버튼**: 사용자 결정으로 만들지 않는다. 켠 채 잊는 위험도 커진다.
- **스위치 하나(`SIMAI_REPLAY_TEST`)로 둘 다**: 알림 흐름만 보고 걷기 확인은 하고 싶은 시험(또는 그 반대)을 못 한다. 두 개로 나눴다.

## 시험

- server: `ReplayTestSwitchTest`(기본 꺼짐, 켜도 FILE 뿐일 때만, RTSP·섞임·SYNTHETIC·모름·HealthState 없음은 안 됨), `SetupCameraCheckTest`
  (SKIPPED_REPLAY 는 허용일 때만 관문 통과·passed 아님·단계·저장), `WatchAlertReplayTestSwitchTest`(켜면 시험 입력도 보냄·기억 안 함, 끄면 -724
  그대로), `SetupReplayTestSwitchIntegrationTest`(스위치 켠 문맥: FILE → 하트비트 응답 `camera_check_skip`·세션 SKIPPED_REPLAY 통과·완료 200,
  RTSP → 응답에 없음·완료 409 `SETUP_CAMERA_CHECK_NOT_PASSED`, FILE+RTSP 섞임 → 없음, viewer_health 1.7.0 두 칸), 기존
  `HealthIntegrationTest`(기본 `replay_test_alerts` false, 1.7.0)·`WatchAlertIntegrationTest`(-724 그대로)·`SetupCompleteIntegrationTest`·
  `SetupSessionIntegrationTest`(lease 2.4.0)·`WorkerJobIntegrationTest`.
- ai 워커: `test_client`(2.4.0 해석·모르는 사유 버림), `test_runner`(응답마다 덮어씀·거둠), `test_site_setup`(건너뛰기면 걷지 않고 SKIPPED_REPLAY
  후 미리보기·굽기, 대기 중에 오면 그때 건너뜀, 없으면 지금처럼 대기 마감 실패, 계약 검증).
- 단말: `test_replay_test_switch`(.env 읽기·기본 꺼짐·잘못된 값 거부, 끄면 소리 끔·띠, 켜면 FILE 에도 소리·하트비트 sounding·빨간 띠, 켜도 RTSP 는
  띠 없음).
- front: `TestInputBadge`(켜짐 배지·꺼짐·RTSP), `run.test`(적용 상태 줄), `cameraCheck.test`(SKIPPED_REPLAY 통과·걷기 창 닫힘·서버 거부 따름),
  `SetupCalibrateStep.test`(「재생 시험: 걷기 확인 생략」 표시).
- 계약: `validate_examples.py`, Python 생성 모델 `--check`·`ai/tests/contracts`, TS `check:generated`.

## 잔여 위험

- **켠 채 잊음**: RTSP 에서는 동작이 같아 표시도 없다. 그 상태로 재생을 띄우면 실제 알림이 나가지만 그때는 띠·배지가 뜬다. 끄는 것은 4절 절차와
  기동 로그 WARN 에 기댄다.
- **단말·EC2 스위치 어긋남**: 관제 화면은 EC2 스위치만 보인다. 단말만 켜면 현장 소리는 나는데 관제 화면은 「실제 알림 꺼짐」이다(반대도 같다).
  단말 띠는 단말 스위치를 그대로 보인다. 노트북 하트비트에 단말 스위치를 싣는 것은 넘긴 일.
- **재생 시험 이벤트가 LIVE 로 남는다**: -724 남은 일 1(입력 출처 필드) 그대로. 재생 시험 기간의 경보·지표를 제품 성능으로 쓰지 않는다(규칙 7).
- **SKIPPED_REPLAY 세션은 틀어짐 감시 기준선이 없다**: 걷기 확인이 기준선을 만들기 때문이다. 재생 시험 관제에서 틀어짐 경고가 「기준선 없음」일 수 있다.
- 녹화 재생으로 만든 보정은 녹화 당시 카메라 자리 기준이다. 스위치가 켜져 있는 동안 완료할 수 있으므로, 재생 시험 뒤 라이브로 바꾸면 완료 관문이
  다시 막는 것에 기대어 새 세팅을 한다.

## 넘긴 일

- 노트북 하트비트(`laptop_heartbeat`)에 단말 재생 시험 스위치 상태를 실어 관제 화면이 두 스위치를 함께 보이게 하기(마이너, 필요하면).
- -724 남은 일 1(이벤트 입력 출처 `input_provenance`)·2 의 경보음 부분(재생 중 관리자 화면 경보음).
- 명세 정리: 3.10·7.3 에 「시험 전용 스위치(기본 꺼짐)로 재생 입력의 실제 알림 허용」 예외, 세팅 카메라 확인 절에 SKIPPED_REPLAY.
