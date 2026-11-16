# S15P31A101-774 GPU 조작 신호는 보이는 화면·최근 조작만, 조작만으로 미루는 유휴에 상한

관련 명세: 3.12(GPU 사용 원칙), 3.2(Tier 0 우선). 앞 티켓: -702 자동 정지, -708 유휴 기준 시각·조작 신호(`S15P31A101-708-idle-autostop-baseline.md`).
계약 변경 없음(관리 REST 는 계약 밖), DB 변경 없음(Flyway 없음).

## 현상(2026-11-16)

09:00 admin1 이 GPU 를 켜고 09:12 READY 뒤 관제·세팅·작업 없이 약 2시간 켜져 있었다(시간당 4.39 USD). Tier 0 가 I1 에 붙어 있으면 관리자 화면 조작
신호(`POST /api/gpu/presence`)도 사용으로 세는데, 브라우저 5곳이 화면(일부는 /work)을 열어 둔 채 신호를 계속 보내 유휴 30분이 끝나지 않았다.
신호를 한 번만 보내도 30분이 늘어나고, 상한이 없어 신호가 이어지는 한 끝없이 켜져 있을 수 있었다.

## 바꾼 것

### front(`front/apps/admin/src/features/gpu/useGpuPresence.ts`)

- 보내는 조건: 화면이 보이고(`document.visibilityState === 'visible'`) 마지막 사용자 조작(`pointerdown`·`keydown`·`touchstart`·`wheel`, window capture)이
  5분(`GPU_PRESENCE_ACTIVE_WINDOW_MS`) 안이며 마지막 신호 뒤 1분(`GPU_PRESENCE_MIN_INTERVAL_MS`)이 지났을 때. 조작 순간과 1분 주기 점검에서 판단한다
  (`shouldSendGpuPresence`, 순수 함수).
- 마우스 이동·탭 열림·상태 주기 조회는 신호가 아니다.

### server(`GpuPowerService.idle`, `GpuProperties.presenceMaxExtension`, `simai.gpu.presence-max-extension`)

- 실제 사용 기준 = max(준비 완료, `GpuActivity.lastUse`). 기준 + 유휴 N분 안이면 지금처럼 유지한다.
- 그 뒤 Tier 0 가 붙어 있고 조작 신호가 N분 안에 있으면 유지하되, 기준 + 상한(기본 90분)을 넘으면 조작이 이어져도 `IDLE` 로 끈다.
- LIVE·세팅·업로드·분석 작업 중이면 그 전에 `autoStopBlock` 이 막으므로 상한과 무관하게 끄지 않는다(시험으로 확인).
- 상한 0·음수·비움은 기본 90분이다(다른 `simai.gpu.*` 시간과 같은 `positive` 규칙). 상한을 끄는 설정은 두지 않았다(그러면 같은 과금 버그가 돌아온다).

### 정지 사유 기록

- `gpu_launch_event.reason_code` 는 CHECK 제약이 없지만 새 값을 만들지 않고 `IDLE` 그대로 둔다(유휴 정지라는 뜻은 같고, `IDLE` 을 세는 집계·시험이 그대로다).
- 구분은 같은 사건(STOP_REQUESTED·STOPPED)의 `detail` 에 남긴다. 로그 `GPU 자동 정지(IDLE, <detail>)` 에도 같은 글을 싣는다.
  - 상한: `PRESENCE_CAP basis=READY|LAST_USE since=<기준> presence_only_minutes=<분> cap_minutes=90 last_presence=<시각>`
  - 일반 유휴: `basis=READY|LAST_USE|PRESENCE since=<기준> idle_minutes=<N>`
- 다른 사유(BOOT_TIMEOUT·SESSION_ENDED·FAILED·ADMIN)는 detail 없음 그대로.

## 정한 수치와 근거

