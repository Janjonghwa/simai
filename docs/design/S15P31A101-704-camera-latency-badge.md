# S15P31A101-704 카메라 칸 판정 지연 배지

관련 명세: 6.1(T_lag, System Health lag DEGRADED 600 ms), 6.6(capture_to_risk p95 600 ms), 12.3(I1 health), 12.4(viewer_health),
13장(관제 화면). 사용자 결정 2026-11-14: 상단바 「지연 126 ms」는 무슨 지연인지 알 수 없고, GPU 를 꺼 두면 「끊김」이 오류처럼
보인다. 지연은 각 카메라 칸 오른쪽에 보이고, 화면은 필요한 정보만 둔다(이번 UX 재설계 원칙). front 만 바꾼다(계약·서버 그대로).

## 바꾼 화면

- 카메라 영상 칸 `CameraTile`(CCTV 관제 4분할, 실시간 관제의 「CCTV로 확인」 패널 `CamSplitPanel`, 떠 있는 CCTV `CctvFloat`가 함께 씀)
  오른쪽 위: 그 카메라의 촬영→판정 지연 `● 180 ms`(색 점·글자) 또는 그 칸만 `끊김`. 그 밖에는 아무것도 두지 않는다.
  예전 `● LIVE`·`실시간`·`· 180 ms` 글은 뺐다.
- 영상 없는 역할의 상태 칸 `StateTile`: 가운데 `실시간`/`끊김`은 그대로, 지연 ms 는 가운데 글에서 빼고 오른쪽 위 배지로 옮겼다
  (끊김은 가운데 글이 있으므로 배지에는 지연만).
- 상단바: 「지연」 → 「판정 지연」. GPU(Tier 0) 꺼짐이면 숨긴다. 색은 그대로 두 단계(600 ms 이상 `delay`). -702 에서 GPU 점
  마우스 올림 정보창으로 옮길 예정이라 더 손대지 않았다.
- 코드: `lib/tier0Lag.ts`(`LAG_OK_MS`·`lagLevel`), `features/cctv/cctv.ts`(`tileBadge`), `features/cctv/CctvPage.tsx`(`CameraBadge`),
  `hooks/useTier0Connected.ts`, `features/layout/TopBar.tsx`. 시험: `__tests__/features/cctv/{CctvPage,CamSplitPanel,cctvCameras}`,
  `__tests__/features/layout/TopBar`, `__tests__/lib/tier0Lag`.

## 데이터 경로

`/topic/health`(viewer_health, 1 Hz) → `realtime.store.receiveHealth` → `lagSample`(-665 에 이미 있던 경로) → 최근 5 초 창 평균.

- 카메라별 지연 = `tier0_health.pipeline_lag_ms + (tier0_health.t_latest_global − tier0_health.cameras[].last_capture)`.
  T_lag 는 Tier 0 가 노트북 시계 차이를 보정해 잰 값이고, 뒤 두 시각은 모두 노트북 촬영 시각이라 차이에 시계 차이가 없다.
  그 카메라의 마지막 처리 프레임이 판정 시각에 얼마나 묵었는지다.
- 계약에 카메라별 촬영→판정 값 필드는 없다. `capture_to_risk_ms` 는 전체 백분위뿐이고, `cameras[].frame_age_ms` 는 Tier 0 가 싣지
  않으며 실려도 시계 보정이 없어 쓰지 않는다(`lib/tier0Lag.ts` 머리 주석). 그래서 계약·서버를 바꾸지 않고 위 값을 쓴다.
- 끊김 = Tier 0 가 그 카메라 `status` 를 `NO_SIGNAL`·`WAITING_KEYFRAME` 으로 알린 것(`cameraState`, 기존).

## 색 기준

초록 `ok` < 300 ms, 노랑 `sev-caution` < 600 ms, 빨강 `sev-critical` ≥ 600 ms(사용자 결정). 600 은 명세 6.1 DEGRADED·6.6 p95 와 같다.
300 은 명세에 없는 값으로 사용자가 정했다. 상단바의 지연 색 경계도 `> 600` 에서 `≥ 600` 으로 맞췄다(`isLagHigh = lagLevel === 'high'`).
`front/CONVENTION.md` 는 빨강·노랑을 위험 등급에만 쓰라고 하지만 이 배지는 사용자가 신호등 색을 정해 예외로 적었다.

## GPU 꺼짐 판정

`useTier0Connected()` = EC2 와 이어짐(`useLinkAlive`, health 3 초 안) **그리고** `viewer_health.tier0_link === 'CONNECTED'`.
EC2 는 Tier 0 I1 하트비트가 끊기면 `tier0_link` 를 `DISCONNECTED` 로 싣는다(GPU 를 꺼 둔 정상 상태도 같다). 거짓이면 칸 배지·상단바
지연을 모두 숨긴다. 이전에도 `healthCameras`·`lagSample` 이 `tier0_link` 를 보고 비웠지만, 배지 함수(`tileBadge`)와 상단바가 이 신호를
직접 받게 해 끊기기 전 표본이 남아도 보이지 않게 했다(시험으로 고정).

## 버린 대안

- 계약에 카메라별 `capture_to_risk_ms` 추가: I1 LIVE 계약이라 Lead 승인·Tier 0 변경이 필요하고, 위 계산으로 같은 뜻을 얻는다.
- LIVE 점 유지: 배지가 두 개(LIVE·지연)가 되어 최소 화면 원칙에 어긋난다. 카메라가 멈추면 그 칸 지연이 커지다가 Tier 0 가 끊김으로
  알리므로 카메라 쪽 신선도는 지연 배지가 대신한다. 브라우저 중계 자체가 멈추면 기존대로 칸이 흐려지고 상태 줄(중계 끊김)이 뜬다.
- 모의 화면(`CctvDemo`, 중계에 못 붙었을 때)에도 지연 배지: 실제 영상 칸이 아니라서 두지 않았다(LIVE 시계 그대로).

## 남은 위험

- 프레임은 들어오는데 디코딩만 멈춘 채 `playing` 단계로 남으면(1 초 넘게 새 프레임 없음) 예전 LIVE 점이 꺼지던 신호가 이제 없다.
  칸은 흐려지지 않는다.
- 카메라별 값은 1 Hz health 의 마지막 처리 프레임 기준이라, 카메라가 막 멈춘 직후 몇 초는 값이 커지는 중에도 초록·노랑일 수 있다.
- 영상 없는 역할은 GPU 꺼짐이면 여전히 「카메라 상태를 받지 못했습니다」 한 줄을 본다(카메라 목록이 Tier 0 health 에서 오므로). 이번
  범위 밖이다.
