# S15P31A101-661 World ID 병합 뒤 소비자 상태 승계

관련 명세: 5.2(ID 교정 기록·병합·분할), 5.5(PPE 상태 전환), 6.2(PPE 상태), 3.2·20.4(EXP-02 아바타, 교정 직후 외형), 13.2·13.3(뷰어 사람 표시·인원). 계약 변경 없음(I1 `world_state` 의 `aliases`·`split_from`·`identity_revision`·`identity_confidence`·`track_lifecycle` 만 읽는다). 관련: -657(!607, 둘 다 살아 있는 중복 트랙을 더 자주 병합, 미머지), -653(PPE ID 불확실 건너뛰기), -650(아바타 Tier 0 부하 기준).

## 문제

World Track Manager 가 같은 사람의 두 ID 를 합치면(MERGE) 남는 ID 의 `identity_revision` 이 오르고 사라지는 ID 가 `aliases` 뒤에 붙는다. 소비자는 지금 차수 상승을 모두 "다른 사람일 수 있음"으로 보고 처음부터 다시 쌓는다.

- PPE: 창 초기화(`IDENTITY_REVISION_CHANGED`), 흡수 ID 의 local ID 를 남는 ID 가 이어받으면 다시 초기화(`IDENTITY_MAPPING_CHANGED`). 3초 이상 다시 쌓아야 WORN/NOT_WORN 이 나온다.
- 아바타: 안정 수·표본을 비우고 자산 차수 불일치로 다시 굽는다(초기 메시 5 프레임, 체형 확정 30 프레임).
- 뷰어: 선택·초점 사람이 장면에서 빠지면 선택을 비운다. CCTV 창 카메라도 그대로 남는다.
- 인원수: TENTATIVE 중복 ID 까지 세어 병합 전후로 출렁인다.

-657 이 병합을 자주 일으키므로 병합 한 번마다 이 비용이 생긴다. 병합은 "같은 사람이었다"는 교정이라 남는 ID 의 상태를 이어 가도 된다.

## 순수 병합 판정 (`ai/src/simai_ai/tracking/identity_carry.py`)

소비자가 앞서 본 같은 World ID 의 (identity_revision, aliases, split_from) 과 이번 값을 비교해 아래를 모두 채우면 순수 병합이다.

1. `identity_revision` 이 올랐다.
2. 앞서 본 `aliases` 가 그대로 앞에 남아 있고(빠지거나 순서가 바뀌지 않음) 새 alias 가 하나 이상 붙었다.
3. `split_from` 이 그대로다(새로 생기거나 바뀌지 않음).
4. 같은 `world_state` 에 `split_from` 이 이 ID 이고 `identity_revision` 이 이번 값과 같은 Track 이 없다(같은 시각 분할로 갈라져 나간 N. 분할은 W·N 의 차수를 같은 값으로 올린다, `split_marks`).

하나라도 어긋나면 지금처럼 처음부터 다시 쌓는다. 그래서 분할(원래 ID 는 별칭 변화 없이 차수만 오름), swap 되돌리기(별칭 없이 두 ID 차수 상승), 병합과 분할이 같은 차수로 겹친 경우, 알 수 없는 교정은 모두 초기화다. 흡수된 ID 는 새로 붙은 alias 들이다.

`identity_confidence` 는 판정에 넣지 않았다(지시와 다름, 아래 '지시와 다른 점'). 남는 Track 은 병합한 시각부터 자기 관측 3 프레임 + 1 s 동안 늘 `IDENTITY_UNCERTAIN(LATE_REASSOCIATION)` 이라(world_track `_merge` 의 `confirm_left`, `uncertain_hold_ms`) 불확실을 거르면 순수 병합이 하나도 생기지 않는다. 불확실 구간은 각 소비자의 기존 규칙이 맡는다(PPE: 불확실 칸 건너뛰기·3초 초과 초기화, 아바타: 누적 일시정지, 뷰어: 1.5 s 메시 유지 뒤 캡슐).

## 소비자별 승계

