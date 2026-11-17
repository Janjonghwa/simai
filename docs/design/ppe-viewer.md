# PPE 관제 화면 표시 설계

S15P31A101-531. 기획서 §8.10(관제 화면에 World ID 별 안전모·조끼 상태와 근거 시각), §12.3(`ppe_state` 화면용 현재 상태), §12.4(`/topic/ppe`), §13.3(PPE 표현), §19.1(전달 토픽 미결)이 기준이다. 명세 v1.3 수정 69, 계약 v7.22.0(`messages/ppe_view` 1.0.0).

앞 단계: PPE 프로세스(-507) → Tier 0 I1 `ppe_state` → EC2 저장·현재 상태(-516, [`ppe-state-ingest.md`](ppe-state-ingest.md)). 이 이슈는 EC2 현재 상태를 관제 화면까지 잇는다. R9 판정(-519)과는 따로이며, 이 표시는 경보가 아니다(§7.3).

## 흐름

```
I1 ppe_state ─▶ PpeStateIngestor ─(현재 상태가 바뀌었고 커밋됨)─▶ PpeStateStored
                                                                  │
                    PpeViewRelay(한 스레드, Tier 0 마다 최신만) ◀───┘
                        │  PpeViewRepository.snapshot(source_id)  (ppe_current_state, 만료 반영)
                        ▼
            STOMP /topic/ppe  ppe_view 1.0.0  ──▶ 관제 앱 stomp.ts(계약 검사) ─▶ realtime.store.ppe[tier0_source_id]
GET /api/ppe/current(연결마다 1회, 같은 모양) ──▶ usePpeSnapshot ───────────────┘
                                                                  │
                    ppeView.personPpe(같은 World ID·identity_revision, 만료·연결)
                        ├─ 3D 미착용 경고 표식(usePpeGearStates → viewer3d personHelmets·personVests, 형상은 -783 에서 뺌)
                        └─ 글 표시용 훅 usePersonPpe(안전모·조끼·사유·마지막 근거 시각). 화면에 그리는 일은 프론트 디자인(아래 넘긴 일)
```

## 코드와 시험

| 부분 | 코드 | 시험 |
|---|---|---|
| 계약 | `contracts/messages/ppe_view.schema.json`, 예시 `ppe_view`·`.expired`·`.no_people`, 반례 5개 | `validate_examples.py`, server `ContractExamplesRoundTripTest` |
| EC2 발행 | `server/.../domain/ppe/PpeStateStored`·`PpeViewRepository`·`PpeViewRelay`, `PpeStateIngestor`(이벤트), `RealtimeTopics.PPE` | `PpeViewIntegrationTest`(실제 I1 → STOMP 계약 검사, REST 만료·60초 창·401) |
| EC2 REST | `PpeViewController`(`GET /api/ppe/current`, VIEWER·FOREMAN·ADMIN) | 같은 시험 |
| 관제 앱 수신 | `front/apps/admin/src/lib/stomp.ts`(`PPE_TOPIC`, 3D Source 로 넘기지 않음), `store/realtime.store.ts`(`ppe`, `receivePpe`) | `__tests__/lib/stomp.test.ts`, `__tests__/features/ppe/ppeView.test.ts` |
| 표시 규칙 | `features/ppe/ppeView.ts`, `usePpe.ts`, `api.ts` | `ppeView.test.ts`, `AppLayout.test.tsx`(처음 상태 훅) |
| 3D 표식 | `features/viewer3d/people/ppe/PpeWarningMark.tsx`, `PersonLayer.tsx`, prop `personHelmets`·`personVests`(CONTRACT.md·CHANGELOG.md). 형상(`PpeGear`·`ppeFit`·`ppeAssets`, GLB 2개)은 -783 에서 지웠다 | `__tests__/features/viewer3d/personPpe.test.tsx`(장면 렌더 포함) |
| 개발 mock | `front/apps/admin/src/mocks/scenario/ppe.ts`(`ppeViewAt`: mock 사람 셋을 안전모 착용·미착용·모름으로), `mocks/realtime.ts`(`/topic/ppe` 2 Hz, `GET /api/ppe/current`) | `__tests__/mocks/realtimeScenario.test.ts`(계약), `__tests__/mocks/realtime.test.ts` |

