# S15P31A101-861 낙상(R4) 소리는 같은 사람 묶음에 묶이지 않고 바로

-860 이 서버 워치 진동에서 R4 를 같은 사람 묶음(-723)에서 뺐다. 소리도 같은 기준으로 맞춘다(사용자 결정 2026-11-23).
관련: -497(소리 사다리), -723(같은 사람 동시 경보 묶음), -724(재생 중 소리 막음), -819(관제 화면 낙상 소리), -860(워치), 명세 3.10·7.3·7.4.

## 채널별 변경

| 채널 | 위치 | 이전 | 이번 |
|---|---|---|---|
| 노트북 현장 소리 | `ai/src/simai_ai/risk/sound_ladder.py` `SoundLadder.decide` | 같은 사람에게 열린 같거나 높은 등급(예: R1 CRITICAL)이 있으면 새 R4 도 `SILENT` | R4 는 **다른** 이벤트의 열린 경보에 막히지 않는다. 자기 이벤트 항목은 그대로 봐서 같은 R4 재판단은 `SILENT` |
| 관제 화면 소리 | `front/apps/admin/src/features/events/alarmPolicy.ts` | -819 에서 이미 R4 를 같은 사람 묶음에서 뺐다(새 낙상은 `restart` 로 바로 한 묶음 다시 울림) | 동작 그대로. 상수 주석에 이번 결정·-860 기준을 적고 묶임 유지·재발행 1회 시험을 더함 |

- 규칙 ID 는 각 쪽 한 곳: Tier 0 `IMMEDIATE_RULE = R4_RULE_ID`, front `FALL_RULE = 'R4'`.
- R4 는 묶는 쪽으로는 계속 참여한다. R4 가 열려 있으면 같은 사람의 R1·R5 등 같거나 낮은 등급은 그대로 `SILENT`(Tier 0 `active`, front `levels`).
- R4 가 울리면 Tier 0 `_local_alert` 가 같은 사람의 같거나 낮은 등급 열린 경보를 `SILENT` 로 돌린다(기존 동작). 노트북은 R4 패턴으로 계속 운다.
- 재생 중 소리 막음(-724)·LIVE 만 실제 알림·ACK 30 s·소리 뒤 60 s(CRITICAL 은 시간 억제 없음)는 바꾸지 않았다. 계약 변경 없음.

## 버린 대안

- R4 등급을 올려 묶음을 피하기: 이미 CRITICAL 이고, 등급은 Tier 0 정본이라 소리 때문에 바꾸지 않는다.
- 노트북 `gateway/site_alert` 에도 front 처럼 새 R4 때 패턴을 다시 시작: R1·R4 모두 CRITICAL 반복 패턴이라 소리는 끊기지 않고 이어진다. 범위 밖.

## 시험

- `ai/tests/risk/test_sound_ladder.py`: R1 소리 중 R4 → `REPEAT`(다른 R4 도), R4 중 R5·R1 → `SILENT`, 같은 R4 재판단 → `SILENT`.
- `front/apps/admin/src/__tests__/features/events/useAlarmSound.test.ts`: R1 중 R4 → `restart`(-819 기존), R4 중 R1·R5 → 같은 키·재시작 없음, 같은 R4 재발행 → 같은 키·재시작 없음.

## 잔여 위험

- 같은 사람에게 서로 다른 R4 이벤트가 동시에 열리면(ID 갈라짐) 각각 울린다(-860 과 같음). R4 는 드물고 급해서 받아들인다.
- 노트북은 같은 등급 패턴이 이미 울리는 중이면 새로 시작하지 않으므로, R1 CRITICAL 이 울리는 중 R4 가 와도 귀로는 같은 소리가 이어진다(R4 가 소리를 이어받아 R1 을 확인해도 멈추지 않는다). 필요하면 `gateway/site_alert` 후속 티켓.
