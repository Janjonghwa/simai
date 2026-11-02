# 배경 메시 정리·압축 도구와 뷰어 로더 설계 (S15P31A101-404)

- **관련 명세**:
  - `docs/spec/05-processing.md` §5.3 (3D 배경 재구성 산출물과 버전 관리)
  - `docs/spec/20-ai-experiments.md` §20.3 (EXP-01 정적 3D 배경 재구성 · 텍스처 메시)
  - `docs/spec/13-viewer-3d.md` §13 (3D 관제 뷰어 배경 Scene 표시 · 정합 상태 · 출처 구분)
- **관련 계약**: `contracts/experiments/scene_output.schema.json` (2.1.0, `SURFACE_SOURCE_MAP` 코드 `0=UNOBSERVED`, `1=INTERPOLATED`, `2=OBSERVED`, `3=ESTIMATED_PLANE`)
- **관련 문서**:
  - `docs/design/viewer-background-quality.md` (S15P31A101-360 배경 가독성 · `SURFACE_SOURCE_MAP` · 접지 그림자 · 후처리)
  - `docs/design/exp01-textured-mesh.md` (EXP-01 카메라 역투영 텍스처 베이킹 · `surface_source` 생성)
  - `docs/design/server-site-scene-register.md` (EC2 Scene 등록 API: `POST /api/uploads/sign` → `PUT` → `POST /api/scenes`)
- **코드·시험 위치**:
  - 노트북 정리·압축 코어: `ai/src/simai_ai/scene/mesh_compress.py`
  - Blender 5.2 Headless 스크립트: `ai/tools/scene_mesh/blender_cleanup.py`
  - 노트북 CLI 진입점 및 KTX2 어댑터: `ai/tools/scene_mesh/compress_scene_mesh.py`, `ai/tools/scene_mesh/ktx_cli.mjs`, `ai/tools/scene_mesh/ktx_spawn_hook.mjs`, `ai/tools/scene_mesh/README.md`
  - AI 단위·통합 시험: `ai/tests/scene/test_mesh_compress.py`
  - 3D 뷰어 로더 및 배경 레이어: `front/apps/admin/src/features/viewer3d/scene/sceneStore.ts`, `front/apps/admin/src/features/viewer3d/scene/SceneBackground.tsx`, `front/apps/admin/src/features/viewer3d/scene/meshoptimizer.d.ts`, `front/apps/admin/public/basis/`
  - 3D 뷰어 시험: `front/apps/admin/src/__tests__/features/viewer3d/viewerBackgroundQuality.test.tsx`, `front/apps/admin/src/__tests__/features/viewer3d/sceneBackground.test.tsx`

---

## 1. 배경과 문제 정의

1. **물체 주변 부유 조각(floating islands)**: TSDF/Poisson 재구성 후 카메라 시야 경계나 책상·의자 주변에서 본체(교실 바닥·벽·가구)와 떨어진 작은 부유 삼각형 덩어리가 남는다. 세션 9 정식 배경(`scene-3ce8c02f/textured.glb`)을 3D 공간 연결 성분으로 분석하면 전체 55개 연결 성분 중 33개가 면 수·면적 0.1% 미만의 부유 조각이다.
2. **비압축 기하·텍스처 초기 전송 부담**: 기존 `textured.glb` 는 36개 서브메시(`textured_0000`..`0034` + `unobserved_neutral`), 400,000 삼각형(1,200,000 인덱스), `4096×4096` PNG 사진 아틀라스를 비압축 상태로 담아 파일 크기가 **17.85 MB (`17,847,488` bytes)** 다.
3. **관측 출처(`SURFACE_SOURCE_MAP`) 보존 제약**: `AGENTS.md` 규칙 7(관측과 추정을 구분한다)과 `scene_output` 2.1.0 계약에 따라 `surface_source_0000.png` 의 픽셀 값 `0·1·2·3` 은 손실 압축이나 선형 보간으로 변형되면 안 된다.

---

## 2. 파이프라인 구조와 정한 수치·근거

노트북 도구(`compress_scene_mesh.py`)는 **① Blender 5.2 Headless 기하 정리** → **② `@gltf-transform/cli@4.5.1` Meshopt + KTX2 압축** → **③ 메타데이터(`extras.simai`, `KHR_materials_unlit`) 및 무손실 `SURFACE_SOURCE_MAP` 보존 검증** → **④ `report.json` 출력 및 선택적 Scene `CANDIDATE` 등록** 순서로 동작한다.

