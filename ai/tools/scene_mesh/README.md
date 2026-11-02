# 배경 메시 정리·압축 도구 (`ai/tools/scene_mesh/`)

EXP-01(`static_reconstruct`) 산출물인 `textured.glb` 를 노트북에서 일회성 후처리하여,
작은 부유 조각(floating island)을 제거하고 UV 보존 면 수 감축(`Decimate` `COLLAPSE`) 후
`@gltf-transform/cli@4.5.1` 로 기하(`EXT_meshopt_compression`)와 사진 텍스처(`KHR_texture_basisu` KTX2)를 압축한다.

> [!IMPORTANT]
> - `SURFACE_SOURCE_MAP` 텍스처(`surface_source_NNNN.png`)는 코드 값 `0·1·2·3` 이 손실 압축으로 깨지지 않도록 **무손실 8-bit PNG** 로 유지·복사한다.
> - 입력 `.glb` 파일은 절대 덮어쓰지 않으며, 최상위 `extras.simai` 와 머티리얼 속성(`KHR_materials_unlit` 등)을 보존한다.

## 1. 전체 파이프라인 실행 (권장)

```powershell
uv run --project ai python ai/tools/scene_mesh/compress_scene_mesh.py `
  --in C:/Users/SSAFY/simai-local/session9/scene-3ce8c02f/textured.glb `
  --out C:/Users/SSAFY/simai-local/scene-mesh-404 `
  --target-faces 400000 `
  --min-island-ratio 0.001 `
  --ktx2-mode uastc
```

산출물(`--out` 디렉터리):
- `textured.glb`: 정리·압축 완료된 배경 메시 (`EXT_meshopt_compression`, `KHR_mesh_quantization`, `KHR_texture_basisu`)
- `surface_source_NNNN.png`: 입력 폴더에 있던 출처 지도(무손실 8-bit PNG 그대로 복사)
- `report.json`: 입력/출력 SHA-256, 전후 면 수, 제거된 부유 조각 수·비율, 전후 파일 크기, 사용 도구·버전, 설정값 기록

## 2. Blender 5.2 헤드리스 단독 실행

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" -b `
  --python ai/tools/scene_mesh/blender_cleanup.py -- `
  --in C:/Users/SSAFY/simai-local/session9/scene-3ce8c02f/textured.glb `
  --out C:/Users/SSAFY/simai-local/scene-mesh-404 `
  --target-faces 400000 `
  --min-island-ratio 0.001
```

## 3. 새 Scene 버전(`CANDIDATE`) 등록

압축된 `textured.glb` 를 관리자 API(`POST /api/uploads/sign` → `PUT` → `POST /api/scenes`)를 통해
기존 ACTIVE Scene 을 덮어쓰지 않고 새 `CANDIDATE` 버전으로 등록한다.
관리자 토큰은 **환경변수(`SIMAI_ADMIN_TOKEN`)로만 주입**하며, 명령줄 인자나 출력에 노출하지 않는다.

```powershell
$env:SIMAI_ADMIN_TOKEN = "<관리자_BEARER_토큰>"
uv run --project ai python ai/tools/scene_mesh/compress_scene_mesh.py `
  --in C:/Users/SSAFY/simai-local/session9/scene-3ce8c02f/textured.glb `
  --out C:/Users/SSAFY/simai-local/scene-mesh-404 `
  --register-scene `
  --server-url https://simai.live `
  --site-id <SITE_UUID> `
  --aligned-to-calibration-id <CALIBRATION_UUID>
```
