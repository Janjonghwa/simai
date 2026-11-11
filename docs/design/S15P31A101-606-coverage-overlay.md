# S15P31A101-606 관제 화면 바닥 감시 범위 겹침

관련 명세: 4.4(보정 상태·회전 자동 복구 `rotfix`, 4.4-4 시야 이탈 감시 저하), 4.5(보정 버전과 정적 Scene 계보), 5.2(위치는 2대 이상에서 삼각측량), 13장(뷰어 장면 구성·상태 표시, 13.6 2D 비상 화면). 앞 티켓: `calibration/coverage.py`(설치 안내 기하 커버리지), `docs/design/S15P31A101-593-setup-bake-images.md`(세팅 산출물 등록 경로).

## 문제

정적 3D 맵은 휴대폰 보충 촬영(-605)으로 카메라가 못 보는 곳까지 채워진다. 관제자가 "맵이 보이는 곳 = 감시되는 곳"으로 오해하면 안 된다(코치 지적: 건설 현장에서는 사각이 가장 중요하다). 그래서 관제 화면에 바닥 칸마다 실제로 몇 대가 그 자리에 선 사람을 보는지(2대 이상 = 위치 삼각측량 가능 / 1대만 / 사각)를 표시한다. Visual 층 표시이며 Tier 0 판정에 들어가지 않는다.

## 한 일

- **바닥 영역**(`ai/src/simai_ai/calibration/floor_coverage.py` `floor_mask_from_points`): 세팅 MapAnything 점구름을 `world_alignment.json` 의 `mapanything_to_world` 로 World 로 옮기고, |Z| ≤ 5 cm 점으로 10 cm 점유 격자 → 2회 닫기(틈 잇기) → 4-연결 가장 큰 영역 하나 → 안쪽 구멍 메우기. 1 m² 미만·점 없음이면 None(모르면 비운다, AGENTS 규칙 2).
- **커버리지**(`floor_coverage_record`): 10 cm 칸 3×3 = 30 cm 칸으로 묶고(9 칸 중 5 칸 이상 바닥이면 그 칸을 바닥으로), 칸 중심마다 `coverage.floor_camera_counts` 로 카메라 수를 센다. `floor_camera_counts` 는 `coverage_guidance` 루프에서 뽑아낸 `_person_visibility`(발목·머리 높이 투영, 왜곡 포함 영상 경계)를 그대로 쓴다. 두 번째 구현을 두지 않았다.
- **어디서 만드나**(방식 A): 세팅 (g) 미리보기 단계(`reconstruct_tasks.preview` → `write_floor_coverage`)가 같은 점구름·작업 폴더 `calibration.json`(`publish` 가 미리보기 전에 새 보정 문서로 바꿔 둔다)으로 `floor_coverage.json` 을 쓴다. 실패해도 미리보기는 그대로다(예외는 경고만). 단계 어댑터(`site_setup_stages.preview`)가 읽어 계약 검사(스키마 + 칸 수)를 통과한 것만 `PreviewDraftResult.floor_coverage` 로 넘기고, 처리기(`site_setup.py`)가 등록 보정 버전과 같을 때만 미리보기 스냅샷부터 결과까지 `artifacts.floor_coverage` 로 싣는다. 스냅샷 전체가 계약 검사를 받으므로(어기면 INTERNAL_CONTRACT_ERROR) 미리 걸러 표시용 자료가 세팅을 멈추지 않게 했다.
- **계약 v7.30.0**(마이너): `site_setup` 1.9.0 `$defs/floor_coverage`, `artifacts.floor_coverage`, progress 에 floor_coverage 가 있으면 `calibration_version` 필수(`job_callback` 2.16.0·`job_heartbeat` 2.8.0). 예시 `job_callback.site_setup`, 반례 `job_heartbeat.site_setup_bad_floor_cells`·`job_heartbeat.site_setup_floor_coverage_without_calibration`. Python 모델 재생성. TS 생성 대상(STOMP 메시지)이 아니라 front 재생성 없음.
- **서버**: `V52__floor_coverage.sql`(보정 버전 PK, 추가만), `SetupArtifactRegistrar.registerFloorCoverage`(스냅샷 보정 버전 일치·이 현장 보정·칸 수 확인, `ON CONFLICT DO NOTHING`), `SceneService.current()` 가 Scene `calibration_version` 의 행을 선택 `floor_coverage` 로 싣는다(원문 그대로, 읽지 못하면 생략).
- **front**: `lib/floorCoverage.ts`(읽기·칸 묶음·지금 범위 판정, three.js 없음) → 3D `viewer3d/scene/FloorCoverageLayer.tsx`(`floorCoverage` prop, 바닥 위 3 cm 반투명, 클릭 안 받음, 꼭짓점은 `worldToThree`), 2D `plan/PlanCoverage.tsx`(구역 아래, `toPlan`), 관제 도구 모음 「감시 범위」 버튼과 범례(`CoverageLegend`, 종류별 넓이 m²). 뷰어 `CONTRACT.md`·`CHANGELOG.md` 갱신.

