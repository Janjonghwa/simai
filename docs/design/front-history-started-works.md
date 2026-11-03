# 히스토리가 시작된 작업만 보이고 작업 다시 보기에서 3D·2D 맵을 걷어 낸다 (S15P31A101-433)

히스토리 목록과 작업 다시 보기는 서버 `GET /api/work-units?days=7` 의 작업을 그대로 보여서, 아직 시작하지 않은 작업(오늘 오후·앞으로 잡힌 작업)까지 목록에 떴다. 서버는 `ends_at >= now - days` 로 거를 뿐이다
([server-work-units.md](server-work-units.md)). mock 만 시작 전 작업을 빼고 있어서 시험도 이 문제를 잡지 못했다.
작업 다시 보기 가운데 칸에는 CCTV 목 화면 위에 숨겨 둔 3D 창(`SHOW_3D = false`)과 오른쪽 아래 2D 평면 창이 있었다. 팀 결정(2026-11-03)으로
3D·2D 맵은 두지 않고 CCTV 화면만 남긴다.

## 관련 명세

- 8.5 기록·최소 리플레이, 요구사항 FR-28·29. 명세 8.5 는 시각 T 의 상태를 "Three.js 에 재생"한다고 쓴다. **명세와 다름:** 작업 다시 보기에서는 3D·2D 맵을 지운다
  (사용자 요구, 2026-11-03 회의). 구간 기록 API(`GET /api/replay`)와 "데이터 없음"(보간하지 않는다) 규칙은 그대로다. 명세 정리는 별도 MR 이 한다.

## 코드·시험 위치

모두 `front/apps/admin/src` 아래.

| 위치 | 내용 |
|---|---|
| `features/history/works.ts` | `startedWorks(works, now)`: `from_ms <= now` 인 작업만, 같은 객체로 돌려준다 |
| `features/history/constants.ts` | `STARTED_RECHECK_MS`(1분): 목록을 다시 거르는 간격 |
| `features/history/useWorkReplay.ts` | `useWorks` 가 받은 목록에 `useNow(STARTED_RECHECK_MS)` 로 `startedWorks` 를 씌운다. 재생 훅에서 구역(`zonesRaw`)·`source`·이벤트 표시(`useReplayMarks`)를 걷어 냈다 |
| `features/history/ReplayStage.tsx`, `WorkReplayPage.tsx` | 가운데 칸은 CCTV 목 화면만 둔다. 3D 창·2D 평면 창·작은 창 크게/작게 보기·선택을 3D 로 넘기던 코드를 지웠다 |
| `features/history/replayIndex.ts` | 2D 평면 표시용 `markableAt`·`marksOf` 를 지웠다 |
| `features/history/zonedSource.ts` | 재생 Source 에 구역을 넣던 `withZones` 를 지웠다(3D 를 위한 것이었다) |
| `features/plan/plan.ts` | `PERSON_KEY_PREFIX` 를 파일 안에서만 쓴다(히스토리가 마지막 사용처였다) |
| `mocks/history.ts` | `historyWorks` 가 서버처럼 시작 전 작업도 준다 |
| `__tests__/features/history/` | `replay.test.ts`(`startedWorks` 경계), `HistoryListPage.test.tsx`(시작 전 작업이 없고 시각이 지나면 1분 안에 나타난다), `WorkReplayPage.test.tsx`(시작 전 작업 주소는 찾을 수 없다, 3D·2D 창이 없다), `zonedSource.test.ts` 삭제 |
| `__tests__/mocks/history.test.ts` | mock 이 시작 전 작업도 주는지 |

