# S15P31A101-821 관제 경보·3D 표시

- 근거: 명세 13.1·13.2·13.4, 2026-11-20 사용자 결정. 계약(`contracts/`)과 내부 심각도(`CRITICAL`·`HIGH`·`CAUTION`·`INFO`)·기존 기능(-817 CCTV 새 경보 따라가기, -819 낙상 경보음)은 유지한다.

## 바꾼 파일과 핵심 결정

- **경보 배너(`AlarmBanner.tsx`, `AckButton.tsx`)**
  - 순서: `SeverityBadge size="large"` · 사건(17px 굵게)/대상 두 줄 · 경과 시간(1초마다 갱신, `ec2Now`·`elapsedLabel` 기반) · `AckButton size="default"`.
  - 「상세」·「CCTV로 확인」·「확인 대기 외 N건」과 진입 예측 보조 줄을 뺐다.
  - `holdOnExit`(선택, 기본 false)는 닫힘 전환 150 ms 동안 내용을 유지할 때 쓴다.
- **오른쪽 이벤트 카드(`IncidentCard.tsx`)**
  - 세로 가운데 큰 `SeverityBadge` · 가운데 두 줄(가장 최근 경보 규칙 굵게 / 짧은 대상 이름) · 오른쪽 세로 가운데 `HH:MM:SS` 시각.
  - 묶인 카드 제목·배지·시각은 진행 중 묶음이면 진행 중 경보 중 최신(`raised_at` 최대), 모두 해소면 전체 중 최신을 따른다.
  - 진행 중 카드 테두리(`ring-1 ring-inset`)는 유지하되 `CRITICAL` 은 `sev-critical`(빨강), `HIGH`·`CAUTION` 은 `sev-caution`(주의 노랑) 하나로 통일했다.
- **CCTV 화면 배너(`CctvPage.tsx`)**
  - `pt-(--notices-h,0px)` 여백을 제거해 `/cctv` 에서 경보 배너가 영상 격자를 밀지 않고 위에 덮이도록 했다(`AppLayout` 은 -820 에서 `/cctv` 띠 밀기를 이미 제거함).
- **3D 사람 이름표(`LabelSprite.tsx`, `PersonLayer.tsx`)**
  - 사람 `LabelSprite` 높이를 `0.4 m` 로 키우고, 최소 화면 높이(`CAP_MIN_PX`)를 `22 px` → `30 px` 로 올리며 `useFrame` 스케일에 최소 `30 px` 하한을 적용했다.
  - 사람 칩은 배경과 구분되는 밝은 칩 바탕(`dark: rgba(30, 45, 72, 0.95)`, `light: rgba(255, 255, 255, 0.97)`), 테두리(`1 px`), 그림자(`shadowBlur: 4`), 아래쪽 삼각형 꼬리(`+12 px`)로 머리를 가리킨다. `palette.ts`(범위 밖)는 건드리지 않고 `LabelSprite` 안에서 사람 칩 바탕만 분기했다.
- **3D 구역 이름표·호버 연동(`store/zoneHover.store.ts`, `Selectable.tsx`, `ZoneLayer.tsx`, `ZoneList.tsx`, `ZoneListRow.tsx`)**
  - 실시간 관제 모드에서는 구역 이름표를 기본 숨기고, 호버(`useZoneHoverStore`)·선택·진행 중 경보(`risk`)가 있을 때만 표시한다.
  - `SpatialViewportHost.tsx`(-820 범위)를 건드리지 않고도 관제 모드만 정확히 가리도록 `MonitorZoneLabels` 컨텍스트와 뷰어 스토어의 관제 레이어 조건(`mode === 'LIVE' && layers.zones === 'interactive' && layers.workSurfaces === 'visible'`)을 함께 본다. 구역 편집 모드·세팅 구역 단계·작업 전 확인·가상 레이아웃 편집에서는 기존처럼 이름표가 모두 보인다.
  - 3D 구역 포인터 진입/이탈과 왼쪽 「위험 구역」 목록 행의 마우스 진입/이탈이 `useZoneHoverStore` 로 양방향 강조 연동된다. `ZoneListPanel` 언마운트 시 호버 상태를 비워 잔상을 막는다.
- **3D 카메라 이름표(`CameraLayer.tsx`)**
  - 기본 숨김, 마커에 마우스를 올렸을 때(`onPointerOver`~`onPointerOut`)만 `LabelSprite` 를 그린다.
- **기타 범위 정리(`DashPanel.tsx`, `ZoneActions.tsx`, `ZonesPage.tsx`, `plan/*`)**
  - `DashPanel` 에 선택 `className` prop 추가.
  - `ZoneActions`·`ZonesPage` 「구역 추가」 단추를 `size="action"` + `Plus` 아이콘으로 통일(`ZoneActions` 는 `variant="action"`, 접힌 `ZonesPage` 는 `variant="secondary"`).
  - 2D 평면(`HoverTip`, `PlanZones`, `planDraw`, `planTip`)의 `HIGH` 표시를 노랑(`sev-caution`)과 `!` 기호로 통일.

## 버린 대안

- `SpatialViewportHost.tsx` 에 `<MonitorZoneLabels value={!editing}>` 를 직접 감싸는 방법: -820(공통 틀) 범위 파일이라 동시 작업 충돌을 피하기 위해 범위 파일만으로 동작하는 `layers.workSurfaces === 'visible'` 판별 + `MonitorZoneLabels` 병행을 택했다.
- `palette.ts` 의 `label.background` 를 직접 바꾸는 방법: 범위 밖 파일이고 `palette.test.ts` 와 맞물려 있어 `LabelSprite` 내부의 사람 칩 바탕 분기로 처리했다.

## 남은 위험

- 관제 모드 판별이 `MONITOR_LAYERS`(`workSurfaces: 'visible'`)에 기대므로, 향후 관제 화면에서 작업면도 `interactive` 로 바꾸면 `SpatialViewportHost` 에 `<MonitorZoneLabels value={!editing}>` 를 감싸야 한다.
- `ZoneList`·`ZonesPage` 의 「주의」 표기는 -820 이 바꾼 `SEVERITY_LABEL.HIGH = '주의'` 를 따르며, 구역 편집 화면의 `CAUTION` → `HIGH` 재선택 시 두 선택지가 모두 「주의」로 보이는 점은 기존 저장 데이터 마이그레이션 이후 정리할 수 있다.
