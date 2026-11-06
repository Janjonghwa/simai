# 12차 로비 GPU 세션 일괄 실행 가이드 (`-515` 3D-Fixer + `-514` FLUX.1-Fill-dev 8-bit)

H100 GPU Pod(Linux, CUDA >= 11.8, VRAM >= 24 GB) 한 세션에서 **`-515` 3D 물체 제자리 완성(`HorizonRobotics/3D-Fixer`)** 과 **`-514` 평면 미관측 영역 확산 인페인팅(`black-forest-labs/FLUX.1-Fill-dev` 8-bit 양자화)** 을 순차적으로 한 번에 실행하고 단일 결과 압축본(`s12_gpu_poc_results.tar.gz`)으로 회수하기 위한 스크립트 모음이다.

## 1. 노트북에 미리 준비된 단일 업로드 번들

- 경로: `C:/Users/SSAFY/simai-local/gpu-poc-map/s12_gpu_poc_input.tar.gz` (폴더: `s12_gpu_poc_input/`)
- 포함 내용:
  - `current-scene-baseline.glb`: `-511` 추정 평면을 제거한 12차 로비 순수 TSDF 원본 GLB
  - `current-scene-textured.glb`: 12차 로비 기존 GLB
  - `calibration.json`: 4대 카메라(`cam_A..cam_D`) 최신 보정
  - `images_raw/cam_{A..D}.jpg`: 4대 카메라 원본 배경 사진
  - `images_undistorted/cam_{A..D}.png`: 왜곡 보정 배경 사진
  - `candidates/*.npz`, `manifest.json`: 분리된 물체 후보(`cone_00` + 일반 물체)별 3D-Fixer 조건 입력 텐서(`518×518` 마스크 크롭, 정규화 `K`, OpenCV `cam2world`, 부분 점 클라우드)

## 2. H100 Pod 일괄 실행 절차

```bash
# 1) 노트북 -> Pod로 입력 번들 업로드 (SSAFY 망에서는 -J simai-ec2 경유)
scp -P <POD_PORT> C:/Users/SSAFY/simai-local/gpu-poc-map/s12_gpu_poc_input.tar.gz root@<POD_IP>:/workspace/

# 2) Pod 안에서 일괄 실행 (3D-Fixer 설치 -> 3d_fixer 물체 완성 -> FLUX.1-Fill-dev 8-bit 평면 인페인팅 -> 통합 GLB 및 결과 tar.gz 압축)
export HF_TOKEN="<HUGGINGFACE_TOKEN_FOR_FLUX1_FILL_DEV>"
bash ai/experiments/object_completion/gpu/run_all_s12.sh \
  /workspace/s12_gpu_poc_input.tar.gz \
  /workspace/s12_gpu_poc_run

# 3) 완료 후 단일 결과 압축본을 노트북으로 회수하고 즉시 Pod Stop
scp -P <POD_PORT> root@<POD_IP>:/workspace/s12_gpu_poc_run/s12_gpu_poc_results.tar.gz \
  C:/Users/SSAFY/simai-local/gpu-poc-map/
```

## 3. 순차 메모리 관리 및 주요 환경 변수

- **프로세스 분리 및 명시적 언로드**:
  - `Step (b)`(`3d_fixer` 물체 완성)와 `Step (c)`(`FLUX.1-Fill-dev` 8-bit 평면 인페인팅)는 별도 Python 서브프로세스로 실행되며, 각 단계 종료 시 `backend.unload()` / `unload_diffusion_pipeline()` 및 프로세스 종료로 GPU VRAM을 100% 반환한 뒤 다음 모델을 적재한다.
  - 각 단계와 물체별 GPU 피크 메모리(`torch.cuda.max_memory_allocated` -> `gpu_max_memory_allocated_mb`) 및 소요 시간(`seconds`)을 JSON 보고서(`gpu_session_summary.json`, `session12-objects-3d_fixer-report.json`, `session12-plane-inpaint-report.json`)에 기록한다.
- **주요 환경 변수**:
  - `SIMAI_OBJECT_BACKEND=3d_fixer` (기본값 `3d_fixer`; 실패·불가 시 `convex_hull` 대체 없이 원본 메시 조각 유지)
  - `SIMAI_GLB_INPAINT=diffusion`
  - `SIMAI_INPAINT_MODEL_ID=black-forest-labs/FLUX.1-Fill-dev`
  - `SIMAI_INPAINT_QUANTIZATION=8bit` (`bitsandbytes` 8-bit 또는 `torchao` int8 양자화)
  - `SIMAI_INPAINT_STEPS=20`, `SIMAI_INPAINT_GUIDANCE=30.0`, `SIMAI_INPAINT_TILE_SIZE=1024`, `SIMAI_INPAINT_TILE_OVERLAP=128`

## 4. 산출물 구조 (`s12_gpu_poc_results.tar.gz`)

- `gpu_session_summary.json`: 단계별 GPU 피크 메모리(`gpu_max_memory_allocated_mb`), 소요 시간(`seconds`), 채택 물체 수, 평면 인페인팅 요약
- `objects/`:
  - `session12-objects-3d_fixer.glb`, `session12-objects-3d_fixer-report.json`
  - `01_candidates_bev.png` .. `05_surface_source_provenance.png`
- `inpaint/`:
  - `session12-plane-inpainted.glb`, `session12-plane-inpaint-report.json`, `session12-plane-inpainted_fill/`
  - `comparisons/floor_compare.png`, `comparisons/wall_*_compare.png`, `comparisons/scene_4cam_inpaint_compare.jpg`
- `combined/`:
  - `session12-combined-planes-and-objects.glb`: `-514` 확산 인페인팅 평면 + `-515` 3D-Fixer 복원 물체 + 라바콘 정형 도형을 하나로 합친 최종 GLB
  - `session12-combined-4cams.jpg`: 4대 카메라 전/후 비교 이미지
