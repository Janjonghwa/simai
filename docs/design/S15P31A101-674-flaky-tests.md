# S15P31A101-674 간헐 실패 시험과 실제 서버 연결 누수

11-12~13 CI·로컬에서 부하가 걸릴 때만 깨지던 시험을 원인에서 고친다. skip·재시도 장식은 쓰지 않았고, 시험이 보려는 동작은 그대로 둔다.
관련 명세 절은 없다(시험 기반). 제품 동작은 바꾸지 않았다(아래 「제품 코드」).

## 원인과 고침

| 시험 | 원인 | 고침 |
|---|---|---|
| front `useSupplementCapture` 「올리고 다시 구우면…」 | 실제 2초 세션 조회 주기(`SETUP_SESSION_POLL_MS`)를 두 번 기다려 시험 하나가 부하 없이 4.4 s. 부하가 조금만 걸려도 5 s 초과 | mock 시계를 옮긴 뒤 `client.refetchQueries` 로 조회를 바로 한 번 한다(`pollNow`). 대신 「끝난 세션도 다시 굽는 동안은 2초 주기로 조회, 끝나면 멈춤」을 `refetchInterval` 값으로 직접 단정해 주기 조회가 빠진 회귀를 잡는다 |
| front `ZonesPage` 「상한에 닿은 기록에서도…」 | 기록 100단계를 화면으로 채우느라 이름·심각도를 200번 고쳐 화면 전체를 200번 다시 그림(전체 시험 4.2 s) | 앞 100단계는 store `record` 로 채우고(내용 없는 단계, 묶음 이름이 달라 합쳐지지 않음), 끌기 직전 고침·끌기·끄는 중 고침·되돌리기는 화면으로 한다. 상한에 닿은 상태에서 끌기를 하는 의도는 같다 |
| front `ZonesPage` 「드롭다운은 「새 구역 추가」가 먼저…」 | 파일 첫 시험이 첫 요청 준비(fetch·MSW·응답 스키마 첫 검사)와 첫 그리기를 같이 치러 목록 도착이 기본 대기 1 s 를 넘김(바쁜 루프 부하에서 1.4 s, 「Unable to find role="option"」) | `renderPage` 의 도착 대기(목록 3개)에 `ARRIVAL = { timeout: 3_000 }`. 도착은 시험 대상이 아니라 준비라서 시험 시간 5 s 안에서 기다린다 |
| front `PreflightStage` 3D 동적 import | `vi.mock('@/features/viewer3d', importOriginal)` 이 진짜 입구를 펼쳐 three·R3F·drei·후처리 묶음을 처음 변환·적재. 화면의 `lazy(import('@/features/viewer3d'))` 가 기다리는 동안 부하 없이 10.4 s, 부하에서 15 s 대기 초과 | mock 이 `Viewer3D` 대역과 `createExternalSource`(three 를 쓰지 않는 `data/spatialSource`)만 낸다. 이 화면이 런타임에 쓰는 것은 둘뿐이다 |
| front `TbmLayoutEditorDialog` 실제 연결 | `@/lib/api` 를 그대로 써서 주소가 실행 환경 `VITE_API_BASE_URL` 을 따름. MSW 가 모든 요청을 처리(request:match)해도 시험마다 undici 가 그 호스트로 TLS 연결을 연다(탐침 4회) | `vi.mock('@/lib/env')` 로 API·STOMP 주소를 `https://tbm-layout.invalid` 로(`.invalid` 는 이름 풀이가 되지 않는다). 처리기는 `*/api/…` 라 그대로 받는다 |
| front `PreflightStage` 서명 시험들(`signAs`) | 서명 추가는 이미지 만들기·mock 서버 POST·목록 다시 받기를 거치는데 「서명자 1명」을 기본 대기 1 s 로만 찾음(버너 부하 반복에서 1회 초과) | `signAs` 의 서버 왕복 대기 셋에 `ROUND_TRIP = { timeout: 5_000 }`. 쓰는 시험은 모두 30 s 제한이다 |
| front `ZonesPage` 「레이아웃 구역 이름 변경과 새 구역 추가…」 | 추가가 끝나면 화면은 새 구역 초안을 지우고 같은 틱에 만든 구역을 연다(`openZone`). 시험이 mock 서버 상태만 보고 바로 「새 구역」을 고르면, React 가 아직 다시 그리기 전이라 드롭다운 값이 그대로 새 구역이어서 바뀜 이벤트가 일어나지 않고, 뒤이어 만든 작업면 구역이 열려 고소 작업면이 켜진 채 보였다 | 초안이 지워지고 드롭다운이 만든 구역으로 바뀌길 기다린 뒤 고른다(화면의 추가 마무리를 기다림) |
| server `WatchWaveCheckinIntegrationTest`·`WatchMotionMatchIntegrationTest` | 시험이 16~20 s 분량 world_state(10 Hz, 160~200개)를 한꺼번에 넣는다. 대조 스레드가 밀리면(주기 대조의 DB 쓰기, CPU 부하) 대기열 상한 `MAX_PENDING_WORLD`(50)를 넘은 것을 버리는 제품 동작 때문에 표본이 비어 점수가 모자라 잇지 못함. 특징 창 저장 대기(5 s)도 시간 초과를 조용히 넘겨 덜 저장된 채 대조할 수 있었음 | 25개(상한 절반)마다 `matcher.awaitInputs()` 로 대조 스레드가 처리하길 기다린다. 창 저장 대기는 결과를 단정하고 상한 30 s(조건 대기). 「같은 창 재전송」의 고정 `Thread.sleep(500)` 은 표지 창 하나를 더 보내 그것이 저장될 때까지 기다리는 것으로 바꿨다(한 연결의 메시지는 순서대로 대조 스레드 하나에 쌓인다)<br>둘째 원인(CI #946): 두 클래스는 같은 Spring 문맥의 대조기 하나를 쓰고 대조기는 앞 시험의 트랙을 들고 있는데, World 시각 계수기가 클래스마다 따로였다(MotionMatch 는 지금부터, Wave 는 지금 + 60 s 부터 60 s 씩). MotionMatch 다음에 Wave 가 돌면 걷는 사람(1.6~2.2 Hz) 트랙이 손 흔들기(2.0 Hz) 시험과 같은 World 시각에 겹쳐 경쟁 짝이 되고 점수 차이 문턱(0.4)에 걸려 잇지 않을 수 있었다 → 두 클래스가 한 계수기(`WatchMatchTestTimes`, 시작 지금 + 30 s, 45 s 간격)를 쓴다. 가장 긴 시험의 world_state 가 36 s 라 앞 시험 트랙은 늘 다음 시험 창보다 앞이고, 시험 10개 뒤에도 471 s 로 `MAX_FUTURE_MS`(600 s) 안쪽이다 |
| ai `tests/deploy/test_crash_recovery_g3.py` 두 시험 | 하네스(`deploy/scripts/crash_recovery_g3.py`)가 Tier 0 기동 확인(3 s) 뒤에 노트북 시험 상대를 붙인다. R1 은 첫 프레임 약 0.2 s 뒤에 나고 local_alert 는 그때 붙은 노트북에만 한 번 가서, 부하로 기동이 늦으면 놓쳐 60 s 시간 초과(저널에 R1 RAISED `local_alerted=true` 가 있는데 노트북 연결 0.25 s 전) | 노트북을 Tier 0 를 띄우기 전에 붙이기 시작하고(0.2 s 마다 다시 붙음), 신호를 「노트북 local_alert 또는 EC2 가 받은 R1 RAISED 전이」로 넓혔다. 어느 쪽이었는지 시나리오 `trigger` 에 남긴다(EC2 전이는 fsync 뒤라 「fsync 전 kill」 경우가 그 회차에는 빠질 수 있다)<br>그 뒤 `test_late_cameras…` 가 다른 곳에서 깨졌다: 복원 R7(구역 대상)은 감시 확보 근거(VERIFIED)가 해소 유지 시간 3 s 이어져야 해소되는데, 앞 프로세스들은 4~5 s 만에 kill(다시 RESTORED, 새 구간)되고 마지막도 `sleep(3)` 뒤 끝내 부하에서 「근거로 해소됨」이 빈 채 끝났다 → 마지막 단계가 3 s 뒤에도 열린 구역 복원 이벤트가 있으면 해소될 때까지 조건 대기(상한 30 s, 넘겨도 오류로 보지 않음). 사람 대상 복원 이벤트는 더 오래 지켜볼 뿐이라 D-G3-09 판정은 약해지지 않는다. 같은 파일 `test_i1_tls.Ec2Server` 기동 대기 5 s 고정 → 조건 대기 30 s(극부하에서 websockets 첫 import·TLS 준비가 5 s 를 넘김) |

## 제품 코드

- `WatchMotionMatcher.awaitInputs()`(패키지 공개, 시험용): 대조 실행기에 빈 작업을 넣고 기다린다. 기존 시험용 `matchNow()` 옆에 두었고 운영 경로는 부르지 않는다. 버리는 동작(`MAX_PENDING_WORLD`)은 의도된 역압이라 그대로 둔다.
- `deploy/scripts/crash_recovery_g3.py`: 측정 하네스(제품 서비스가 아님)의 시작 순서·kill 신호·마지막 관찰 대기만 바꿨다. Tier 0 서비스 코드는 그대로다.

## 반복 결과

부하는 노트북(22 논리 코어)에서 `node -e 'for(;;){}'` 바쁜 루프를 함께 돌려 걸었다. 「전」은 이 MR 전 코드다.

| 대상 | 전 | 후 |
|---|---|---|
| server 워치 대조 두 클래스(10개) | `--rerun-tasks` 5회(버너 22) 중 1회 실패(MotionMatch 「이어진 뒤 멈춰…」 배정 0/3). CI #921·#946 에서 Wave 「서 있는 세 사람…」 실패 | 5회(버너 8) 10/10 × 5 통과 |
| front 대상 4파일(109개) | 부하 없이·전체 2벌 동시 부하 10회 모두 통과(재현 안 됨). 버너 22 로 10회 중 2회 실패(ZonesPage 상한 시험 2회, 「드롭다운은…」 1회) | 버너 8 로 5회 × 2벌 109/109 통과(최종 코드). 중간 실행에서 새로 드러난 `signAs` 대기·레이아웃 추가 경쟁을 고친 뒤다 |
| 시험 하나 시간(부하 중) | 「올리고 다시 구우면」 4.4 s, 상한 시험 3~7 s, 3D 첫 시험 1.6~4 s(부하 없이 10 s) | 0.46 s, 0.22 s, 0.22 s |
| ai `test_crash_recovery_g3.py`(15개) | 버너 22 로 4회 모두 실패(late_cameras 4/4, reduced 2/4, phantom 1/4, 모두 R1 local_alert 60 s 시간 초과) | 버너 8 로 5회 15/15 × 5 통과, 복원 R7 「근거로 해소」 매회 확인 |
| front 전체(285 파일, 3702개) + TLS 탐침 | Tbm 이 `simai.live` 로 TLS 4회 | 1회 전부 통과, `simai.live` 0회(`.invalid` 만: monitor-edit 29, scene-regen 44, tbm-layout 4, monitor-session 1) |

front `format:check`·`lint`·`typecheck` 통과. ai 하네스 두 파일의 ruff 지적(21개)·형식 차이는 이 MR 전과 같다(검사 범위 밖 파일).

## 버린 대안

- 시험 시간 제한만 늘리기: 실제 2초 주기·200번 다시 그리기·three 적재는 그대로 남아 CI 가 더 느려지면 다시 깨진다.
- 가짜 시계(`vi.useFakeTimers`)로 2초 주기 진행: React Query 주기 조회·MSW·`waitFor` 가 함께 가짜 시계를 타야 해 더 깨지기 쉽다. 조회를 앞당기고 주기 값을 단정하는 쪽이 의도가 분명하다.
- server 시험에 `Thread.sleep` 으로 10 Hz 흉내: 실제 지연이 16 s 늘고 부하에서도 보장이 없다.
- ai 하네스에서 앵커(anchor_wall_ms)를 더 뒤로: 고정 지연을 늘릴 뿐 기동이 그만큼 더 늦으면 같은 경쟁이 남는다.

## 잔여 위험

- ai 시험은 실제 Tier 0 프로세스를 kill -9 하는 실측이라 CPU 를 오래 쓴다. 다른 대기(재시작 뒤 snapshot·health, 60 s)는 조건 대기라 손대지 않았다.
- front 다른 무거운 파일(SetupPage 등)은 이번 범위가 아니다(전체 시험에서 통과).
- 극부하(버너 22 + 전체 front 2벌 + gradle 동시)에서만 본 것은 남겼다: ai 하네스 자식 프로세스 60 s 무출력(`harness_*` 「자식이 기대한 줄을 내지 않았다」), 재시작 뒤 snapshot 60 s 대기 초과, front 범위 밖 `useWorkReplay` 2·`MonitorWorkSession` 1 실패, vitest 워커 기동 시간 초과. 모두 버너 8 반복에서는 나지 않았다.
- server 공유 World 시각 계수기는 두 클래스에 시험이 더 늘면 `MAX_FUTURE_MS`(600 s)에 가까워진다(지금 최대 471 s). 시험을 더할 때 `WatchMatchTestTimes` 주석의 계산을 다시 본다.