| 소비자 | 순수 병합 | 분할·그 밖의 차수 상승 |
|---|---|---|
| PPE (`ppe/state.py`) | 남는 ID 의 창·상태 유지. 흡수 ID 의 칸을 넘겨받고 흡수 ID 상태는 지움. 흡수 ID 가 쓰던 local ID 의 주인 기록을 남는 ID 로 옮김(주인 변경 초기화 면제). 흡수 ID 의 늦은 관측은 `RETIRED_WORLD_ID` 로 버림(30 s 기억) | 지금처럼 `IDENTITY_REVISION_CHANGED` 초기화 |
| 아바타 (`hmr/stream.py`) | 표본·자산·굽는 중인 메시 유지, 다시 굽지 않음. 안정 수만 0(30 프레임 다시 안정될 때까지 외형 누적·체형 확정 멈춤, 명세 20.4). 흡수 ID 상태 지움 | 지금처럼 안정 수·표본 비우고 새 차수로 다시 굽기 |
| 뷰어 메시 (`avatarMeshPolicy.ts`) | 차수가 다르면 지금처럼 캡슐(워커가 새 차수 포즈를 보내면 맞춰짐) | 같음 |
| 뷰어 선택·초점 (`viewer3d.store.ts`) | 빠지는 사람 ID 가 생존 트랙 별칭이면 선택·초점을 생존 트랙으로. 별칭이 늦으면 500 ms 기다림 | 분할 새 ID 로는 옮기지 않음(별칭이 아님) |
| CCTV (`CctvFloat.tsx`) | 고른 사람의 지금 World ID 가 바뀌면 그 사람 기준으로 카메라 다시 고름 | — |

### PPE 칸 이관 규칙

- 남는 ID 의 장비가 **비어 있으면**(UNKNOWN, 유효 칸 결과 없음, 아직 닫지 않은 칸 표본 없음) 흡수 ID 의 장비 상태를 통째로 옮긴다(상태·사유·창 시작·칸 결과·대기 표본·근거·만료 기준).
- 두 쪽 모두 확정이고 **서로 반대**면(WORN ↔ NOT_WORN) UNKNOWN(`CROSS_CAMERA_CONFLICT`)으로 두고 이번 칸부터 다시 쌓는다(반대 근거와 같은 처리).
- 그 밖에는 남는 ID 의 상태를 두고 칸별로 합친다. 남는 ID 의 창 시작 이전 칸은 넣지 않는다. 같은 칸: 같으면 그대로, 한쪽이 없거나 빈 칸이면 다른 쪽, 엇갈리면 `CONFLICT` 가 있으면 `CONFLICT`, 아니면 `UNKNOWN`. 대기 표본은 카메라별로 합치고 같은 카메라가 엇갈리면 둘 다 둬서 칸 결합이 충돌로 본다.
- 처음 보는 남는 ID(예: 소실됐던 옛 ID 가 새 ID 를 흡수)는 aliases 중 PPE 가 쌓던 ID 를 같은 규칙으로 넘겨받는다(분할 표시가 있으면 안 함).
- `policy_version` `ppe-policy-0.3.0` → `ppe-policy-0.4.0`. 계측 `PpeJudge.identity_events`(`revision_reset`·`merge_carried`)를 서비스 진단(`diagnostics()["identity"]`)에 싣는다.

입력 경로: Tier 0 `_share_avatar` 는 트랙마다 `world_state` 트랙 문서를 그대로 펼쳐(`{**track, ...}`) 공유 문서에 싣므로 `aliases`·`split_from` 은 이미 들어 있다. Tier 0 쪽 변경은 없고, PPE 서비스(`services/ppe/loop.py`)가 사람 관측(`PersonObservation.aliases`·`split_from`·`split_source`)에 옮겨 싣는다.

### 아바타 자산 차수

이미 보낸 자산 메시지는 바꾸지 않는다. 계약(avatar_asset `appearance_version`: "같은 버전을 다른 내용으로 다시 보내지 않는다")과 규칙 6 때문에 "자산의 identity_revision 만 새 값으로 갱신"을 메시지 수정으로 하지 않고, 워커 내부에서 `PersonState.revision_base`(마지막으로 처음부터 다시 쌓기 시작한 차수)를 두어 `자산 차수 ≥ revision_base` 면 지금 사람의 자산으로 본다(`asset_current`). 병합 뒤 포즈는 새 차수, 자산은 옛 차수(자산 차수 < 포즈 차수)이고 이는 `docs/design/avatar-pose-stream.md` 'ID 교정 뒤 외형'(병합: 남은 ID 가 교정 전 자산을 계속 가리켜도 된다)과 EC2 규칙(자산 차수 > 포즈 차수만 null)에 맞다. 다음 새 버전(텍스처 등)부터 새 차수로 적는다. 외형 누적기(`AppearancePipeline`)도 `revision_base` 를 기준 차수로 받아 병합에서 색 누적을 비우지 않는다.

### 뷰어 메시 짧은 불확실 유지