## 명세에 없어 정한 것과 근거

| 항목 | 값 | 근거 |
|---|---|---|
| 전달 방식 | 새 토픽 `/topic/ppe` + 새 I2 메시지 `ppe_view`, 처음 상태는 같은 모양의 REST | §12.3 이 EC2 의 "화면용 현재 상태"를 따로 두라고 한다. REST 와 토픽이 같은 모양이면 앱 처리가 하나다 |
| 메시지 단위 | Tier 0 마다 사람 전체 스냅샷 한 장 | 스냅샷이면 사라진 사람이 화면에 남지 않는다(차분이면 지우는 규칙이 따로 필요) |
| 보내는 때 | 현재 상태가 하나라도 바뀐 커밋 뒤. 밀리면 Tier 0 마다 한 번만(보낼 때 DB 에서 다시 만든다) | PPE 는 1~2 Hz 라 바뀔 때만 보내도 충분하다. 과거·만료 메시지만 온 경우는 화면이 바뀌지 않아 보내지 않는다 |
| 스냅샷 범위 | 촬영 시각이 스냅샷 시각 − 60초 이후, 최신 촬영 순 최대 64명 | 60초 넘게 갱신이 없는 사람은 world_state 에서 빠졌을 가능성이 크다. 64 는 계약 `people` 상한(ppe_state 와 같음) |
| 만료 반영 | 착용·미착용인데 `expires_at` 이 지났으면(또는 없으면) `state` UNKNOWN·`reason` EVIDENCE_EXPIRED, `reported_state` 는 보낸 값 | §13.3 "근거가 만료되면 UNKNOWN". 보낸 값을 남겨 진단할 수 있게 한다. EC2 는 UNKNOWN 을 착용·미착용으로 바꾸지 않는다(스키마로 막음) |
| 내부 값 | 근거 프레임 키·local ID·모델 판·유효 칸 수·지원 비율은 싣지 않는다 | 화면 표시에 쓰지 않고, 브라우저로 내보낼 이유가 없다. 이력은 `ppe_state` 표에 남는다 |
| 화면 쪽 만료 | 지금 EC2 시각 = `snapshot_at_ms` + (지금 − 받은 시각). 이 값이 `expires_at_ms` 를 넘으면 UNKNOWN | PPE 프로세스가 멈추면 새 스냅샷이 오지 않으므로 화면이 스스로 만료를 본다. 1초마다 다시 본다 |
| EC2 끊김 | `isLinkAlive`(health 3초) 가 거짓이면 모든 사람 UNKNOWN(`EC2_DISCONNECTED`, 화면 전용 사유) | §13.3 "EC2 연결이 끊기면 UNKNOWN" |
| 붙이는 조건 | 지금 world 트랙(LOST 제외)과 World ID·`identity_revision` 이 같을 때만 | §13.3 "이전 ID 의 상태를 새 사람에게 넘기지 않는다". 차수가 다르면 아무것도 붙이지 않는다(UNKNOWN 도 아님) |
| 글 표시 | 이 MR 에서는 그리지 않는다. 데이터(`usePersonPpe`: 상태·사유·마지막 근거 시각, 만료·연결 반영)까지만 둔다. → -656 에서 착용이 아닐 때만 이름표 뒤 글(`usePpeLabels`·`ppeLabel`, 예: `안전모 미착용 · 조끼 모름(30초 전)`)로 넣었다 | 관제 화면 디자인을 프론트가 정한다(이원준 결정 2026-11-09). 처음 구현했던 이름표 글·보호구 카드는 뺐다 |
| 3D 형상 | 안전모·조끼 GLB(-552). 골격 막대는 양쪽 귀 가운데에 안전모, 어깨·골반 관절로 조끼 크기를 맞춘다. 캡슐·아바타 메시 사람은 캡슐 몸의 고정 자리. WORN 은 채도를 낮춘 실물, UNKNOWN·NOT_WORN 은 그 장비를 그리지 않는다(-656: 반투명 0.3 + 외곽선은 착용과 구분되지 않았다. 착용이 아닐 때는 이름표 글로 알린다). 안전모·조끼 중 하나라도 NOT_WORN 이면 머리 위 빨간 표식 하나(골격 머리 공 위 0.07+0.22 m, 캡슐 1.64 m, 높이 모름 2.0 m). 높이 모름 기둥은 표식만 | §13.3 표현과 크기 맞춤(어깨 폭·몸통 길이). 아바타 메시 머리 정밀 맞춤은 하지 않고 캡슐 자리에 둔다(19.1 미결) |

