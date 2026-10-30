# 3D 뷰어 배경 사진 화질·출처 지도·접지 그림자·후처리

S15P31A101-360. 기획서 §5.3(정적 3D 배경 glTF), §20.2(EXP-01 관측된 형상과 추정된 공간 구분), §20.3(표면 출처 지도 0·1·2), `scene_output` 2.1.0(`SURFACE_SOURCE_MAP` 0·1·2·3, -317), AGENTS 규칙 1·2·3·7·8 기준이다.
코드는 `front/apps/admin/src/features/viewer3d/scene/SceneBackground.tsx`·`sceneContract.ts`·`sceneStore.ts`, `GroundShadowLayer.tsx`, `PostProcessingLayer.tsx`, `ViewerCanvas.tsx`, `ViewerShell.tsx`, 시험은 `front/apps/admin/src/__tests__/features/viewer3d/viewerBackgroundQuality.test.tsx` 다.

## 1. 바꾼 이유 (사진에 조명을 다시 비추던 문제)

세션 8 로비 텍스처 메시(`lobby314-r518-sim3-plane/bake-area/all/textured.glb`)와 `lobby_014934_s1a.glb` 를 뷰어에 올렸을 때 원본 카메라 사진보다 색이 어둡고 뿌옇게 죽어 보인 원인은 세 가지였다.

1. **이중 조명 + 정점 법선 부재**: `trimesh` 가 내보낸 `textured.glb` 의 사진 텍스처 메시(`textured_0000`~`0007`)는 `KHR_materials_unlit` 확장 없이 기본 `MeshStandardMaterial` 로 들어 있고, 정점 속성에 `NORMAL` 이 없다(`POSITION`, `TEXCOORD_0` 만 존재). 이미 실제 실내 조명이 구워진 사진 텍스처 위에 Three.js 광원을 다시 계산하되, 법선이 없어 `directionalLight(0.8)` 항이 0 이 되고 `ambientLight(0.6)` 만 곱해져 원본 밝기의 약 60% 로 어두워졌다.
2. **ACES 필름 톤매핑과 단면 컬링**: R3F `<Canvas>` 기본값인 `ACESFilmicToneMapping` 이 sRGB 사진 색을 한 번 더 압축해 흰 벽과 파란 표지판 채도가 바랬고, `doubleSided: false` 라 얇은 경계 삼각형이 뒷면 컬링되면서 뒤쪽 `unobserved_neutral` 회색 면이 얼룩처럼 비쳤다.
3. **`DIMMED` 반투명 알파 정렬 깨짐**: `DEFAULT_UNVERIFIED`·`UNCONFIRMED` 상태에서 배경 전체에 `transparent: true, opacity: 0.35` 를 일괄 적용하면, 하나로 합쳐진 방 메시(`textured_0000`, 85만 인덱스) 안에서 앞벽과 뒷바닥 삼각형이 깊이 정렬 없이 인덱스 순서대로 알파 블렌딩되어 앞뒤 가림이 무너졌다.

따라서 **사진 텍스처(`map`)가 있는 배경 메시**는 `MeshBasicMaterial`(`map.colorSpace = SRGBColorSpace`, `toneMapped: false`, `side: DoubleSide`)로 바꾸고, `<Canvas>` 도 `outputColorSpace: SRGBColorSpace`, `toneMapping: NoToneMapping`(`flat`)으로 맞췄다. **텍스처가 없는 정점색·회색 메시**(`scene-demo.gltf` 및 `textured.glb` 의 `unobserved_neutral`)는 기존 `MeshStandardMaterial` 과 반투명 흐림(`DIMMED_OPACITY = 0.35`)을 유지하되, 정점 법선(`normal`)이 없으면 `geometry.computeVertexNormals()` 를 호출해 입체 음영이 살아나게 했다.

### `SceneDisplay`(`FULL`·`DIMMED`·`HIDDEN`) 판정 조건과 `DIMMED` 표현 변경 근거

`sceneContract.ts` 의 `sceneStatusOf` 는 자산이 `LOADED` 된 뒤 아래 조건을 **모두** 만족할 때만 `verification = 'VERIFIED'`, `display = 'FULL'` 로 올려 원래 밝기·채도로 그린다.

1. `GET /api/scenes/current` 응답이 `state === 'COMMITTED'` 이고 `default === false` 이며 `calibration_version` 문자열이 있다.
2. 실시간 `world_state.scene_version` 및 `world_state.calibration_version` 이 모두 도착해 있고, 배경의 `scene_version`·`calibration_version` 과 일치한다.
3. 실시간 `system_health.scene` 의 `committed_version`·`live_version` 이 배경의 `scene_version` 과 일치하고 `mismatch === false` 다.

반대로 다음의 경우에는 정합 근거가 없으므로(`AGENTS` 규칙 7) 규칙을 지우지 않고 그대로 유지한다.