## 데이터 모양

```json
"floor_coverage": {
  "calibration_version": "cal-setup-12",
  "cell_m": 0.3,
  "origin_xy": [0.4, 0.3],
  "cols": 6, "rows": 4,
  "cells": "..1122012222012222.11222",
  "ankle_height_m": 0.1, "head_height_m": 1.7,
  "floor_source": "MAPANYTHING_FLOOR_POINTS",
  "is_upper_bound": true
}
```

칸 (r, c) 는 `[x0 + c·cell_m, x0 + (c+1)·cell_m) × [y0 + r·cell_m, y0 + (r+1)·cell_m)`, `cells[r·cols + c]`. 교실 10 × 8 m 는 약 900 자(1 KB 안팎), 상한 40,000 자(60 × 60 m).

## 명세에 없어 정한 값

| 값 | 정한 것 | 근거 |
|---|---|---|
| 바닥 점 높이 띠 | \|Z\| ≤ 0.05 m | World 바닥 평면 허용 오차 `WorldFrameConfig.plane_tol_m` 3 cm + 정렬 잔차 여유. 책상 윗면(0.7 m 이상)·의자 좌판은 빠진다 |
| 점유 격자 | 10 cm, 점 1 개 이상 | 발 크기보다 작다. 2.4 m 높이 카메라에서 10 m 떨어진 바닥은 영상 한 줄이 바닥 10 cm 남짓이라 2 점을 요구하면 먼 바닥이 끊긴다. 외톨이 점은 가장 큰 영역 고르기에서 빠진다 |
| 닫기 | 3×3 구조 2회(약 40 cm 틈까지) | 먼 바닥 줄 간격·작은 가림을 잇는다. 바깥 경계를 늘리지 않게 둘레를 비우고 닫는다 |
| 구멍 | 메운다 | 책상 밑·기둥 뒤처럼 방 안인데 바닥 점이 안 잡힌 곳이다. 비워 두면 사각이 "모름"으로 숨는다. 메우면 기하 상한으로 평가된다(사각이면 사각으로 보인다) |
| 바깥 | 늘리지 않는다 | 어느 카메라에도 바닥으로 안 보인 곳은 바닥인지 모른다(규칙 2). -605 보충 점구름이 들어오면 다시 계산해 늘릴 수 있다(넘긴 일) |
| 최소 넓이 | 1 m² | 그보다 작으면 바닥 추출 실패로 보고 비운다 |
| 커버리지 칸 | 30 cm(바닥 칸 3×3, 과반 5/9) | 사람 어깨 폭(약 45 cm)보다 작아 사각 경계가 보이고, 응답이 1 KB 안팎이다. `coverage_guidance` 의 0.5 m 는 안내 문구용이라 표시에는 거칠다 |
| 사람 높이 | 발목 0.1 m · 머리 1.7 m | `CoverageConfig` 기본값 그대로(설치 안내와 같은 기준). 문서에 함께 싣는다 |
| 2대 기준 | `min_cameras` 2 고정 | 명세 5.2 삼각측량. 문자 `2` 의 뜻을 고정하려고 다른 값은 거부한다 |
| 3D 띄움 | 바닥 위 3 cm, 깊이 쓰기 끔·polygonOffset | 재구성 바닥 요철에 묻히지 않을 만큼만. 사람·구역 피킹과 가림에 영향이 없다 |
| 색 | 2대 이상 초록 `#4fc98a`, 1대 파랑 `#6886d5`, 사각 보라 `#b04fd6` | 빨강·주황·노랑은 위험 등급 전용(front/CONVENTION「화면」). 사각을 가장 짙게(불투명도 0.5) |
| 기본 표시 | 꺼짐 | 관제 화면의 1차 신호는 사람·구역·경보다. 바닥 전체 색면을 늘 깔면 구역 채움·경보 강조와 겹쳐 읽기 어렵다. 관제자가 사각을 볼 때 한 번 눌러 켠다. 편집 중에는 숨긴다 |
| 지금 범위 판정 | 센 보정 = world_state 보정, 또는 Scene 보정 = 센 보정이고 서버 Scene 정합(viewer_health.scene.compatibility)이 같은 지금 보정을 VERIFIED 로 판정 | 범위는 보정에 따라 바뀐다. 다시 계산하는 대신 "오래됨"으로 숨긴다(가장 단순한 맞는 규칙). 다만 관제 중 `rotfix`(명세 4.4)는 카메라 중심을 고정하고 버전만 바꾸므로 VERIFIED(중심 10 cm·회전 2° 이하) 동안은 그대로 보인다. world 가 없으면(보정 모름) 그리지 않는다 |
| 저장 | 보정 버전마다 한 행, 먼저 등록한 것 유지 | 범위는 보정 + 바닥 영역에만 달려 같은 보정의 다른 Scene(정식·-605 보충)에도 맞다. 재시도 대여의 점구름 차이로 세팅을 거부하지 않으려고 덮어쓰지도 거부하지도 않는다(규칙 6) |
| Flyway | V52 | V49 는 -602, V50 은 -597, V51 은 -609(TBM 양식, 처음 V49 로 겹쳐 옮김). 처음엔 V51 이었으나 -609 핫픽스(!556)에 V51 을 내주고 V52 로 옮겼다 |