| 단계 / 항목 | 정한 수치·방식 | 근거 |
|---|---|---|
| **3D 연결 성분(Island) 판정** | 서브메시 개별이 아닌 **장면 전체(Scene-wide) 3D 월드 정점 좌표(`1e-5 m` 양자화)** 기준 연결 성분 탐색 | `textured.glb` 는 한 교실 메시를 카메라 가시성·아틀라스별로 36개 오브젝트(`textured_0000`..`0034`, `unobserved_neutral`)로 쪼개 둔다. 오브젝트별로 연결 성분을 구하면 주 벽면에 붙은 작은 재질 조각까지 고립된 조각으로 오판해 벽에 구멍이 뚫린다. 3D 월드 정점 공유 기준으로 묶어야 진짜 공중에 떠 있는 조각만 분리된다. |
| **부유 조각 제거 임계값 (`--min-island-ratio`)** | 기본 `0.001` (전체 면 수의 **0.1% 미만** 이면서 전체 표면적의 **0.1% 미만** 인 연결 성분 제거) | 세션 9 교실(`145.43 m²`, `400,000` 면)에서 `0.001` 은 약 `400` 면 · `0.145 m²` (`38 cm × 38 cm`) 미만이다. 의자·책상·단상 본체 성분(각 `0.12%~91.1%`)은 모두 보존하면서 센서 노이즈로 생긴 부유 조각 33개(`4,265` 면, `1.54 m²`)만 정확히 제거한다. 면 수와 면적 두 조건을 모두 만족할 때만 지워, 면 수는 적지만 넓은 평면 판이나 면적은 작지만 조밀한 설비 형상이 실수로 지워지지 않게 방어한다. |
| **UV 보존 면 줄이기 (`--target-faces`)** | 기본 `400,000` 면, Blender `Decimate` (`COLLAPSE`, `delimit={'UV', 'MATERIAL', 'SEAM'}`), 이미 `<= target_faces` 이면 **건너뜀** | UV 아틀라스 경계(`UV`)와 재질 경계(`MATERIAL`)를 잠근 채 Collapse 감축을 수행해 `textured_0000` 아틀라스와 `surface_source_0000.png` 의 UV 정합이 어긋나지 않게 한다. 부유 조각 제거 후 이미 목표 면 수 이하(`395,730 <= 400,000`)면 불필요한 재삼각화를 건너뛰어 원본 기하·UV 품질을 100% 유지한다. |
| **무조명 기하 법선 비생성 (`export_normals=False`)** | 입력 `.glb` 에 `NORMAL` 속성이 없으면 내보낼 때도 법선을 생성하지 않음 | 사진 텍스처 메시는 뷰어에서 `MeshBasicMaterial` (무조명)로 그리므로 정점 법선이 필요 없다. Blender 가 기본값으로 면별 평면 법선(flat normal)을 생성하면 공유 정점이 면마다 쪼개져 정점 수가 `146k` → `668k` 로 4.5배 부풀어 오른다. 입력에 법선이 없을 때 `export_normals=False` 로 내보내고, 뷰어 `SceneBackground.tsx` 가 무텍스처 메시(`unobserved_neutral`)에만 `computeVertexNormals()` 를 호출한다. |
| **기하 압축 (`EXT_meshopt_compression`)** | `@gltf-transform/cli@4.5.1 meshopt --level medium --quantization-volume scene --quantize-position 16 --quantize-texcoord 16` | 16비트 위치·UV 양자화(`KHR_mesh_quantization`)와 Meshopt 부호화를 결합한다. 특히 `--quantization-volume scene` 으로 36개 서브메시가 **단일 장면 양자화 격자**를 공유하게 해 서브메시 경계에서 양자화 오차로 인한 미세 틈(crack)이 생기지 않는다. |
| **사진 텍스처 압축 (`KHR_texture_basisu`)** | `@gltf-transform/cli@4.5.1 uastc --slots "baseColorTexture" --level 2 --rdo --rdo-lambda 1.0 --zstd 18` (기본 `uastc`, `--ktx2-mode etc1s` 선택 가능) | `4096×4096` 사진 아틀라스는 바닥·벽·책상 모서리와 표지판 경계가 한 장에 모여 있어, 저비트율 `ETC1S` 로 압축하면 블록 번짐과 색 번짐(chroma bleeding)이 눈에 띈다. `UASTC` + RDO + Zstd(레벨 18)는 날카로운 경계 품질을 유지하고 GPU 에서 BC7/ASTC/ETC2/RGBA8 로 즉시 트랜스코딩되면서도 전체 `.glb` 크기를 **17.85 MB → 9.44 MB (47.12% 감축)** 로 줄인다. |
| **출처 지도 무손실 보존 (`SURFACE_SOURCE_MAP`)** | `--slots "baseColorTexture"` 로만 KTX2 변환 + 외부 `surface_source_*.png` 8비트 무손실 복사·검증 | `surface_source_0000.png` 는 `0(UNOBSERVED)·1(INTERPOLATED)·2(OBSERVED)·3(ESTIMATED_PLANE)` 이산 코드 지도이므로 손실 압축을 절대 적용하지 않고 8비트 그레이스케일 PNG(`bit_depth=8, color_type=0`) 원본 해시 그대로 유지한다. |
| **뷰어 로더 (`sceneStore.ts`, `SceneBackground.tsx`)** | `GLTFLoader` 에 `MeshoptDecoder` 와 공용 `KTX2Loader`(`transcoderPath = '/basis/'`) 연결 | `three/examples/jsm/libs/basis/basis_transcoder.js`·`.wasm` 을 `/basis/` 정적 자산으로 제공하고, `SceneBackground` 가 현재 R3F `WebGLRenderer` 의 하드웨어 압축 텍스처 지원(`detectSupport(gl)`)을 공용 `KTX2Loader` 에 반영한다. 압축되지 않은 기존 `.glb`/`.gltf` 도 같은 로더로 그대로 읽힌다. |