`AVATAR_IDENTITY_HOLD_MS = 1500`. 같은 `identity_revision` 으로 `IDENTITY_UNCERTAIN` 이 이어진 World 시간(데이터 Store 가 앞 프레임과 비교해 `WorldFrame.identityUncertainSince` 에 붙임)이 1.5 s 안이면 메시를 유지한다. 근거: Tier 0 는 불확실 사유가 한 번 생기면 1.0 s(`uncertain_hold_ms`) 유지하고, 병합·재연결 뒤 일관성 확인은 자기 관측 3 프레임(0.3 s) + 1.0 s 라 짧은 불확실은 대개 1.3 s 안에 끝난다. 그동안 워커는 새 포즈를 보내지 않으므로(불확실이면 추론 건너뜀) 끊김(1.0 s)·늦음(1.0 s) 기준도 그만큼 늘려 마지막 자세로 둔다. 위치·접지는 늘 Tier 0 값이다. 이어진 시간을 모르면(첫 프레임부터 불확실한 Store 밖 호출 등) 지금처럼 캡슐이다. PersonLayer 는 고치지 않았다(-658 과 겹침 회피).

### 선택·초점 승계

- 별칭 표(흡수 ID → 생존 ID, `aliasOwners`)는 `ViewerShell` 한 곳에서 데이터 Store 의 World 가 바뀔 때마다 뷰어 Store 에 넘긴다. zustand 구독은 같은 set 안에서 불리므로 흡수 ID 의 장면 대상이 React 반영으로 빠지기 전에 표가 먼저 바뀐다(보통 즉시 승계).
- 별칭이 아직 없으면 선택·초점을 비우고 `MERGE_CARRY_GRACE_MS = 500`(10 Hz 몇 프레임) 안에 별칭이 오거나 생존 트랙이 등록되면 옮긴다. 그사이 다른 대상을 고르거나 선택을 풀면 옮기지 않는다.
- 호스트 선택 키는 뷰어 `onSelectionChange` 로 따라오고, CCTV 창은 그 키가 바뀌면 다시 고른다. 3D 가 없거나 키가 흡수 ID 로 남아 있어도 CCTV 는 `currentPersonId`(world_id 또는 aliases)로 지금 ID 가 바뀐 순간 다시 고른다(처음 나타남·사라짐에는 반응하지 않아 카메라 마커로 고른 카메라를 덮지 않는다).

### 인원수

`selectTrackedCount` 는 `CONFIRMED`·`OCCLUDED` 만 센다(TENTATIVE·LOST 제외). 쓰는 곳: 이벤트 패널 머리줄 "작업자 N명", 작업 상태 배지의 "사람이 보이면 작업 시작 안내". TENTATIVE 는 확정 횟수(3회) 전이라 첫 0.3 s 남짓 늦게 세지만 중복 ID 출렁임을 막는다.

## 지시와 다른 점

- 순수 병합 조건의 "identity_confidence 가 불확실이 아님"은 넣지 않았다. 위 판정 절의 이유(병합 직후 늘 불확실). 불확실 구간은 소비자별 기존 규칙이 맡는다.
- 아바타 "자산의 identity_revision 만 새 값으로 갱신"은 보낸 자산을 고치지 않고 워커 내부 기준(`revision_base`)으로 했다. 아바타 안정 수는 0 으로 돌렸다(명세 20.4 교정 직후 누적 멈춤). 표본·자산은 유지.

## 명세와 다른 점 (명세 반영 요청)

- 5.5 상태 전환: "상태를 바로 초기화하는 것은 `identity_revision` 이 오를 때(분할·swap 교정 포함)" → "순수 병합(aliases 추가, 분할 아님)은 남는 ID 의 상태를 잇고 흡수 ID 칸을 넘겨받는다. 그 밖의 차수 상승(분할·swap 교정 포함)은 초기화"로 고쳐야 한다. "World ID 대응이 바뀔 때"에도 "흡수 ID → 남는 ID 이어받기 제외"를 더한다.
- 13.2·13.3: 같은 차수의 1.5 s 이내 `IDENTITY_UNCERTAIN` 은 메시 유지, 인원수는 CONFIRMED·OCCLUDED 기준, 선택 승계.

## 버린 대안

- **PPE 를 병합 때 흡수 ID 쪽 상태로 늘 덮기**: 남는 ID 가 더 오래 쌓은 근거를 버린다. 비어 있을 때만 옮긴다.
- **반대 확정 상태 중 하나 고르기(최근 근거 우선)**: 둘 중 누구 근거가 맞는지 모른다. UNKNOWN 으로 다시 쌓는다(모르면 비운다).
- **Tier 0 가 병합 이벤트를 따로 내보내기**: I1 `world_state` 계약 변경이 필요하고(Lead 승인), 기존 필드로 판정할 수 있다.
- **아바타 병합 때 새 자산 버전을 같은 GLB 로 다시 알리기**: 다른 자산 키는 파일 주소를 같이 쓰지 않는 규칙이라 다시 올려야 하고, 설계가 이미 자산 차수 < 포즈 차수를 허용한다.
- **뷰어가 사라진 ID 를 선택으로 계속 쥐기(유예 동안)**: 장면에 없는 대상을 선택으로 두면 호스트·CCTV 가 없는 사람을 본다. 비우고 승계만 기다린다.
- **호스트(`useViewerSelection`)에서 승계**: 3D Selectable 해제와의 순서를 알 수 없다. 뷰어 Store 가 해제 시점에 바로 판단하고 호스트는 선택 변경 알림으로 따라온다.

