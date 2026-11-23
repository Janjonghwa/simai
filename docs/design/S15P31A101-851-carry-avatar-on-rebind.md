# 워치 배정이 옮겨 간 새 ID 에 아바타 외형 잇기와 경보 이름 유지 (S15P31A101-851)

관련 명세: 13.2 사람 표시·MHR 메시 아바타(`13-viewer.md`), 5.2 외형 누적과 관측·추정 구분(`05-processing.md`),
부록 C.6 워치 배정(`appendix-c-extensions.md`), 11.4-5 익명 World ID 와 표시 이름(`11-data.md`), AGENTS 규칙 1·7·8.
관련 설계: [`server-watch-match.md`](server-watch-match.md)(-793 자리 이어받기), [`S15P31A101-826-demo-watch-rebind.md`](S15P31A101-826-demo-watch-rebind.md),
[`S15P31A101-743-avatar-appearance-continuity.md`](S15P31A101-743-avatar-appearance-continuity.md), [`S15P31A101-742-avatar-fill-display.md`](S15P31A101-742-avatar-fill-display.md),
[`S15P31A101-835-replay-person-names.md`](S15P31A101-835-replay-person-names.md).

## 현상 (2026-11-23 리허설 녹화)

1. 14:14:36 KST 추창만이 넘어지는 순간 Tier 0 가 같은 사람을 새 트랙으로 갈랐다(옛 `0008` → 새 `0010`). Re-ID 가 `0010` 을 옛 ID `0007` 로 되살렸다
   (`tier0.log` `late_merge decision=linked subject=…-0010 best=…-0007`, 이 부활은 -848 이 막는 중). 3D 의 칠한 아바타가 사라지고 회색 관절 인형 「작업자 #6」이 됐다.
2. 14:14:51 워치 배정이 자리 이어받기(-793)로 새 트랙에 옮겨 와 이름은 「추창만」으로 돌아왔지만 외형은 회색 그대로였다.
3. 같은 때 앞서 난 낙상·진입 경보 카드(옛 `0008`)가 「작업자 #7」로 바뀌었다(서버 `person_names` 에서 `0008` 이 `UNIDENTIFIED 미확인 7` 이 됨).

아바타 서비스의 자체 이어받기(-743)는 기하·시간 조건(겹침 0.3 s 이하, 주변 1.5 m 혼잡 없음 등)이 하나라도 어긋나면 새 ID 를 처음부터 쌓는다.
넘어지는 순간처럼 옛 트랙이 OCCLUDED 로 몇 초 더 실리고 새 트랙이 겹쳐 생기면 조건을 못 맞춘다(`avatar.log` `carry={'no_donor': …}`, `inherited=0.0000`).
반면 서버 워치 대조는 "사라진 자리에 새 트랙 하나" 를 따로 판정해 배정을 옮긴다. 이 판정을 외형 표시에도 쓰는 것이 이 티켓이다.

## 정한 지점과 근거

### 1. 외형 이어받기: 뷰어 표시 계층(텍스처만)

| 단계 | 위치 | 내용 |
|---|---|---|
| 짝 받기 | `front/apps/admin/src/features/watch/api.ts` | `/api/watch-bindings` 응답의 `handoff_from`·`handoff_confirmed_at`(서버는 -793 부터 이미 싣는다)을 zod 스키마에 선택 필드로 더했다 |
| 짝 만들기 | `features/watch/watchStatus.ts` `nextAppearanceDonors`, `features/watch/useWatch.ts` `useAppearanceDonors` | 지금 배정 행에 `handoff_from` 이 있으면(임시·확정 모두) `새 world_id → handoff_from`. 같은 World ID 가 계속 배정돼 있으면 짝 유지(걸음 대조 승격으로 `handoff_from` 없는 새 행이 생겨도). 그 World ID 배정이 없어지면(반증 `MATCH_LOST`·해제·교체) 짝 삭제 → 이어받기도 되돌아간다 |
| 넘기기 | `features/layout/SpatialViewportHost.tsx` → `Viewer3D` `personAppearanceDonors` → `viewer3d.store` | 다른 호스트 속성(`personHelmets` 등)과 같은 방식. 내용이 같으면 다시 그리지 않는다 |
| 고르기 | `features/viewer3d/people/avatarAppearanceCarry.ts` `chooseAvatarAppearance` | 짝이 없거나 주는 ID 가 지금 world 에 다시 보이면(LOST 제외) 자기 외형. 주는 쪽 외형(기억 → 아직 남은 포즈 자산)이 있고 자기 외형이 없거나 `surface_ratios.OBSERVED` 가 더 작으면 주는 쪽 외형 |
| 기억 | 같은 파일 `appearanceMemoryOf`·`rememberAppearance` | 사람마다 지금 입힌 외형(이어받은 것 포함)을 데이터 저장소별로 기억한다(64명, Source 가 바뀌면 비움). 주는 쪽 포즈는 30초 뒤 지워지고(`AVATAR_FORGET_MS`) A → B → C 로 연달아 옮겨 가도 처음 외형이 이어진다 |
| 그리기 | `features/viewer3d/people/PersonLayer.tsx`, `avatarAppearance.ts` `useAvatarAppearance(…, getRequest)` | 고른 요청으로 텍스처를 받는다(같은 파일이면 캐시를 다시 쓴다). 메시 형상·포즈·위치·골격·`identity_revision`·출처는 모두 새 ID 의 것 그대로이고 바꾸는 것은 메시에 입히는 albedo·출처 지도뿐이다. 채움(-742)·대칭(-790)·`face_policy` 처리는 고른 외형에 그대로 적용된다 |

