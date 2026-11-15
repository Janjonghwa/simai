# S15P31A101-730 보호구 감시 상태 한 줄 · 작업 전 확인 경고

근거: UX 점검(2026-11-15) 06 관제 행. 관제 화면에 「안전모·조끼 감시 중 · 교실 시험 기준 · 0.35m」 줄 아래에 「보호구 감시 멈춤」 카드가 함께 떠서 3D 화면 가운데를 가렸다. 「0.35m」는 뜻이 없었고, 「GPU 서버 보호구 감시 확인」은 화면에서 할 수 없는 일이었다.

관련 명세: 13.2(관제 머리줄), 7.2·7.4(R2·R3 높이 프로파일), 5.5(PPE). 계약·서버 변경 없음.

## 바꾼 것

| 위치 | 전 | 후 |
|---|---|---|
| `/live` 보호구 줄(`WorkPpeStatus`) | 「안전모·조끼 감시 중 · 교실 시험 기준 · 0.35m」, 그 아래 별도 카드 `PpeWorkNotice` 「보호구 감시 멈춤 / 보호구 상태가 들어오지 않아… / 현장 GPU 서버의 보호구 감시가 켜져 있는지 확인하세요.」 | 한 줄. 정상 「안전모·조끼 감시 중」. 15 s 넘게 PPE 상태가 없으면 같은 자리가 회색 「보호구 감시 꺼짐」. 원인·할 일·규칙 기준은 hover(title)와 「자세히」에만 둔다 |
| 규칙 기준 글 | 「교실 시험 기준 · 0.35m」 | 「교실 시험 기준 · 높이차 0.35m 이상을 고소로 판정」(자세히 안) |
| 꺼짐의 할 일 | 「현장 GPU 서버의 보호구 감시가 켜져 있는지 확인하세요」 | 「그동안 현장에서 직접 확인하세요. 계속 꺼져 있으면 GPU 관리자에게 알리세요.」 |
| 작업 전 확인 창 | 없음 | 경고 줄(시작은 막지 않음): 필수 보호구가 있고 Tier 0 는 이어졌는데 PPE 상태가 안 들어오면 「보호구 상태가 들어오지 않아 안전모·조끼 미착용 경보가 나지 않습니다. 현장에서 직접 확인하세요」. 연결된 워치 0대면 노트북 연결에 따라 한 줄 |
| 서명 줄 | 체크 칸처럼 생긴 네모(눌러지지 않음) | ✓(초록)/✗(빨강) 상태 아이콘 |

## 정한 수치·규칙과 근거

- 꺼짐 판정은 기존 값 그대로다: 수신 10 s(`PPE_RECEIPT_STALE_MS`) + 15 s 지속(`PPE_OFF_SUSTAIN_MS`, 옛 `PPE_NOTICE_SUSTAIN_MS`). 감시할 장비가 없으면(둘 다 끔) 수신을 묻지 않는다. 울릴 경보가 없으니 꺼짐을 알릴 까닭도 없다.
- 시작 전 PPE 경고는 Tier 0 연결이 `up` 일 때만 낸다. GPU 가 꺼졌거나 준비 중이면 GPU 확인 창(-702)이 맡고, 여기서 함께 알리면 중복이다. 수신을 모르면(`unknown`, 이전 EC2) 내지 않는다.
- 시작 전 확인은 그 순간 상태라 15 s 지속은 두지 않는다. 창이 열려 있을 때만 `GET /api/ppe/current` 를 5 s 마다 묻는다.
- 워치 경고: `viewer_health.watches.connected === 0` 일 때 한 줄. 노트북(현장 경보 단말, 명세 3장) `laptop.link` 가 끊겼으면 「현장에서 경보가 울리지 않습니다」, 이어졌으면 「현장 경보는 현장 노트북에서만 울립니다」, 모르면 워치만 말한다. 워치 수를 모르면(health 없음·이전 EC2) 내지 않는다.

## 쓴 서버 상태

`GET /api/ppe/current`(ppe_view 1.1.0 `last_received_at_ms`), `/topic/health`(`tier0_link`·`remote_state`·`laptop.link`·`watches.connected`). 모두 이미 있어 계약을 바꾸지 않았다.

## 버린 대안

- base-ui Popover 로 hover 창: 관제 알림 영역은 `pointer-events-none` 겹침 안이라 포털·초점 처리가 늘어난다. `title` + 「자세히」 펼침으로 충분하다.
- 워치와 노트북을 합쳐 「경보 단말 0대」 하나만: 노트북만 이어진 흔한 경우(워치 0대)를 놓친다.

## 코드·시험

- `front/apps/admin/src/features/monitor/WorkPpeStatus.tsx`·`workPpeText.ts`, `PpeWorkNotice.tsx` 삭제, `layout/AppLayout.tsx`
- `front/apps/admin/src/features/work/preflight/preflight.ts`(`startWarnings`)·`useStartWarnings.ts`·`PreflightConfirmDialog.tsx`·`PreflightStage.tsx`
- 시험: `WorkPpeStatus.test.tsx`, `preflight.test.ts`, `PreflightConfirmDialog.test.tsx`, `PreflightStage.test.tsx`

## 명세와 다른 점(보고)

명세 13장 36행은 화면에 `교실 시험 기준 · 0.35m` 안내를 "표시"한다고 적는다. 이 티켓에서는 그 안내를 hover·「자세히」로 옮기고 글을 「교실 시험 기준 · 높이차 0.35m 이상을 고소로 판정」으로 바꿨다. 명세 정리 MR 에서 반영할 일이다.

## 잔여 위험

- Tier 0 가 LIVE 가 아니면 PPE 는 보내지 않는다(ai/services/ppe/loop.py). 세팅 뒤 Tier 0 가 LIVE 가 아닌 채로 작업 전 확인을 열면 PPE 경고가 날 수 있다. 시작은 막지 않으므로 오해 정도의 위험이다.