## 버린 대안

- **방식 B(front 가 `map_cameras` 로 직접 계산)**: 바닥 영역을 front 까지 나르는 길(계약·서버 저장)은 A 와 똑같이 필요하고, `map_cameras` 에는 왜곡 계수가 없어 `coverage.py` 와 다른 두 번째 구현이 된다. A 가 더 작고 정확하다.
- **바닥 다각형 외곽선 추출 뒤 `coverage_guidance(polygon)`**: 외곽선 추적·단순화를 따로 만들어야 하고, 구멍 처리와 칸 격자가 다시 다각형 안팎 판정으로 돌아온다. 점유 격자를 그대로 칸 마스크로 쓰는 편이 같은 결과를 더 짧게 낸다. 그래서 `coverage.py` 에서 투영·경계 판정만 공용 함수로 뽑았다.
- **보정 문서(`providers/calibration`)에 바닥 영역 싣기**: Tier 0 가 받는 닫힌 문서라 Tier 0 를 먼저 올려야 하고, Visual 층 자료가 위치 정본 문서에 섞인다.
- **산출물을 객체 저장소에 올려 참조로 알리기**(-593 사진처럼): 수 KB 문서라 본문에 바로 싣는 편이 내려받기·해시 대조 없이 단순하다.
- **`scene_version` 에 열 추가**: 같은 보정의 미리보기·정식·이후 Scene 마다 복사해야 한다. 보정 버전 키가 맞다.
- **보정이 바뀌면 서버가 다시 계산**: 서버(Java)에 투영 코드를 또 두게 된다. 세팅을 다시 하면 새로 생기므로 오래됨 표시로 충분하다.
- **기본 켜짐**: 위 「기본 표시」 근거로 버렸다.

## 기존 Scene

이 기능 전에 만든 세팅 Scene(지금 운영 활성 Scene 포함)은 `floor_coverage` 행이 없어 버튼이 보이지 않는다(아무것도 그리지 않는다). 새 세팅을 돌리면 생긴다. 소급 채우기는 저장된 산출물로 할 수 있다: 세팅 미리보기 GLB(`job-objects/site_setup/<job>/<lease>/preview_scene.glb`, MapAnything 점구름 정점색 메시, glTF Y-up = World (x, z, −y))의 정점을 World 로 되돌리면 `floor_coverage_from_points` 입력이 되고, 보정 문서는 `calibration_document` 표에 있다. 결과를 `floor_coverage` 표에 넣으면 된다. 이번 티켓에서는 만들지 않았다(정식 굽기 GLB 는 채움 면이 섞여 관측 바닥이 아니므로 쓰면 안 된다).

## 코드·시험 위치

