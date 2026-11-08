# 관제 뷰어 맵 카메라 자세(`map_cameras`) 투영 설계 (S15P31A101-541)

## 1. 배경 및 관련 명세 절
- **관련 명세 절**: 명세 3.7(시각 배경 Scene), 4.5(Scene 버전 관리), 5.3·7.5(Visual Geometry vs Operational Geometry 분리), 13(3D 관제 뷰어), 20.2·20.4(`scene_output` 계약).
- **선행 티켓**:
  - `S15P31A101-539` (`docs/design/final-map-pipeline.md`): 최종 맵 파이프라인의 기본 경로(`FALLBACK1_MA_ALIGNED_WM2`)는 MapAnything 추론 자세를 Sim(3) 정렬한 `MA_ALIGNED_SIM3` 자세로 TSDF 기하와 텍스처를 굽고, 그 카메라 자세 사본을 `scene_output.method.map_cameras` 및 `scene.glb`의 `extras.simai.map_cameras`에 기록한다.
  - `S15P31A101-527` / `S15P31A101-534` (`docs/design/viewer-view-dependent-texture.md`): 관제 뷰어의 시점 의존 텍스처(VDT) 모듈.
- **해결하려는 문제**:
  - `MA_ALIGNED_SIM3` 자세는 Tier 0 초기 `calibration.json` 자세와 카메라 광학 중심 기준 수 cm(`cam_pose_deltas_m` 약 3~6 cm) 차이가 있다.
  - 뷰어 VDT가 초기 `calibration.json` 자세로 배경 사진을 투영하면, `MA_ALIGNED_SIM3`로 구운 맵 기하 및 깊이 그림자 맵(`bakeVdtShadowMaps`)과 투영 행렬이 어긋나 가구·바닥 경계에서 고스팅과 깊이 일치 게이트(5 cm) 탈락이 발생한다.

## 2. 결정 값과 근거

### 2.1 `R`, `t` 방향 규약 고정 (`OPENCV_WORLD_TO_CAMERA`)
- **규약**: `map_cameras[camera_id]`의 `R`(3×3)과 `t`(3×1, 단위 m)는 **정본 World 좌표계(m, 오른손, +X 동, +Y 북, +Z 상방) → OpenCV 카메라 좌표계(+X 우측, +Y 하방, +Z 정면)** 변환이다:
  $$\mathbf{p}_{\text{cam\_cv}} = R \cdot \mathbf{p}_{\text{world}} + \mathbf{t}, \quad \mathbf{C}_{\text{world}} = -R^\top \mathbf{t}$$
- **근거**:
  - AI 파이프라인(`ai/src/simai_ai/scene/pose_depth_fit.py`)이 `fv.R_w2c`, `fv.t_w2c`를 그대로 `map_cameras`에 직렬화한다.
  - 뷰어 VDT(`vdtTypes.ts`, `vdtMath.ts`)의 `VdtCameraInput` 입력 규약과 동일하므로 추가 역행렬 변환 없이 `worldCoordinates.ts`의 `THREE_TO_WORLD_MATRIX`만 곱해 Three.js 공간 점 $\mathbf{P}_{\text{three}}$를 OpenCV 카메라 좌표로 투영할 수 있다:
    $$\mathbf{p}_{\text{cam\_cv}} = (R \cdot M_{\text{three}\to\text{world}}) \mathbf{P}_{\text{three}} + \mathbf{t}$$

### 2.2 Server `GET /api/scenes/current` 응답 확장
- **결정**: `SceneService.current()`에서 활성 `scene_version.scene_output` JSON의 `method.map_cameras`가 비어 있지 않은 객체이면 응답 최상위에 `"map_cameras"` 필드로 그대로 내려준다.
- **근거**:
  - `V17__scene_job_activation.sql`에서 이미 `scene_version.scene_output (jsonb)` 컬럼을 보관하고 있고 `SceneService.current()`가 `s.scene_output::text AS output`을 조회하고 있으므로 별도 DB 스키마 변경(Flyway 마이그레이션)이나 S3 추가 다운로드 없이 즉시 노출할 수 있다.
  - `map_cameras`가 없는 과거(레거시) Scene이나 시연용 기본(`DEFAULT`) Scene에서는 필드를 생략하여 하위 호환을 유지한다.

### 2.3 Front `viewer3d` 투영 자세 우선순위 및 폴백
- **결정**:
  1. `GET /api/scenes/current` 응답의 `map_cameras`(`CurrentScene.mapCameras`)
  2. `vdt.map_cameras`(`SceneVdtConfig.mapCameras`)
  3. `scene.glb`의 `extras.simai.map_cameras`(`Object3D.userData.simai.map_cameras`, `readSceneMapCamerasFromObject3D`)
  4. 위 세 곳에 모두 없거나 특정 `camera_id`가 빠져 있으면 기존 `vdt.cameras`의 초기 `calibration` 자세(`poseSource: 'CALIBRATION'`)로 폴백한다.
- **근거**:
  - 운영 경로(`GET /api/scenes/current`)뿐 아니라 로컬 개발 하네스(`?scene=vdt-s12`, `?scene=lobby-preview`)처럼 GLB 파일만 직접 로드하는 경로에서도 GLB 내장 `extras.simai.map_cameras`를 통해 동일한 정합을 보장한다.
  - Tier 0 위치 추정 및 카메라 마커(`GET /api/calibrations/current` → `CameraLayer`)는 `calibration.json` 정본을 그대로 사용하여 아키텍처 불변 규칙 1(Tier 0 위치 정본과 3D 시각 맵 분리)을 준수한다.
  - 모든 `World → Three.js` 좌표 변환은 `worldCoordinates.ts`를 통해서만 수행한다(불변 규칙 3).

## 3. 버린 대안
- **대안 A: AI 파이프라인이 `calibration.json` 정본을 `MA_ALIGNED_SIM3` 자세로 덮어쓰기**
  - *기각 사유*: 아키텍처 불변 규칙 1 위반. Tier 0 실시간 사람 발 위치 추정(`world_state`)과 안전 구역 판정은 검증된 `calibration.json` 정본을 써야 하며, 시각 맵 전용 정렬 자세가 이를 오염시키면 안 된다.
- **대안 B: 뷰어가 GLB `extras.simai.map_cameras`만 읽고 서버 응답에는 포함하지 않기**
  - *기각 사유*: GLB 로드 전에 Scene 메타데이터 단계(`readCurrentScene`)에서 카메라 자세 출처(`pose_source`)를 검증·파악하기 어렵고, 외부 GLTF 변형 도구가 top-level `extras`를 누락할 경우 대비가 안 된다. 서버 응답(`map_cameras`)을 1순위로 두고 GLB `userData.simai.map_cameras`를 폴백으로 함께 지원하는 것이 가장 안전하다.

## 4. 남은 위험 및 완화
- **위험**: `vdt.cameras`의 이미지 해상도와 `map_cameras`의 `image_size`가 다를 경우 내부 행렬 `K`의 스케일 불일치 가능성.
- **완화**: `applyMapCamerasToVdtCameras`가 `map_cameras[cameraId]`의 `K`와 `image_size`를 한 쌍으로 함께 교체하고, `convertVdtCameraToThree`는 정규화 내부 파라미터(`fx / w, fy / h, cx / w, cy / h`)로 변환하므로 텍스처 파일 리사이즈 여부와 무관하게 정확한 UV 투영이 유지된다.