- **S15P31A101-783 변경**: 3D 안전모·조끼 형상을 그리지 않는다. 캡슐 고정 자리에 붙어 아바타 몸과 어긋나 목업처럼 보인다는 사용자 결정(2026-11-17). 미착용 경고 표식과 이름표 PPE 칸만 남는다(위 표의 「3D 형상」 행 중 형상 부분은 지난 기록).

## 버린 대안

- `/topic/world` 에 PPE 를 싣는다: `/topic/world` 는 I1 `world_state` 를 형식 그대로 다시 싣는 규칙(§12.4)이고 I1 LIVE 계약은 Lead 승인 대상이다.
- I1 `ppe_state` 를 그대로 중계한다: 처음 상태 REST 와 모양이 달라지고, 사라진 사람 정리·만료·근거 내부 값을 앱이 따로 다뤄야 한다.
- 주기적(1초) 스냅샷: 바뀔 때만 보내고 화면이 만료를 스스로 보면 같은 결과다. 사람이 없을 때 빈 메시지를 계속 보낼 이유가 없다.
- 조끼도 3D 모델로 그린다: -531 에서는 뺐으나 조끼 미착용도 같은 경고 표식으로 보이기로 해 -552 에서 GLB 로 더했다(몸통 맞춤은 골격 어깨·골반, 그 밖은 캡슐 고정 자리).
- 이름표 글(워치 글 뒤 `· 안전모 착용`)·고른 사람 보호구 카드: 처음 구현했으나 관제 화면 디자인을 프론트가 정하기로 해 뺐다(데이터 훅만 남김).

## 잔여 위험과 넘긴 일

- 범위 조회 색인: `ppe_current_state (observation_at, source_id)` 를 V45 에서 더했다(S15P31A101-550). 표는 지난 사람을 지우지 않아 색인 없이는 발행·재연결마다 전체를 훑는다.
- 시계: 만료는 Tier 0 시계(`expires_at_ms`)와 EC2 시계(`snapshot_at_ms`)를 같은 UTC 로 본다([`ppe-state-ingest.md`](ppe-state-ingest.md) 잔여 위험과 같다).
- 3D 형상은 jsdom 장면 시험으로 위치·이름·표식 규칙을 본다. 실제 GPU 에서의 모양은 개발 화면(`?ppe=fixed|cycle`)과 mock 으로 눈으로 본다.
- 골격 머리 위치는 표본 기준이고 루트 보간과 조금 어긋날 수 있다(캡슐·메시 아바타일 때). 골격 아바타는 같은 그룹에 둬서 어긋나지 않는다.
- 아바타 메시 머리 정밀 맞춤은 §19.1 에 남긴다(메시 사람은 캡슐 자리에 장비를 둔다).
- **넘긴 일(프론트 디자인)**: §8.10·§13.3 의 "상태와 마지막 근거 시각을 보인다" 글 표시. → -656 에서 이름표 글로 처리(착용이 아닐 때만). `usePersonPpe(worldId)` 가 같은 World ID·차수·만료·연결을 따진 값을 준다. 사유 코드는 계약 `ppe_state` reason 목록과 화면 전용 `EC2_DISCONNECTED` 이다.
- 리플레이에 PPE 상태를 싣는지는 명세 8.5 에 없다(넘김).
- 요구사항 FR-74 갱신 필요(명세 README 요구사항 갱신 목록).