## 정한 값과 근거 (명세에 없음)

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 시작한 작업의 기준 | 계획 시작 시각 `from_ms <= 지금`(임시) | 서버 `work_unit` 에 상태·시작 시각·시작 API 가 없다. 실제 작업 시작 기록은 다음 작업에서 만든다(실시간 관제 화면의 작업 목록 패널의 "작업 시작" 버튼). 그때 `startedWorks` 한 곳과 다시 보기 구간의 시작점만 바꾸면 되게 판정을 한 함수에 모았다 |
| 시계 | 브라우저 `Date.now()` | TBM 화면이 같은 브라우저 시각으로 `from_ms` 를 만든다. 이 화면에는 현장 시각 보정(`ec2Now`)이 필요한 곳이 없다 |
| 다시 거르는 간격 | `STARTED_RECHECK_MS` = 1분 | 작업 시작 시각이 분 단위라 1분이면 화면에 늦어도 1분 안에 나타난다. `useNow` 가 분 경계에 맞춰 바꿔 서로 다른 작업이 같은 순간에 함께 나타난다 |
| 거르는 자리 | 목록과 다시 보기가 같은 `useWorks` | 두 화면이 같은 기준을 써서, 시작 전 작업 주소로 바로 들어오면 기존 "작업을 찾을 수 없습니다."가 나온다. 작업 하나를 받는 API 가 없어 다시 보기도 목록에서 작업을 찾는다 |
| 작업 객체 | 거른 뒤에도 같은 객체 | 1분마다 다시 걸러도 `useWorkReplay` 의 구간 기록·재생 Source 가 다시 만들어지지 않는다 |
| 재생 엔진 | 남긴다 | CCTV 와 타임라인의 시계(`createReplaySource`)이다. 3D 입구(`features/viewer3d`)는 동적 import 로 부르는 채로 둔다(CONVENTION) |

## 버린 대안

- **서버가 거른다(`starts_at <= now`)**: `server/` 는 곽나혁 님 몫이고, 같은 API 를 쓰는 다른 화면에도 영향이 간다. 진짜 시작 기록이 생기면 서버가 거르는 쪽이 맞다.
- **TBM 보고서 행으로 판정**: 계획 단계에서도 만들 수 있고(레이아웃만 있으면), 작업마다 `GET /api/reports/tbm?work_id=` 한 번씩 부른다.
- **`event_counts > 0`·구간 기록 유무**: 이벤트가 0 건이어도 작업은 시작했을 수 있고, 작업마다 요청이 든다.
- **3D·2D 를 켜는 스위치로 남기기**: 쓰지 않는 코드와 시험이 남는다. 되살리려면 git 기록에 있다.

## 잔여 위험

- 계획 시작 시각이 지났지만 실제로 시작하지 않은 작업도 목록에 뜬다(임시 기준의 한계). 다음 작업의 시작 기록이 이 한계를 없앤다.
- 노트북 시계가 현장 시각과 크게 어긋나면 작업이 일찍/늦게 나타난다.

## 넘긴 일

- **실제 작업 시작 기록**: 실시간 관제 화면의 작업 목록 패널에 "작업 시작" 버튼을 두고 서버 `work_unit` 에 시작 시각을 남긴다. 그러면 히스토리는 시작한 때부터의 CCTV 를 타임라인으로 보인다. `startedWorks` 와 다시 보기 구간의 시작점(`from_ms` → 시작 시각)을 그때 바꾼다.
- **3D 뷰어 REPLAY 모드**: 히스토리가 마지막 사용처였다. 지금은 `features/viewer3d` 의 개발 하네스와 자체 시험만 쓴다. 뷰어 기능이라 이 MR 에서는 두었다.
- **VLM 바운더리박스 CCTV**: VLM 근거 입력 이미지에 위험 구역·대상자를 덧그리는 것(`ai/src/simai_ai/vlm/overlay.py`, S15P31A101-420)은 VLM 호출에만 쓰고 저장하지 않는다. `vlm_rationale` 결과 계약에도 이미지 칸이 없고 조회 API 도 없다. 대상자는 박스가 아니라 접지점 원이다. 이미지 저장·조회가 생기면 다시 보기의 CCTV 를 그 이미지로 바꾼다. 지금은 CCTV 목 화면(`CctvMockWall`)을 둔다.