- **`DIMMED`**:
  - `state === 'DEFAULT'`(`default === true`, 시연용 기본 배경 및 로컬 `lobby-preview`) → `verification = 'DEFAULT_UNVERIFIED'`
  - `state === 'COMMITTED'` 이지만 `world_state` 나 `system_health.scene` 이 아직 오지 않아 일치 여부를 확인할 수 없을 때 → `verification = 'UNCONFIRMED'`
  - 단, 개발 검수 토글(`?sceneReview=on`, `SceneReviewContext`)을 켠 동안에만 화면 표시를 일시적으로 `FULL` 로 올린다(상태 판정값 `verification`·`display` 는 그대로 `DIMMED`).
- **`HIDDEN`**:
  - `world_state` 또는 `system_health.scene` 과 버전이 어긋날 때(`verification = 'MISMATCH'`), 혹은 자산을 아직 읽는 중·실패·만료일 때.

**`DIMMED` 표현 방식 변경 근거**:
무텍스처 메시(`scene-demo.gltf` 등)는 기존처럼 `transparent: true, opacity: 0.35`(`DIMMED_OPACITY`)로 낮춰도 단색 벽·바닥이라 문제가 없지만, 하나의 큰 메시(`textured_0000`)에 구워진 사진 텍스처 배경을 반투명으로 만들면 삼각형 깊이 정렬이 깨져 앞벽과 뒷바닥이 뒤섞여 보인다. 따라서 사진 텍스처(`map`)가 있는 메시는 **불투명 깊이 가림(`transparent: false, opacity: 1`)을 유지**한 채 **명도 배율 `0.65`(`DIMMED_TEXTURE_BRIGHTNESS`) + 채도 비율 `0.25`(`DIMMED_TEXTURE_SATURATION`)** 로 낮추고, 상단 배너·상태 줄의 `시연용 기본 배경(정합 미검증)` / `정합 확인 전` 안내를 함께 유지해 정합 근거가 없음을 명확히 알린다.

## 2. 정한 수치와 근거

| 구분 | 항목 | 값 | 근거 |
|---|---|---|---|
| 사진 배경(`map` 있음) | 재질·색공간·톤매핑 | `MeshBasicMaterial`, `SRGBColorSpace`, `toneMapped: false`, `DoubleSide` | 이미 카메라 노출·조명이 구워진 사진 텍스처가 광원 재계산·톤매핑 없이 원본 픽셀 색 그대로 나오게 한다 |
| 무텍스처 메시(`map` 없음) | 재질·법선 | 기존 조명 재질 유지 + 법선 없을 때 `computeVertexNormals()` | `scene-demo.gltf` 와 `unobserved_neutral` 회색 면은 광원 음영이 있어야 벽·바닥 모서리가 구분된다 |
| `DIMMED` 표시(사진 텍스처) | 불투명 유지 + 명도·채도 감쇠 | `transparent: false`, 명도 `0.65`, 채도 `0.25` | 반투명 알파 블렌딩으로 앞뒤 벽 삼각형 가림이 깨지는 현상을 막고, 깊이 버퍼 가림은 유지한 채 사람·구역보다 눈에 덜 띄게 한다 |
| `DIMMED` 표시(무텍스처) | 반투명 불투명도 | `DIMMED_OPACITY = 0.35` | 기존 -59 규칙 그대로 유지 |
| `SURFACE_SOURCE_MAP` 샘플링 | 필터·색공간·밉맵 | `NearestFilter`, `NoColorSpace`, `generateMipmaps: false` | 정수 출처 코드(`0`·`1`·`2`·`3`)가 선형 보간이나 sRGB 감마 변환으로 섞이지 않게 한다(AGENTS 규칙 2·7) |
| 출처 값 `2` (`OBSERVED`) | 명도 / 채도 / 빗금 | `1.00` / `1.00` / `0.00` | 직접 관측된 텍셀은 사진 색 그대로 표시 |
| 출처 값 `3` (`ESTIMATED_PLANE`) | 명도 / 채도 / 빗금 | `0.82` / `0.85` / `0.00` | 색은 카메라 관측이나 형상을 수평 평면에 맞춘 곳(-314)이므로 약하게 감쇠해 직접 관측과 구분 |
| 출처 값 `1` (`INTERPOLATED`) | 명도 / 채도 / 빗금 | `0.72` / `0.75` / `0.00` | 작은 구멍(≤ 25 cm²) 둘레 보간 색이므로 관측·추정 평면보다 한 단계 더 낮춤 |
| 출처 값 `0` (`UNOBSERVED`·기타) | 명도 / 채도 / 빗금 | `0.38` / `0.25` / `0.18`(사선 빗금) | 관측 근거가 없는 곳은 어둡게 흐리고 화면 공간 사선 빗금(`gl_FragCoord`)을 얹어 관측 텍스처처럼 보이지 않게 한다 |
| 접지 그림자(`GroundShadowLayer`) | 조건·크기·불투명도 | `ground_contact.state === 'VALID'`, 지름 `0.68 m`, 최대 알파 `0.42`, `Z = +0.003 m` | 어깨 너비(약 0.45 m)보다 약간 넓은 부드러운 방사형 그림자로 바닥 접지감을 주되, `EXTRAPOLATED`·`OCCLUDED`·`MISSING` 트랙에는 그리지 않는다 |
| 후처리(`PostProcessingLayer`) | 기본값·구성 | 기본 `off`(`false`), 켰을 때 `N8AO`(`aoRadius: 0.6`, `intensity: 1.4`, `halfRes: true`) + `SMAA` | 모서리 깊이감과 계단 현상 완화를 선택 옵션(`postProcessing` prop, `?post=on`)으로 제공하고 기본 렌더 비용은 늘리지 않는다 |

