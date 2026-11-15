# S15P31A101-719 세팅 실패·대기 화면이 원인과 다음 행동을 말하게 하기

관련 명세: 3.2(세팅 세션·카메라 확인), 3.10(하트비트·현장 연결), 4.3·4.5(1단계 각도 헬퍼), 6.1(T_lag), 12.4(viewer_health), 13장(관제 화면).
근거: 17차 UX 점검(2026-11-15, 캡처는 저장소 밖 `simai-local/ux-audit/2026-11-15`, 시간표 `simai-local/session17/timeline.txt`).
세팅 화면 원칙(11-14 재설계): 최소 화면, 큰 창, 뒤늦게 막지 않기, 모호한 라벨 없음, 상태·오류에 다음 행동. front 만 바꿨다(서버·계약 그대로).

## 항목별 화면 문구

| # | 화면 | 전 | 후 |
|---|---|---|---|
| 1 | 세팅 실패 안내(`setupFailures.ts`) CAMERA_CHECK_NOT_PASSED | 서버에서 세팅을 끝내지 못했어요 / 잠시 뒤 다시 시도하세요… (원인은 「자세히」) | 걷기 창과 같은 원인: 「어느 카메라에서도 사람이 보이지 않았습니다」·「카메라 4에서 사람이 충분히 보이지 않았습니다」·「카메라 2 위치가 보정과 맞지 않습니다」·「「걷기 시작」을 누르지 않아 카메라 확인 시간이 지났어요」 + 다시 시도 뒤 할 일 |
| 1 | 〃 BACKGROUND_INCOMPLETE | 사람 없는 배경을 다 모으지 못했어요 | 카메라 2, 카메라 4에서 사람 없는 배경을 다 찍지 못했어요 / 그 카메라 앞에서 사람과 장비가 비키고 영상이 들어오는지 확인한 뒤 다시 시도하세요 |
| 1 | 〃 워커·서버가 내는 그 밖의 코드 | 폴백 | CAMERA_CHECK_UNAVAILABLE·SETUP_QUEUE_TIMEOUT·CANCELLED·LIVE_EXP_BLOCKED·INVALID_INPUT·INTERNAL_CONTRACT_ERROR 마다 무엇이/어떻게 |
| 2 | 1단계 카메라 배지·「지금 할 일」 | 확인 중 / 카메라 상태를 확인하고 있어요 (끝없이) | 배지 「확인 못 함」 / 현장 게이트웨이와 연결이 끊겨 카메라를 확인할 수 없어요 · 게이트웨이 노트북과 카메라 헬퍼가 켜져 있는지 확인하세요. 확인 없이도 「다음」으로 시작할 수 있어요 |
| 2 | 1단계 「이전 맵 재사용」 판정·맵 목록 | 카메라가 이전 맵 때와 같은 자리인지 확인하고 있어요 / 카메라 위치를 확인하고 있어요 | 현장 게이트웨이와 연결이 끊겨 카메라 위치를 확인할 수 없어요 · 게이트웨이 노트북이 켜져 있는지 확인하거나 새 맵 만들기를 골라 주세요 / 게이트웨이 연결이 끊겨 카메라 위치를 확인할 수 없어요 |
| 3 | 걷기 창(`walkStatus`) | 「사람 안 보임 0/30」이 이어지다 마감 뒤 조용히 실패 | 걷기 10초 뒤에도 모든 카메라 0회면 「10초 동안 어느 카메라에도 사람이 보이지 않았어요」 + 한 사람만 천천히 지나가기·계속 0이면 시도 중단 후 관제(Tier 0) 확인. 카운트다운 0 뒤에는 「덜 보인 카메라: 카메라 2, 카메라 4」 |
| 3 | 걷기 카운트다운 | 다시 시도한 세션의 1번 시도가 앞 세션 시작 시각을 써 바로 0초(추정) | 기준을 세션+워커 시도 ID(`attemptKey`)로 바꿔 새로 센다 |
| 4 | CCTV 칸 지연 배지 | `● 209 ms` | `● 판정 209 ms`, 마우스 올림: 판정 지연: 이 카메라가 찍은 뒤 위험 판정(Tier 0)까지 걸린 시간, 최근 5초 평균. 300 ms 미만 초록, 600 ms 이상 빨강 |
| 5 | 배경 촬영 창(`captureStatusText`) | 0초인데 「거의 다 찍었습니다」 1분 넘게 | 「아직 찍는 중: 카메라 4. 그 카메라 앞을 비우고 조금만 더 기다려 주세요.」 못 찍고 끝나면 가린 카메라가 없을 때 덜 찍은 카메라 이름 |
| 추가 1 | 상단바(세팅·관제·CCTV 공통) | 게이트웨이가 끊겨도 아무 표시 없음(17차 13:11) | GPU 점 앞 「현장 연결 끊김 · 감시 중단」, 마우스 올림에 끊긴 시각·멈춘 것·확인할 것 |
| 추가 3 | 작업 전 확인 창 | 「작업 시작」이 꺼진 까닭이 체크 칸이면 말이 없음 | 위 확인 칸 두 개를 체크하면 「작업 시작」이 켜집니다 |

