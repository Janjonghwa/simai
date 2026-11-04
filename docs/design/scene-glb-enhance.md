# 3D 맵(GLB) 시각 보강 설계 (S15P31A101-481)

관련 명세: 5.3(정적 3D 복원·시각 자산), 규칙 1·7(시각용은 Tier 0 값·위험 판정에 쓰지 않는다).
코드: `ai/src/simai_ai/scene/glb_enhance.py`, 연결 `reconstruct_tasks.py`(`enhance_visual_glb`·bake·preview·fallback), `reconstruct.py`(metrics·notes). 시험: `ai/tests/scene/test_glb_enhance.py`.

## 하는 일
texrecon 으로 굽은 `textured.glb` 는 면의 약 46%가 미관측 회색(`unobserved_neutral`)이고 가려진 바닥은 메시가 없어 검게 뚫린다. 보기만 좋게 두 가지를 더한다.

- (A) 바닥 평면: 관측 바닥 정점(z < 0.08 m)의 xy 1~99 분위 사각형, z = (z 5분위 − 0.02 m), 관측 바닥 텍셀 중앙색 단색. 기존 메시 아래에 깔려 겹침이 없다.
- (B) 미관측 면 색: 미관측 정점마다 반경 0.35 m 안 관측 정점 최근접 6개의 역거리 가중 평균색을 `COLOR_0` 으로 넣고 재질을 정점색 사용(baseColorFactor 1)으로 바꾼 사본으로 교체. 35 cm 밖은 회색 유지.
- (C) 작은 부유 조각 제거는 벽에 구멍이 나서 기각(넣지 않음).

## 결정과 근거
- trimesh 없이 glTF JSON/BIN 을 직접 읽고 BIN 뒤에 덧붙인다. Windows tier0 extra 에 trimesh 가 없고 Pod 이미지에 Blender 가 없다. 관측 메시·텍스처 바이트·좌표는 바이트 그대로(시험이 접두 동일을 단언).
- World→glTF `(x, z, −y)` 는 `texture_mesh.to_glb_points` 한 곳만 쓴다(규칙 3).
- 채택 검사: 보정 카메라(K·R·t)와 사람 지운 배경 사진이 있으면 정점·면 투영 래스터라이저(NumPy)로 원본·보강본을 카메라 시점에 그려 마스크 밖 화소 MAE 를 비교한다. 평균이 `check_tolerance_mean`(0.002), 카메라 한 대가 `check_tolerance_camera`(0.005) 넘게 나빠지면 원본 유지. 메시 없는 화소는 뷰어 바탕색(검정)으로 본다. 입력이 없거나 쓸 카메라가 없으면 메시 지표(빈 바닥 비율·미관측 회색 면적이 늘지 않고 줄어듦)만으로 정하고 `check.mode=MESH_METRICS_ONLY` 로 남긴다.
- 연결은 별도 stage 를 만들지 않고 `bake`·`preview`·`fallback` 끝에서 호출한다(기존 시험이 stage 순서를 고정). 맵 재생성도 같은 StaticReconstructor 경로라 자동 적용. 실패·시간 초과·불채택이어도 단계를 실패시키지 않고 원본을 둔다. 스위치 `SIMAI_GLB_ENHANCE=0` 또는 작업 옵션 `enhance:false`(기본 켬).
- 바닥 평면은 출처 지도에 없는 추정 면이라 `observed_surface_ratio`·SURFACE_SOURCE_MAP 은 건드리지 않고 `extras.simai.enhanced.estimated_floor_plane.surface_source=ESTIMATED_PLANE` 으로 표시. 보강된 GLB 는 `ALREADY_ENHANCED` 로 다시 처리하지 않는다. 삼각형 수는 바닥 적용 시 +2.

## 기록
`bake.json`/`preview.json` 의 `enhance`(요약+detail) → `metrics.json` 의 `enhance`. `quality.notes` 에 `glb_enhance=applied(모드)` 또는 `skipped:사유`. glTF `extras.simai.enhanced`.
미결: scene_output.quality 는 `additionalProperties:false` 라 구조화 `quality.enhance` 를 못 넣는다. 필요하면 scene_output 마이너 버전에서 추가(Lead 결정).

## regen-6 로비 실측 (카메라 A·C, 사진 긴 변 2304 → 렌더 640)
| 지표 | 전 | 후 |
|---|---|---|
| 검사 MAE 평균 | 0.1696 | 0.1364 (A 0.1387→0.1082, C 0.2005→0.1646) |
| 빈 바닥 비율(위에서 본 5 cm 격자) | 0.2244 | 0.0038 (63.65 m² 평면, 14.04 m² 채움) |
| 미관측 회색 면적 | 62.04 m² | 0.75 m² (면 18.4만 중 1828) |
소요 약 17 초, GLB 18.4 → 19.9 MB.

## 잔여 위험
- 검사는 단순 래스터(조명·그림자 없음)라 절대 점수보다 전후 차이만 쓴다. 카메라 2대만 사진이 있어 실제 파이프라인(4대)에서는 더 안정적.
- 정점색은 정점 해상도라 큰 미관측 면은 번진 색으로 보인다(주변 색 추정, 사실이 아님).
- 압축 GLB(meshopt/draco/KTX2)는 보강하지 않는다(압축 전 단계에 둔다).