## 남은 위험

- 병합 전 중복 쌍은 `POSSIBLE_DUPLICATE` 로 매 시각 불확실이라, 병합까지 3 s 넘게 걸리면 PPE 는 병합 전에 이미 불확실 3초 초과로 초기화된다(이 티켓 밖, -657 이 병합을 빠르게 하면 줄어든다).
- 병합 직후 남는 ID 의 불확실(약 1.3 s)이 다른 사유와 이어져 3 s 를 넘으면 PPE 는 지금처럼 초기화된다.
- 뷰어 메시: 병합은 차수가 바뀌므로 워커가 새 차수 포즈를 보낼 때까지(불확실 해제 + HMR 지연) 캡슐이다. 짧은 불확실 유지는 차수가 그대로인 경우에만 돕는다.
- 오병합(다른 두 사람을 합침): 이전에는 차수 상승으로 초기화돼 숨었지만 이제 PPE·아바타가 한 사람 상태를 이어 쓴다. 반대 확정은 UNKNOWN 으로 막지만, 한쪽만 확정이면 남는 쪽 상태가 이어진다. Tier 0 병합 기준(2대 로컬 트랙 3회, 도달 거리, 병합 금지 쌍)이 1차 방어다. 재생에서 오병합 사례를 따로 본다.
- 인원수: 확정 전 짧게 보인 진짜 새 사람은 0.3 s 남짓 늦게 센다.

## GPU 녹화 재생에서 볼 것

같은 녹화를 이 MR 전후로 재생해 비교한다(-657 머지 뒤 한 번 더).

- PPE: `diagnostics()["identity"]` 의 `merge_carried` / (`merge_carried` + `revision_reset`) = 병합 승계율, 병합 직후 5 s 안 `IDENTITY_REVISION_CHANGED`·`IDENTITY_MAPPING_CHANGED` 사유 비율(병합 직후 PPE 리셋률), 병합 전후 WORN/NOT_WORN 유지 시간, `RETIRED_WORLD_ID` 버림 수.
- 아바타: `identity_events` 의 `merge_asset_kept` / `merge_carried`(병합 뒤 자산 유지율), 병합 뒤 업로드 수(다시 굽기 0 이어야 함), 메시 → 캡슐 전환 횟수와 길이(같은 차수 짧은 불확실 동안 유지되는지).
- 뷰어: 병합 순간 선택 사람이 생존 트랙으로 옮겨지는지, CCTV 카메라가 그 사람을 따라가는지, 인원수 출렁임(이전 대비).
- 오병합 의심: 병합 뒤 PPE 상태가 반대로 바뀐 사례(`CROSS_CAMERA_CONFLICT` 직후), 병합된 두 ID 의 병합 전 골반 거리.

## 시험

- ai: `tests/tracking/test_identity_carry.py`(판정), `tests/ppe/test_association_state.py`(순수 병합 유지·빈 생존 트랙 이관·처음 보는 생존 트랙 이관·local ID 이어받기 면제·반대 확정 UNKNOWN·칸 합치기·분할 초기화·같은 차수 분할 겹침 초기화·split_from 변화 초기화·병합 뒤 짧은/긴 불확실), `tests/ppe/test_service_loop.py`(관측에 aliases·분할 표시), `tests/hmr/test_avatar_loop.py`(병합 자산 유지·다시 굽지 않음·분할/별칭 없는 상승 다시 굽기·흡수 상태 정리), `tests/hmr/test_appearance.py`(누적 유지·새 버전 새 차수).
- front: `viewer3d.store.test.ts`(즉시·늦은 승계, 유예 만료, 다른 선택 우선, 생존 트랙 늦은 등록), `identityCarry.test.ts`, `avatarMesh.test.tsx`(1.5 s 유지·초과·차수 다름), `spatialData.store.test.ts`(불확실 시작 시각), `CctvFloat.test.tsx`(병합 뒤 카메라 다시 고르기), `realtime.store.test.ts`·`EventPanel.test.tsx`(인원수).