## 근거로 쓴 값(서버가 이미 준다)

- 실패 원인: 세션 `failure_code`·`failure_message`, 카메라 확인 결과 `camera_check.problems`(걷기 창과 같은 `walkFailure`), 배경 미완료 카메라는 워커 메시지의
  「미완료 카메라: [...]」 부분, 없으면 `background_collection_pct` 100 미만 카메라. 서버 코드·메시지 원문은 계속 화면에 쓰지 않는다(기존 결정).
- 헬퍼 끊김: `GET /api/cameras/coverage-guides` 의 `age_ms`(마지막 헬퍼 결과 나이). 헬퍼 결과를 한 번도 못 받았으면(`EMPTY`) 첫 빈 응답부터 잰다.
- 현장 연결 끊김: `viewer_health.laptop.link`(EC2 가 하트비트 1초 주기 3회 누락 = 3초면 DISCONNECTED). Tier 0 의 `health.gateway_link` 와 뜻이 같지만 GPU 없이도 온다.

## 명세에 없어 정한 수치

- 헬퍼 끊김 60초: 서버 STALE(10초)만으로는 끊김이라 하지 않는다. 헬퍼는 관리자가 보는 동안만 계산하고 CPU 한 라운드가 16~24초다.
- 걷기 「사람 안 보임」 안내 10초: 걷기 목표 30초의 1/3. 사람이 한 번이라도 보이면 띄우지 않는다.
- 현장 연결 끊김은 GPU 가 꺼져 있으면(작업·세팅 없음) 숨긴다: -704 사용자 결정(GPU 를 꺼 둔 정상 상태를 오류처럼 보이지 않게)과 같은 기준.

## 신호가 없어 하지 못한 것(제안)

**카메라 시각 동기화 문제(17차 90분).** 17차 걷기에서 「사람 0/30」이 이어진 까닭은 게이트웨이 재생의 프레임 시각 불확실성(`uncertainty_no_pts_ms` 45 ms,
`gateway/recorder` 기본값)이 AI 보간 기준(`max_timestamp_uncertainty_ms` 25 ms, `geometry/interpolation.py` `TIMESTAMP_UNCERTAIN`)을 넘어
모든 view 가 버려진 것으로 본다(Lead 지시의 원인과 같고, 06:17 에 20 ms 로 낮춰 다시 재생했다). 지금 Tier 0 health 와 세팅 `camera_check` 어디에도 이 버림이 실리지 않아 화면은 「사람이 안 보임」과
구분하지 못한다. 필요한 신호(둘 다 마이너, health 는 I1 LIVE 계약이라 Lead 승인 필요):