근거:
- **가장 작은 변경 지점.** 배정 이동은 서버(`WatchMotionMatcher.handoffVanished` → `WatchBindings.handoff`)에서 일어나고, 외형 누적은 GPU 아바타 서비스(`AvatarLoop`·`AppearanceCarry`)가 World ID 로 들고 있다.
  아바타 서비스는 서버 워치 배정을 받는 통로가 없다(Tier 0 공유 메모리만 읽는다). 서버 → 아바타 통로를 새로 만들면 계약·배포 단위가 늘고 GPU 세션 검증이 필요하다.
  뷰어는 이미 두 정보를 다 가진다: 배정은 3초마다 REST 로, 옛 ID 의 외형 파일 주소는 `avatar_frame` 자산으로. 그래서 뷰어 표시에서 잇는다.
- **MHR UV 는 사람마다 같다.** 텍스처는 MHR `texcoord_faces` 기준이라 다른 사람의 체형 메시에도 같은 자리에 맞는다(-743 아바타 서비스 이어받기도 같은 전제로 누적을 옮긴다).
- **관측·추정 구분(규칙 7)·Tier 0 정본(규칙 1).** `surface_source`·비율·계약·저장·위험 판정은 그대로다. World ID·위치·접지·골격은 Tier 0 값 그대로 그린다. 이어받기는 화면에 입히는 색뿐이다.
- **얼굴(규칙 8).** 얼굴을 만들지 않는다. 주는 쪽 텍스처의 관측 얼굴 텍셀과 `face_policy` 를 그대로 쓴다. 외형 색으로 신원을 정하지 않는다(짝은 서버 걸음·자리 판정에서만 온다).
- **덜 본 동안만.** 넘어진 사람처럼 새 ID 가 조금만 쌓였을 때는 옛 외형이 낫고, 새 ID 가 옛 ID 만큼 보면 자기 외형으로 넘어간다(관측 비율 비교, 같으면 자기 외형).
- **다시 보이면 안 함.** Re-ID 로 옛 ID 가 되살아나 둘이 같이 보이면 한 텍스처를 두 사람에 입히게 된다. 그때는 다른 사람으로 보고 잇지 않는다.

### 출처 표시: 계약 변경 없이 뷰어 내부

이어받은 외형이라는 표시는 `AvatarAppearanceChoice.carriedFrom`(뷰어 내부)과 브라우저 콘솔 기록
`[viewer3d] 아바타 외형 이어받기 <새 ID> ← <옛 ID>` 뿐이다. 계약(`avatar_frame`·`world_state`)은 바꾸지 않았다.
- 이어받기는 뷰어가 그 순간 고르는 표시 선택이라 저장·전송할 데이터가 없다. 데이터의 출처 기록(`surface_source`, `inherited` 비율)은 아바타 서비스가 따로 정확히 남긴다.
- -742 결정(화면에서는 관측과 채운 면을 구분하지 않음, 2026-11-15)과 같은 층의 표시 선택이다. 화면 토글은 만들지 않았다.

### 2. 경보 카드·배너 이름: 그 ID 가 마지막으로 가졌던 실제 이름

| 위치 | 내용 |
|---|---|
| `front/apps/admin/src/store/realtime.store.ts` `personNameMemory`·`rememberPersonNames` | `viewer_health.person_names` 를 받을 때 `WORLD_MANUAL`·`WATCH_DEVICE` 이름만 World ID 별로 덧붙여 기억한다. 자동 번호(`UNIDENTIFIED`)는 기억하지 않는다. 세션을 새로 시작하면(`begin`·`reset`) 비운다 |
| `features/people/personDisplay.ts` `withRememberedNames` | 지금 실제 이름이 없는(자동 번호·수신 전·맵에 없음) ID 는 기억 이름으로 둔다. 지금 실제 이름이 있으면 지금 이름(이름 변경은 따른다). 기억 이름에는 동명이인 괄호를 붙이지 않는다(같은 사람의 새 ID 와 이름이 같아서) |
| `features/people/usePersonDisplay.ts` `useEventPersonDisplayMap` | 경보 카드(`IncidentCard`)·배너(`AlarmBanner`, 확인 버튼 이름 포함)·사건 상세 머리(`EventDetailHeader`)·워치 격상 알림(`WatchEscalationNotice`)이 쓴다. 3D 이름표·2D 평면은 지금 이름 그대로다 |

