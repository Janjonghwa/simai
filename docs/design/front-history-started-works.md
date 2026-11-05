# 히스토리가 시작된 작업만 보이고 작업 다시 보기에서 3D·2D 맵을 걷어 낸다 (S15P31A101-433, 실제 시작 기록 기준은 S15P31A101-486)

히스토리 목록과 작업 다시 보기는 서버 `GET /api/work-units?days=7` 의 작업을 그대로 보여서, 아직 시작하지 않은 작업(오늘 오후·앞으로 잡힌 작업)까지 목록에 떴다. 서버는 진행 중인 작업과 `COALESCE(actual_ended_at, ends_at) >= now - days` 인 작업을 시작 전 작업까지 모두 준다
([server-work-units.md](server-work-units.md)). mock 만 시작 전 작업을 빼고 있어서 시험도 이 문제를 잡지 못했다.
작업 다시 보기 가운데 칸에는 CCTV 목 화면 위에 숨겨 둔 3D 창(`SHOW_3D = false`)과 오른쪽 아래 2D 평면 창이 있었다. 팀 결정(2026-11-03)으로
3D·2D 맵은 두지 않고 CCTV 화면만 남긴다.

처음(-433)에는 서버에 시작 기록이 없어 계획 시작 시각이 지난 작업을 시작한 것으로 보았다(임시 기준). 서버에 작업 시작·종료(`POST /api/work-units/{id}/start|end`, `work_unit.status`·`actual_started_at`·`actual_ended_at`)가 생기고
실시간 관제의 오늘 작업 목록이 그 버튼을 들게 되어(-486), 히스토리도 실제 시작 기록을 기준으로 바꿨다.

## 관련 명세

- 8.5 기록·최소 리플레이, 요구사항 FR-28·29. 명세 8.5 는 시각 T 의 상태를 "Three.js 에 재생"한다고 쓴다. **명세와 다름:** 작업 다시 보기에서는 3D·2D 맵을 지운다
  (사용자 요구, 2026-11-03 회의). 구간 기록 API(`GET /api/replay`)와 "데이터 없음"(보간하지 않는다) 규칙은 그대로다. 명세 정리는 별도 MR 이 한다.

## 코드·시험 위치

모두 `front/apps/admin/src` 아래.

| 위치 | 내용 |
|---|---|
| `features/history/types.ts` | 작업의 `actual_started_at`·`actual_ended_at` 를 읽는다(서버는 epoch ms 숫자 또는 null). 시작 기록이 곧 상태라 `status` 는 읽지 않는다 |
| `features/history/works.ts` | `startedWorks(works)`: `actual_started_at` 이 있는 작업(진행 중·끝난 작업)만, 같은 객체로 돌려준다. `replayRange(work, now)`: 실제 시작~실제 종료(진행 중이면 now)이고 길이가 없으면 null. `workTimeRange`: 목록·머리글의 실제 시간대("09:02–11:48", 진행 중은 "09:02–진행 중") |
| `features/history/useWorkReplay.ts` | `useWorks` 는 받은 목록을 `select` 에서 `startedWorks` 로 거른다. `useWorkReplay` 는 `replayRange` 로 구간을 정하고 `WorkReplay.range` 로 돌려준다 |
| `features/history/api.ts` | `replayKey(workId, range)`: 구간이 키에 들어 있어, 진행 중이던 작업이 끝나면 끝난 구간으로 다시 받는다 |
| `features/history/ReplayStage.tsx`, `WorkReplayPage.tsx` | 가운데 칸은 CCTV 목 화면만 둔다. 구간이 없으면(방금 시작함) "아직 다시 볼 구간이 없습니다." 한 줄만 둔다 |
| `features/tbm/useWorkSession.ts` | 시작·종료 뒤 `['history','works']` 도 다시 받게 한다 |
| `mocks/history.ts` | 작업에 `status`·`actual_*` 가 있다. 오늘 작업은 `liveWorks`(개발 서버에서는 TBM 목의 작업)를 따라 시작·종료가 따라오고, 시험에서는 계획 시각을 따르는 데모 작업이다 |
| `__tests__/features/history/` | `replay.test.ts`(`startedWorks`·`replayRange`·`workTimeRange`), `HistoryListPage.test.tsx`(시작 기록이 없는 작업은 목록에 없고 시작하면 나타난다), `WorkReplayPage.test.tsx`(진행 중·끝난 작업의 구간, 구간이 없는 작업, 시작 전 작업 주소) |
| `__tests__/mocks/history.test.ts` | 작업의 상태·실제 시작·종료, `liveWorks` 를 따르는 오늘 작업 |