1. `contracts/messages/health.schema.json` `camera_health`: 이미 있는 선택 필드 `timestamp_source`·`timestamp_uncertainty_ms` 를 Tier 0(`services/tier0/health.py`)가
   보고 구간 최대값으로 채우고, `frames.timestamp_uncertain_dropped`(정수, 보고 구간에 시각 불확실성 기준 초과로 뺀 view 수)를 더한다.
   화면: 받은 view 의 절반 넘게 버려지면 CCTV·관제 칸과 상단바에 「카메라 시각 동기화 문제로 영상을 판정에 못 쓰는 중」.
2. `contracts/messages/site_setup.schema.json` `camera_check.problems[].reason` 에 `TIMESTAMP_UNCERTAIN`(카메라별, 워커 `services/worker/site_setup.py` 의
   카메라 확인이 보간에서 뺀 수가 대부분일 때). 화면: 걷기 창·실패 안내가 「카메라 시각 동기화 문제로 사람을 셀 수 없어요 · 게이트웨이 시각 설정을 확인하세요」.
3. 별도로 게이트웨이 재생 기본값 45 ms 와 AI 기준 25 ms 가 서로 맞지 않는다. 어느 쪽을 바꿀지는 Lead 결정(이 티켓 범위 밖).

## 확인한 것(고치지 않음)

- 세팅 실패가 12초 간격으로 두 번 기록: 제품 문제가 아니다. 17차 시간표의 `setup FAILED` 두 줄(14:28:10·14:28:22, 문구 같음)은 세팅 상태 감시 스크립트
  (`setup-wait.sh`, 15초 주기)가 두 개 떠 있어 각자 한 줄씩 남긴 것이다. 서버는 FAILED 세션을 다시 덮지 않는다(`SetupService.onFinished`).
- 서명 뒤 「작업 시작」 22초 늦음: 꺼지는 조건은 세팅 완료·확인 칸 둘·서명 1명뿐이고, 그때 창에 세팅 안내가 없었으며 서명 1명이 보였으므로 확인 칸이 빠져 있던 것으로
  본다(기록에 체크 상태가 없어 확정은 못 함). 꺼진 까닭을 창 본문에 쓰게 했다.

## 버린 대안

- 표에 없는 실패 코드에 서버 메시지 원문을 한 줄 붙이기: 기존 결정(원문은 화면에 쓰지 않음, SetupPage 시험)과 어긋나 버렸다. 대신 워커가 내는 코드를 표에 넣었다.
- 헬퍼 끊김을 서버에 새 필드로 받기: `age_ms` 로 충분하다.
- 현장 연결 끊김을 각 화면(세팅·관제·CCTV)에 따로 두기: 세 화면에 모두 있는 상단바 한 곳이 최소 화면 원칙에 맞다.
- 걷기 「사람 안 보임」을 서버 `elapsed_s` 로 재기: 브라우저 카운트다운과 기준이 둘이 된다. 카운트다운 기준 결함을 고쳐 그것을 쓴다.

## 남은 위험

- 걷기 카운트다운 0초 원인은 시간표만으로 확정하지 못했다(새 세션의 같은 시도 번호를 앞 기준으로 쓴 경우를 막았다).
- 현장 연결 끊김은 GPU 를 끈 채 CCTV 만 볼 때는 뜨지 않는다(CCTV 화면 자체의 중계 끊김 줄이 대신한다).

## 코드·시험

`front/apps/admin/src/features/setup/{setupFailures,SetupCalibrateStep,setup,cameraBadge,nextStep,useReuseScene,useSetupStep1,cameraCheck,CameraCheckModal,useCameraWalk,backgroundCapture,BackgroundCaptureModal,SetupPage}`,
`features/cctv/CctvPage.tsx`, `features/layout/{SiteLinkBadge.tsx,status.ts,TopBar.tsx}`, `features/work/preflight/PreflightConfirmDialog.tsx`.
시험: `__tests__/features/setup/{setupFailures,cameraBadge,nextStep,reuseChoice,backgroundCapture,cameraCheck,SetupCalibrateStep}`, `__tests__/features/cctv/{CctvPage,CamSplitPanel}`,
`__tests__/features/layout/status`, `__tests__/features/work/preflight/PreflightConfirmDialog`.