---

## 3. 세션 9 정식 배경(`scene-3ce8c02f`) 실제 변환 결과

- **입력 파일**: `C:/Users/SSAFY/simai-local/session9/scene-3ce8c02f/textured.glb` (읽기 전용, 원본 미수정)
- **출력 폴더**: `C:/Users/SSAFY/simai-local/scene-mesh-404/` (`textured.glb`, `surface_source_0000.png`, `report.json`)

| 지표 | 변환 전 (원본) | 변환 후 (`scene-mesh-404`) | 변화량 / 비고 |
|---|---:|---:|---|
| **파일 크기 (`textured.glb`)** | `17,847,488` B (17.85 MB) | `9,437,424` B (9.44 MB) | **-8,410,064 B (-47.12%)** |
| **SHA-256 (`textured.glb`)** | `4f144c21...db8af094` | `f8d3325b...ca7ee3c1` | 입력 파일 무변경 검증 완료 |
| **삼각형 면 수 (`faces`)** | `400,000` | `395,730` | 부유 조각 `4,265` 면 제거 (`-1.07%`), Decimate 건너뜀(`<= 400,000`) |
| **3D 연결 성분 (`islands`)** | 총 `55` 개 (`145.43 m²`) | 보존 `22` 개 (`143.89 m²`) | 부유 조각 **`33` 개 제거** (`1.54 m²`, 전체 면적의 `1.06%`) |
| **사용 glTF 확장** | `KHR_materials_unlit` | `EXT_meshopt_compression`, `KHR_mesh_quantization`, `KHR_texture_basisu`, `KHR_materials_unlit` | `extras.simai` 메타데이터 및 재질 `unlit` 보존 |
| **`surface_source_0000.png`** | `320,174` B (`4096×4096`, 8-bit PNG) | `320,174` B (`4096×4096`, 8-bit PNG) | SHA-256 `cbf1cffc...ddc4df9` **100% 동일(무손실)** |
| **뷰어 적재 검증** | 36개 메시, `boundsWorld` 정상 | 36개 메시, `395,730` 면 디코딩, `boundsWorld` `[0.003, 0.001, -0.026]..[9.020, 8.952, 2.800]` m | `MeshBasicMaterial`(`toneMapped: false`) + `surface_source_0000.png` + `unobserved_neutral` 법선 정상 |

---