| 내용 | 위치 |
|---|---|
| 계약 | `contracts/messages/site_setup.schema.json`(`floor_coverage`), `job_callback`·`job_heartbeat` 제목, 예시·반례, `contracts/README.md` |
| ai | `calibration/coverage.py`(`floor_camera_counts`·`_person_visibility`), `calibration/floor_coverage.py`, `scene/reconstruct_tasks.py`(`_mapanything_points`·`write_floor_coverage`), `services/worker/site_setup.py`·`site_setup_stages.py` |
| ai 시험 | `tests/calibration/test_floor_coverage.py`(합성 점구름: 구멍·먼 조각·책상 윗면·성긴 줄·모름, 합성 카메라 배치: `coverage_guidance` 와 같은 수, 칸 문자 인코딩), `tests/scene/test_reconstruct_tasks.py`(World 변환·바닥 없음), `tests/services/worker/test_site_setup*.py` |
| 서버 | `V52__floor_coverage.sql`, `SetupArtifactRegistrar`, `SceneService` |
| 서버 시험 | `SceneServiceTest`(있음·없음·깨짐·기본 배경), `SetupResultRegisterIntegrationTest`(등록→현재 Scene 응답, 먼저 것 유지, 보정 불일치·칸 수 거부) |
| front | `lib/floorCoverage.ts`, `features/viewer3d/scene/FloorCoverageLayer.tsx`(+ store·types·ViewerShell·ViewerCanvas), `features/plan/PlanCoverage.tsx`(+ FloorPlan·LivePlan), `features/layout/useFloorCoverage.ts`·`spatialHost/CoverageLegend.tsx`·`ToolPalette.tsx`·`PlanOverlay.tsx`·`SpatialViewportHost.tsx` |
| front 시험 | `__tests__/lib/floorCoverage.test.ts`, `__tests__/features/viewer3d/floorCoverageLayer.test.ts`, `__tests__/features/layout/SpatialViewportHostCoverage.test.tsx`, `__tests__/features/plan/FloorPlan.test.tsx` |

## 남은 위험

- **가림을 보지 않는다**: 책상·기둥·사람이 가리는 것은 계산하지 않아 표시는 실제 감시 범위의 상한이다(`is_upper_bound`). 특히 메운 구멍(책상 밑)은 기하로는 2대여도 실제로는 안 보일 수 있다.
- **보정이 바뀌면 숨는다**: 새 세팅 없이 보정이 크게 바뀌면(정합 DIMMED·MISMATCH) 범례에 "보정이 바뀌어 감시 범위를 표시하지 않습니다"만 보인다. 다시 계산하려면 세팅을 다시 해야 한다. `rotfix` 소폭 변경은 VERIFIED 동안 예전 범위를 그대로 보인다(회전 2° 까지 먼 바닥에서 한 칸 정도 어긋날 수 있다).
- **바닥 추출 실측 미확인**: 실제 세팅 점구름(MapAnything 518 px 안팎)에서 먼 바닥이 이어지는지, 바깥(창·문 너머) 바닥이 섞이는지는 GPU 세팅 세션에서 확인해야 한다.
- **배포 순서**: 1.6.0 EC2 는 닫힌 `artifacts` 라 floor_coverage 가 실린 하트비트를 400 으로 거부해 세팅이 멈춘다. EC2 를 먼저 배포한다.
- **Flyway 순서**: outOfOrder 를 켜지 않았으므로 V52 가 V49(-602)·V50(-597)·V51(-609) 보다 먼저 운영에 적용되면 뒤 번호가 검증에서 막힌다. 앞 번호 MR 이 먼저 머지되지 않으면 머지 직전에 번호를 다시 고른다.
- **계약 판 충돌**: -605(supplement)와 -597(bake skipped)이 둘 다 v7.29.0·`site_setup` 1.7.0·`job_callback` 2.14.0 을 잡았다(-605 는 `job_heartbeat` 2.7.0 도). 둘 중 하나가 1.8.0·2.15.0 으로 밀린다고 보고 이 티켓은 `site_setup` 1.9.0·`job_callback` 2.16.0·`job_heartbeat` 2.8.0·묶음 v7.30.0 을 쓴다. 판은 제목·설명에만 있어 빈 판이 생겨도 검증에는 영향이 없지만, 같은 파일을 고치므로 나중에 머지되는 쪽이 rebase 로 판·README 를 맞춘다.

## 넘긴 일

- -605 휴대폰 보충 점구름이 World 에 들어오면 그 바닥 점도 넣어 바깥 경계를 넓히고(사각이 "모름" 대신 사각으로 보이게) 다시 계산하는 경로.
- 기존 세팅 Scene 소급 채우기 도구(위 「기존 Scene」).
- 가림 반영(정적 맵 깊이로 칸 중심 시선 가림 검사)은 정확도가 필요해지면 따로 연다.