## 3. `SURFACE_SOURCE_MAP` 연결 지점

1. **AI 산출물(-298·-314·-317)**: `texture_mesh.py` 가 `textured.glb` 의 각 텍스처 메시(`textured_0000`~`0007`)와 같은 UV·해상도의 8비트 단일 채널 PNG `surface_source_0000.png`~`0007.png` 를 내고, `contracts/experiments/scene_output.schema.json`(2.1.0) `assets` 배열에 `asset_kind: "SURFACE_SOURCE_MAP"`(`surface_values`: `0=UNOBSERVED`, `1=INTERPOLATED`, `2=OBSERVED`, `3=ESTIMATED_PLANE`)으로 기록한다.
2. **BE(`GET /api/scenes/current`)**: 현재 `server/.../scene/SceneService.java` 는 `scene_version.gltf_key` 하나만 서명해 `gltf_url` 로 내려준다. 운영에서 `SURFACE_SOURCE_MAP` 까지 함께 내려주려면 Scene 등록(`POST /api/scenes`)·조회(`GET /api/scenes/current`)에 선택 필드 `surface_source_map_urls`(아틀라스 순서 배열 또는 재질 이름 키 맵)를 추가해 presigned URL 로 서명해 내려주면 된다.
3. **뷰어(`sceneContract.ts`·`SceneBackground.tsx`)**:
   - `readCurrentScene` 이 응답의 선택 필드 `surface_source_map_urls`(문자열 배열 또는 `{ [materialOrMeshName]: url }`)를 검증해 `CurrentScene.surfaceSourceMapUrls` 로 읽는다.
   - glTF 로더가 재질·메시 `userData.surfaceSourceMap` 에 직접 `Texture` 를 실어 넘긴 경우에도 그대로 인식한다.
   - 개발 하네스(`?scene=lobby-preview&sourceMap=on`)에서는 로컬 캐시의 `surface_source_0000.png`~`0007.png` 를 바로 연결해 확인할 수 있다.

## 4. 노트북 FPS 측정

- **측정 환경**: Windows 노트북, Headless Chrome(`1280×760`), 개발 하네스 `cam_A` 시점(`?view=3d&scene=lobby-preview&camPose=cam_A&source=normal&avatar=capsule`), 3초간 `requestAnimationFrame` 측정.
- **측정 자산**:
  1. 세션 8 `lobby314-r518-sim3-plane/bake-area/all/textured.glb`(16.3 MB, 40만 면)
  2. 핸드오프 `lobby_014934_s1a.glb`(17.5 MB, 30만 면)

| 자산 | 후처리 `off` (`?post=off`, 기본) | 후처리 `on` (`?post=on`, N8AO + SMAA) | 비고 |
|---|---|---|---|
| 세션 8 `textured.glb` | **120.2 FPS** (평균 8.32 ms, p95 8.50 ms) | **120.2 FPS** (평균 8.32 ms, p95 8.50 ms) | 디스플레이 주사율 상한(120 Hz) 유지 |
| 핸드오프 `lobby_014934_s1a.glb` | **120.2 FPS** (평균 8.32 ms, p95 8.40 ms) | **120.2 FPS** (평균 8.32 ms, p95 8.40 ms) | 디스플레이 주사율 상한(120 Hz) 유지 |

이 노트북에서는 후처리 `on`/`off` 모두 120 FPS 상한을 유지했으나, 저사양 통합 GPU 관제 단말에서 다중 창을 띄울 때의 여유를 위해 기본값은 `off`(`false`)로 두었다.

## 5. 세션 9 배경이 온 뒤 남은 일

1. **기하·텍스처 압축(Draco / Meshopt / KTX2)**: 현재 `textured.glb` 는 비압축 정점·인덱스와 PNG 아틀라스(`4096×4096`)로 약 16~17 MB 다. 세션 9 정식 배경이 오면 `EXT_meshopt_compression` 또는 Draco 로 기하 크기를 줄이고, 아틀라스는 KTX2(BasisU UASTC)로 변환해 GPU VRAM 점유와 초기 네트워크 전송 시간을 줄인다(단, `SURFACE_SOURCE_MAP` 은 손실 압축 시 코드 값 `0·1·2·3` 이 깨지므로 무손실 8비트 PNG 유지).
2. **다단계 LOD**: 전체 층 조감 시점과 카메라 근접 시점에 따라 메시 단순화 단계(예: 40만 면 / 10만 면)를 나누어 전환한다.
3. **BE `surface_source_map_urls` 서명 연결**: 위 3절의 `GET /api/scenes/current` 응답에 `surface_source_map_urls` 서명 주소를 실어 뷰어와 종단 간 연결한다.