## 정한 값과 근거 (명세에 없음)

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 시작한 작업의 기준 | 서버 시작 기록 `actual_started_at` 이 있는 작업 | DB 제약이 진행 중·끝난 작업에는 시작 시각이 있고 예정 작업에는 없게 한다. 클라이언트가 시각을 비교하지 않으므로 시계가 어긋나도 작업이 일찍·늦게 나타나지 않는다 |
| 다시 볼 구간 | 실제 시작~실제 종료, 진행 중이면 그 작업이 화면에 처음 보인 때(EC2 시계 `ec2Now`)까지 | 시작 시각이 EC2 시계라 같은 시계로 끝을 맞춘다. 그 때로 고정해 재생 데이터 키가 매분 바뀌지 않는다. 화면을 연 뒤에 시작한 작업도 시작 뒤가 구간이 된다(목록에 처음 보인 때가 끝). 끝나면 끝난 구간으로 다시 받는다 |
| 구간이 없는 작업 | `to <= from` 이면 기록을 받지 않고 한 줄로 알린다 | 서버가 `from < to` 를 요구한다(400 `INVALID_REPLAY_QUERY`). 방금 시작했거나 시작과 같은 시각에 끝난 작업이 이 경우다 |
| 거르는 자리 | 목록과 다시 보기가 같은 `useWorks` | 두 화면이 같은 기준을 써서, 시작 전 작업 주소로 바로 들어오면 기존 "작업을 찾을 수 없습니다."가 나온다. 작업 하나를 받는 API 가 없어 다시 보기도 목록에서 작업을 찾는다 |
| 재생 데이터 키 | `['history','replay',작업,from,to]`, `staleTime: Infinity` | 같은 구간은 다시 받지 않고, 구간이 바뀌면(진행 중 → 끝남) 새로 받는다 |
| 재생 엔진 | 남긴다 | CCTV 와 타임라인의 시계(`createReplaySource`)이다. 3D 입구(`features/viewer3d`)는 동적 import 로 부르는 채로 둔다(CONVENTION) |

## 버린 대안

- **서버가 거른다(`actual_started_at` 이 있는 작업만)**: `server/` 는 곽나혁 님 몫이고, 같은 API 를 쓰는 작업 관리·관제 목록은 예정 작업도 받아야 한다. 히스토리 전용 필터가 필요해지면 서버에 요청한다.
- **TBM 보고서 행으로 판정**: 계획 단계에서도 만들 수 있고(레이아웃만 있으면), 작업마다 `GET /api/reports/tbm?work_id=` 한 번씩 부른다.
- **`event_counts > 0`·구간 기록 유무**: 이벤트가 0 건이어도 작업은 시작했을 수 있고, 작업마다 요청이 든다.
- **진행 중 작업의 끝을 매분 늘리기**: 재생 데이터 키가 바뀌어 기록을 다시 받고 재생이 처음으로 돌아간다. 작업이 처음 보인 때로 고정한다.
- **3D·2D 를 켜는 스위치로 남기기**: 쓰지 않는 코드와 시험이 남는다. 되살리려면 git 기록에 있다.

## 잔여 위험

- 작업을 시작하기 전에 기록된 Tier 0 데이터는 다시 보기에 나오지 않는다(구간이 실제 시작부터다). 시작 직전 상황이 필요하면 계획 시각까지 넓히는 방법이 있으나 지금은 쓰지 않는다.
- 진행 중인 작업을 연 채로 두면 구간은 그 작업이 처음 보인 때까지다. 이후 기록은 다시 열면 보인다.

## 넘긴 일

- **3D 뷰어 REPLAY 모드**: 히스토리가 마지막 사용처였다. 지금은 `features/viewer3d` 의 개발 하네스와 자체 시험만 쓴다. 뷰어 기능이라 이 MR 에서는 두었다.
- **VLM 바운더리박스 CCTV**: VLM 근거 입력 이미지에 위험 구역·대상자를 덧그리는 것(`ai/src/simai_ai/vlm/overlay.py`, S15P31A101-420)은 VLM 호출에만 쓰고 저장하지 않는다. `vlm_rationale` 결과 계약에도 이미지 칸이 없고 조회 API 도 없다. 대상자는 박스가 아니라 접지점 원이다. 이미지 저장·조회가 생기면 다시 보기의 CCTV 를 그 이미지로 바꾼다. 지금은 CCTV 목 화면(`CctvMockWall`)을 둔다.