| 수치 | 값 | 근거 |
|---|---|---|
| 조작 유효 시간(front) | 5분 | 화면을 읽거나 3D·CCTV 를 보는 몇 분은 사용으로 두고, 자리를 뜬 화면은 서버 유휴 30분보다 훨씬 빨리 빠진다. CCTV 원격 시청의 입력 없음 멈춤(10분)보다 짧다 |
| 신호 최소 간격·점검 주기(front) | 1분 | 서버 자동 정지 점검 주기(`sweep-interval` 60 s)와 같아 더 자주 보내도 소용없다(-708 과 같음) |
| 조작만으로 미루는 상한(server) | 90분 | 유휴 기본 30분의 3배. READY 뒤 관제·세팅 전 화면 점검·준비에 충분하다. 조작만 이어지는 세션의 과금을 최대 약 1.5시간(H100 4.39 USD/h 기준 약 6.6 USD)으로 묶는다. 잘못 끄면 다시 켜는 데 약 4분(첫 부팅 20분+)이 들어 너무 짧게 두지 않았다 |

11-16 사례에 대입하면 09:12 READY 기준 10:42 에 꺼진다(관리자가 계속 조작했어도). 조작이 실제로 없었다면 front 변경만으로 마지막 조작 5분 + 30분 뒤 꺼진다.

## 버린 대안

| 안 | 버린 까닭 |
|---|---|
| reason_code 새 값 `IDLE_PRESENCE_CAP` | CHECK 가 없어 Flyway 는 필요 없지만, 유휴 정지라는 뜻은 같고 `IDLE` 을 세는 쪽을 둘로 갈라야 한다. detail 로 충분히 구분된다 |
| 조작 신호를 DB 에 쌓아 화면별로 세기 | -708 과 같은 까닭(1분 쓰기, 재시작 보존 가치 작음). 상한은 실제 사용 기준이라 메모리 마지막 시각으로 충분하다 |
| 조작만으로는 아예 연장하지 않기 | 관리자가 READY 뒤 화면을 보며 준비하는 30분 남짓 사이에 꺼질 수 있다(-708 사용자 결정: 연결 + 최근 조작) |
| 상한을 조작이 끊긴 시각 기준으로(마지막 조작 뒤 연장만 제한) | 조작이 이어지는 한 끝없이 켜지는 원래 문제가 남는다. 실제 사용 기준이어야 묶인다 |
| 화면 GPU 설정(DB 버전)에 상한을 둠 | 설정 화면·DB 변경이 커진다. 운영 고정값(`simai.gpu.*`)과 같은 층으로 충분하고 필요하면 환경으로 바꾼다 |
| front 에서 탭 하나만 보내기(BroadcastChannel 지도자 선출) | 서버는 마지막 시각만 보므로 여러 탭이 보내도 결과가 같다. 복잡도만 늘어난다 |

## 남은 위험

1. 관리자가 관제·세팅·작업 없이 실제로 90분 넘게 화면을 쓰면(예: 3D 맵·카메라 점검만) 조작 중에도 꺼진다. 다시 켜야 한다(약 4분). 상한은 `simai.gpu.presence-max-extension` 으로 늘릴 수 있다.
2. 상단바 남은 시간 문구(`autoStopText`, -733)는 상한을 모른다. 상한에 가까워도 "자동 정지까지 약 N분" 이 조작 기준으로 보일 수 있다(표시만, 정지 판단은 서버).
3. `GpuActivity.lastUse` 는 세팅 세션 `updated_at` 등 전체 표의 최대값이라, 이번 GPU 와 무관한 갱신도 실제 사용으로 센다(-708 부터 같음, 더 오래 켜는 쪽).
4. 서버 재시작 직후 조작 시각은 뜬 시각으로 둔다(-708). 상한은 실제 사용 기준이라 재시작이 상한을 늘리지는 않는다.
5. /work 화면을 터치 기기에서 계속 만지는 사용(현장 태블릿 등)은 여전히 조작이다. 그 경우도 상한 90분이 막는다.