## 4. 버린 대안과 근거

1. **GPU 분석 워커(Pod) 이미지 안에서 실시간·배치 처리**:
   - **기각 근거**: GPU Pod 컨테이너 이미지에 헤드리스 Blender(수백 MB)와 Node/KTX 도구를 추가하면 이미지 크기와 기동 시간이 크게 늘어나고, `AGENTS.md` 원칙(GPU Pod 는 검증·추론할 때만 켜고 Tier 0 가 자원을 우선 사용)에 어긋난다. 정적 배경 정리는 세션 산출물 생성 시 노트북에서 오프라인으로 1회 수행해 S3 에 올리는 편이 비용·격리 면에서 유리하다.
2. **`trimesh` / `open3d` (`simplify_quadric_decimation`) 기반 면 줄이기**:
   - **기각 근거**: Open3D/trimesh 의 기본 QEM 감축은 다중 서브메시 아틀라스 UV 경계(`UV` seam)와 재질 경계(`MATERIAL`) 잠금 기능이 약해, 감축 후 `textured_0000` 사진 아틀라스와 `surface_source_0000.png` 경계에서 텍스처 늘어짐·번짐이 발생한다. Blender 5.2 의 `Decimate(COLLAPSE, delimit={'UV', 'MATERIAL', 'SEAM'})` 는 UV 아틀라스 경계를 엄격히 보존한다.
3. **Draco (`KHR_draco_mesh_compression`) 기하 압축**:
   - **기각 근거**: Draco 는 압축률은 높으나 WASM 디코딩 CPU 비용이 크고 정점 순서·인덱스 순서가 바뀌어 서브메시 경계 정합이나 스트리밍에 불리하다. 반면 `EXT_meshopt_compression` 은 디코딩 속도가 매우 빠르고(Three.js 내장 `meshopt_decoder.module.js` 지원), 웹 전송 시 gzip/Zstd 와 결합했을 때 크기 이점도 유사하다.
4. **오브젝트별(Per-object) 연결 성분 기준 부유 조각 제거**:
   - **기각 근거**: `textured.glb` 의 36개 오브젝트는 카메라 가시성 조합별로 나뉜 서브메시다. 오브젝트 내부에서만 연결 성분을 따지면 큰 벽면 한가운데에 있는 작은 가시성 패치까지 "작은 조각"으로 분류되어 벽면에 구멍이 뚫린다. 장면 전체 3D 정점 공유 그래프에서 연결 성분을 구해 해결했다.

---

## 5. 잔여 위험

1. **아주 큰 부유 노이즈(`>= 0.1%` 면 수 또는 면적)**: 카메라 역투영 아티팩트가 `400` 면 이상으로 크게 뭉쳐 있으면 기본 임계값(`--min-island-ratio 0.001`)으로는 남을 수 있다. 이 경우 CLI 의 `--min-island-ratio`(예: `0.003`)를 높여 재실행하고 `report.json` 의 `removed_faces_ratio` 로 확인한다.
2. **KTX2 UASTC 의 VRAM 점유**: UASTC 는 디스크 전송 크기(Zstd 압축)는 작지만 GPU 업로드 시 BC7/ASTC/ETC2(4~8 bpp) 또는 미지원 환경에서 RGBA8 로 풀린다. 저사양 내장 GPU 에서 VRAM 이 부족하면 CLI 의 `--ktx2-mode etc1s` 옵션으로 재생성할 수 있다.

---

## 6. 넘긴 일 (이번 티켓 범위 밖)

1. **다단계 LOD (Level of Detail)**: 조감 시점(예: 10만 면)과 근접 시점(40만 면)에 따라 다단계 `.glb` 자산을 전환하려면 `contracts/experiments/scene_output.schema.json` 및 서버 `GET /api/scenes/current` 응답 스키마 변경이 필요하므로 후속 과제로 넘긴다.
2. **배경 재구성 워커 파이프라인 자동 연동**: 현재는 노트북 CLI(`compress_scene_mesh.py --register-scene`)로 정제·압축 후 EC2 Scene `CANDIDATE` 등록까지 한 번에 수행한다. 향후 오프라인 배경 재구성 자동 큐에 결합할 때 이 모듈(`simai_ai.scene.mesh_compress`)을 호출하도록 연결한다.
