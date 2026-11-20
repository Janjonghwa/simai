# S15P31A101-832 3D 기본 시점: 남쪽에서 35°

관련 명세: 13장(관제 3D 뷰어), 4장(좌표 규약).

## 한 일

- `roomViewOf` 를 기존 앞·오른쪽 45° 대각선 고정 배수 방식에서 **World 남쪽(−Y) 정면, 수평 위 35°** 로 Scene 경계 상자 전체가 화면에 들어오는 투영 계산으로 바꿨다.
- 바라보는 점은 Scene 경계 상자 중심 `(cx, cy)` 와 높이 `min[2] + min(0.8 m, 높이 × 0.3)` 이다.
- 카메라 세로 시야각(`fovDeg`, 기본 50°)과 실제 Canvas 화면 비율(`size.width / size.height`)을 받아 경계 상자 8개 꼭짓점이 여유 배수(`FIT_MARGIN = 1.15`)를 두고 모두 시야 절두체 안에 들어오는 최소 거리를 계산한다.
- 모든 위치·방향 계산은 World(m, Z-up)에서 수행하고 마지막에 `worldToThree` 한 곳에서만 Three.js 좌표 `(x, z, −y)` 로 바꾼다(World −Y → Three +Z, `AGENTS.md` 규칙 3).
- `CameraRig` 가 `useThree((s) => s.size)` 와 초기 세로 시야각(`homeFov`) 및 실측 Scene 경계(`viewBoundsFrom`)로 기본 시점을 계산하므로, `<Viewer3D>` 를 쓰는 모든 화면(실시간 3D 관제·세팅 「현장 3D 맵」·작업 전 확인 「현장 3D 맵」·TBM 가상 레이아웃 편집)과 「시점 초기화」 단추에 일괄 적용된다.

## 코드·시험 위치

| 내용 | 위치 |
|---|---|
| 기본 시점·경계 순수 계산 (`roomViewOf`, `viewBoundsFrom`) | `front/apps/admin/src/features/viewer3d/scene/roomView.ts` |
| 시점 적용·초기화 (`CameraRig`) | `front/apps/admin/src/features/viewer3d/CameraRig.tsx` |
| Canvas 초기 카메라 시야각 공유 (`DEFAULT_VIEW_FOV_DEG`) | `front/apps/admin/src/features/viewer3d/ViewerCanvas.tsx` |
| 단위 시험 (남쪽·35°·8꼭짓점 절두체 맞춤·시점 초기화) | `front/apps/admin/src/__tests__/features/viewer3d/roomView.test.ts`, `worldScene.test.ts`, `cameraRig.test.tsx` |

## 명세에 없어 정한 수치

| 수치 | 값 | 근거 |
|---|---|---|
| 여유 배수 (`FIT_MARGIN`) | `1.15` | 8개 꼭짓점이 화면 가장자리에 붙지 않고 로비 정답 그림과 같은 여백을 남기며, 1440×900·1288×674 양쪽에서 방이 잘리지 않게 한다 |
| 초기 화면 비율 기본값 (`DEFAULT_VIEW_ASPECT`) | `16 / 10` (`1.6`) | Canvas 마운트 직전 모듈 상수 계산용. 1440×900 과 같고 더 넓은 1288×674 에서도 잘리지 않는 보수적 값 |
| 스캔 높이 이상치 상한 (`MAX_VIEW_HEIGHT_M`) | `50 m` | `roomLimit.ts` 의 `MAX_SCAN_SIDE_M` 과 동일. 튀는 점이 z 경계를 키운 스캔이면 높이만 기본 범위(0~5 m)로 되돌려 카메라가 멀리 날아가지 않게 한다 |

## 버린 대안

- `roomLimitFrom(bounds)` 그대로 사용: 상자 편집용 한계라 z 를 항상 `0~5 m` 로 덮어쓰므로 `min(0.8 m, 높이 × 0.3)` 이 실측 높이를 반영하지 못하고 낮은 방에서 카메라가 불필요하게 멀어진다. `viewBoundsFrom` 으로 실측 z 를 보존하되 50 m 초과 이상치만 걸렀다.
- 구(bounding sphere) 기반 거리 근사: 가로·세로 비율이 다른 방이나 1288×674 같은 넓은 화면에서 여백이 과도하게 커진다. 8개 꼭짓점의 카메라 축 투영으로 정확한 최소 거리를 구했다.

## 남은 위험

- 목업 빌드의 Scene 자산(`scene-demo.gltf`)은 실측 로비 GLB 와 크기·가구가 다르다. 실측 로비 GLB 경계(약 `7.9×8.4×2.8 m`)는 단위 시험의 `PerspectiveCamera` 투영으로 검증했으나, 실제 운영 로비 GLB 에서의 최종 구도는 현장 배포 후 한 번 더 눈으로 확인하는 편이 좋다.
- 실시간 3D 관제 화면의 좌하단 미니맵·우하단 떠 있는 CCTV 창은 Canvas 위에 겹쳐 뜨므로, 매우 넓은 방에서는 방 모서리 일부가 떠 있는 창 뒤로 지나갈 수 있다(방 자체는 Canvas 안에서 잘리지 않는다).