근거:
- -835 지난 기록 방식(`GET /api/people/display-names` 시각 구간)은 날짜 단위 REST 조회라 실시간 카드가 새로 뜰 때마다 다시 묻게 된다(쿼리 기본 `staleTime` 0).
  관제 화면은 1 Hz health 로 이미 서버가 해석한 이름을 받으므로, 그것을 기억하는 편이 요청 없이 즉시 맞다.
- World ID 는 Tier 0 의 한 사람이고 이름이 바뀌는 경우는 배정이 옮겨 가거나(→ 자동 번호) 이름을 고칠 때뿐이다. 그래서 "그 ID 의 마지막 실제 이름" 이 곧 "경보가 난 시각의 이름" 이다.

## 명세에 없어 정한 값

| 항목 | 값 | 근거 |
|---|---|---|
| 짝 출처 | `watch_binding.handoff_from`(임시·확정) | 서버가 "사라진 자리 하나 · 새 트랙 하나 · 겹침 1 s 이하" 로 정한 것만 쓴다(-793). 모호하면 서버가 옮기지 않으므로 외형도 잇지 않는다 |
| 짝 유지 | 같은 World ID 가 배정돼 있는 동안 | 대조 승격(`MOTION_MATCH` 새 행)에서 `handoff_from` 이 빠져도 외형이 깜빡이지 않게 |
| 고르기 기준 | `surface_ratios.OBSERVED` 비교, 같으면 자기 외형 | 아바타 서비스가 버전마다 싣는 값이라 추가 계산이 없다 |
| 기억 크기 | 64명(가장 오래 갱신 안 된 사람부터 잊음) | 아바타 서비스 휴면 보관 수(`DEFAULT_MAX_DORMANT = 64`)와 같다. 텍스처 바이트는 들고 있지 않고 요청 주소만 든다 |

## 버린 대안

- **아바타 서비스의 외형 캐시 키 재지정.** 정확한 데이터 누적(관측 텍셀 합집합)이 되지만 아바타 서비스가 서버 워치 배정을 받을 통로(새 메시지·계약)가 필요하다.
  -743 의 기하 조건과 서버 배정 판정이 두 곳에 갈리고 GPU 세션 검증 전에는 시연에 못 쓴다.
- **-743 이어받기 조건 완화(겹침·혼잡 문턱).** 다른 사람 옷이 입혀지는 경우가 늘고, -848 이 Re-ID 쪽을 고치는 중이라 같은 `world_track` 경계에서 겹친다.
- **옛 ID 메시·마지막 자세를 새 ID 자리에 그리기.** 새 ID 의 HMR 포즈가 없을 때(넘어져 회색 관절 인형일 때) 옛 자세를 그리면 서 있는 사람처럼 보인다. Tier 0 골격과 어긋나는 지어낸 자세라 하지 않았다.
- **경보 이름에 -835 시각 구간 API 사용.** 위 근거대로 실시간 카드에는 요청이 잦다. 새로 고침 직후처럼 기억이 없는 경우의 보강으로는 넘긴 일로 둔다.

## 남은 위험 (실 재생 검증 필요)

- **실 재생으로 아직 확인하지 않았다.** 11-23 녹화 재생으로 다음을 본다: 배정 이동(14:14:51 근처) 뒤 새 ID 메시에 옛 외형이 입혀지는지, 콘솔의 `아바타 외형 이어받기` 줄, 반증 시 되돌아감.
- **새 ID 에 메시가 없으면 효과가 없다.** 11-23 화면의 회색은 관절 인형(메시 아님)이었다. 넘어진 사람은 ID 불확실·포즈 없음으로 메시가 꺼질 수 있고, 이 티켓은 메시가 있는 동안의 텍스처만 잇는다. Re-ID 부활(-848)이 고쳐지고 새 ID 의 첫 메시가 구워진 뒤에 외형이 붙는다.
- **배정 반영 지연.** 배정 조회는 3초 간격이라 배정 이동 뒤 최대 3초는 회색일 수 있다.
- **틀린 이어받기.** 서버가 다른 사람에게 배정을 옮기면(임시) 그 사람에게 옛 외형이 입혀진다. 반증(`MATCH_LOST`)·교체로 배정이 빠지면 바로 돌아가고 위험 판정에는 영향이 없다.
- **경보 이름 새로 고침.** 기억은 브라우저 세션 안에서만 쌓인다. 배정이 옮겨 간 뒤에 화면을 새로 열면 앞선 경보는 여전히 「작업자 #N」이다(넘긴 일: -835 시각 구간 API 를 한 곳에서 한 번 불러 보강).

## 넘긴 일

- 11-23 녹화 재생으로 위 남은 위험 1·2 확인.
- 경보 이름의 새로 고침 보강(-835 시각 구간 API, 한 곳에서 `staleTime` 을 두고 조회).
- 명세 정리 MR: 13.2 「MHR 메시 아바타 구동」 끝에 "워치 배정이 자리 이어받기로 옮겨 간 새 ID 는 자기 외형이 없거나 덜 본 동안 옛 ID 외형을 입힌다(표시 전용, -851)" 한 줄.
