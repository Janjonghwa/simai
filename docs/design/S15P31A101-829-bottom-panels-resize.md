# S15P31A101-829 관제 아래 미니맵·CCTV 창 크기 조절

## 관련 명세·범위
- 2026-11-20 사용자 요청에 따라 실시간 3D 관제 아래쪽 두 창(왼쪽 아래 2D 미니맵, 오른쪽 아래 떠 있는 CCTV 창)에 화면 안쪽 모서리 비율 고정 크기 조절 손잡이를 붙였다.
- 범위: `features/layout/useAspectResize.ts`, `features/plan/PlanMinimap.tsx`, `features/cctv/CctvFloat.tsx`, `hooks/useFloatSpot.ts` 및 각 단위 시험. `-828` 과의 충돌을 피하기 위해 `SpatialViewportHost.tsx` 와 `features/events/*` 는 고치지 않았다.

## 정한 수치와 근거
- **최소 크기(기본 크기)**: 미니맵은 `--minimap-size` (`min(260px, 34svh)`), CCTV 창은 `max(280px, round(기본 미니맵 크기 × 영상 비율))`. 손잡이를 최소 크기까지 줄이거나 두 번 누르면 저장 값을 지우고 CSS 기본 크기로 돌아간다.
- **최대 크기**: 두 창 모두 높이를 화면 짧은 변의 절반(`floor(min(w, h) / 2)`)까지 허용하고(1440×900 에서 높이 450px·CCTV 16:9 폭 800px, 1288×674 에서 높이 337px·CCTV 16:9 폭 599px), 상대 창·이벤트 패널·작업 표시줄(상단 알림 자식 요소)·도구 모음과 14px(`FLOAT_MARGIN.side`) 간격을 두도록 상한을 줄인다.
- **이벤트 패널·왼쪽 칼럼과의 공존**:
  - CCTV 창은 `useReportCctvFloatBox` 로 놓은 뒤 자리를 알리면 이벤트 패널(`useAvoidCctv`)이 최소 높이(120px)까지 자동으로 줄어 비켜 서므로, CCTV 장애물 계산에서는 이벤트 패널의 최소 높이 구간(120px)만 막는다.
  - 미니맵이 커지면 같은 `<main>` 부모에 `--side-column-bottom` 인라인 변수를 갱신해 왼쪽 위험 구역 목록 칼럼이 미니맵 위로 올라가 겹치지 않게 한다(`--minimap-size` 는 건드리지 않아 CCTV 기본 폭이 따라 흔들리지 않는다).
- **저장 키**: 미니맵은 `simai.plan.minimap.size.v1`, CCTV 는 기존 `simai.cctv.float.v2`(`videoWidth`)를 그대로 쓴다. 읽기·쓰기는 모두 `try/catch` 로 감싸 실패해도 기본 크기로 정상 동작한다.

## 버린 대안
- `:root` 의 `--minimap-size` 를 직접 덮어쓰는 안은 CCTV 기본 폭(`calc(var(--minimap-size) * aspect)`)이 미니맵 크기에 연동돼 버려 버렸다.
- CCTV 창의 브라우저 기본 `resize: 'horizontal'` 은 오른쪽 아래(화면 바깥쪽) 모서리에 고정돼 화면 안쪽 조작이 불가능하므로 제거하고, 끌기 표면(`role="group"`) 밖의 형제 요소로 왼쪽 위 모서리 손잡이(`cursor-nwse-resize`)를 뒀다.

## 남은 위험
- `collectBottomPanelObstacles` 는 `aside[aria-label="이벤트"]`, `[role="region"][aria-label="알림"]` 등 접근성 이름과 `data-testid="plan-minimap"` 으로 주변 상자를 찾는다. `-828` 등 후속 변경에서 해당 라벨이 바뀌면 선택자를 함께 맞춰야 한다.
- CCTV 창을 이미 왼쪽으로 멀리 끌어 옮긴 뒤 미니맵을 크게 키우면 미니맵이 CCTV 앞에서 멈추지만, 반대로 사용자가 CCTV 창을 미니맵 위로 직접 끌어 올려 겹쳐 놓는 위치 이동 자체는 `useFloatSpot` 의 기존 동작대로 막지 않는다.
