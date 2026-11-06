# S15P31A101-528 GPU 세션 실행 순서 및 예상 시간 (Pod 1회 실행 안내)

> **원칙 (AGENTS.md)**: GPU Pod 는 검증할 때만 켜고, 준비된 큐(`gpu_run_all.sh`)를 한 번에 실행한 뒤 결과 묶음을 노트북으로 가져오고 **즉시 Pod 를 Stop** 한다.
> Pod 는 이미 컨테이너이므로 `docker` 를 쓰지 않고, 스크립트가 `conda-forge::openmvs` 네이티브 바이너리(`TextureMesh`, `RefineMesh`, `ReconstructMesh`)와 `/usr/local/bin/texrecon` 을 설치해 직접 호출한다.

---

## 0. 노트북에서 GPU 세션 전 드라이런(`--mode check`) 확인

GPU Pod 를 켜기 전에 노트북에서 스크립트가 호출하는 모든 파이썬 진입점(`simai_ai.scene.reconstruct_tasks`, `simai_ai.scene.texture_mesh`, `ai/experiments/static_scene/run_hybrid_depth.py`)과 `job.json` 입력 형식을 검증한다:

```bash
cd ai
uv run --with opencv-python-headless python -m experiments.geometry_bench.run_bench --mode check
```

---

## 1. 노트북에서 준비된 입력 묶음 (`s12_geometry_bench_input.tar.gz`)

- **파일 위치**: `C:/Users/SSAFY/simai-local/geometry-bench/s12_geometry_bench_input.tar.gz` (약 32.8 MB)
- **포함 내용**:
  - `calibration.json`: 12차 로비 보정본 (`cal-lobby-init-f0ab1c6b-jba2-2f6cc097`, 4대 카메라 `K`, `R`, `t`, 왜곡 계수)
  - `clean_bg_bundle/`: 12차 빈 배경 프레임 4장 (`cam_A..D.jpg`), 유효 픽셀 마스크 (`cam_A..D.valid.png`), `clean_backgrounds.json`
  - `colmap_undistorted/`: 왜곡을 편 `PINHOLE` 이미지 (`cam_A..D.jpg`) 및 COLMAP 텍스트 모델 (`cameras.txt`, `images.txt`, `points3D.txt`)
  - `scene_template.mvs`: OpenMVS 바이너리 `.mvs` 씬 템플릿
  - `current-scene-textured.glb` & `s12_mesh.ply`: 12차 기준선 메쉬 (149,997 faces, World 미터 좌표)
  - `sample_multiframe_stills/`: 다중 프레임 중앙값 검증용 빈 로비 스틸 프레임 샘플 (`i0000*`, 선택 사용)
  - `manifest.json`: 입력 해시 및 단계별 예상 소요 시간

---

## 2. 단계별 예상 소요 시간 (RTX 4090 / A40 / L40S Pod 기준, 총 약 9.5분)

| 단계 | 작업 내용 | 실제 진입점 / 명령 | 예상 시간 |
|---|---|---|---:|
| **Step 0** | 네이티브 OpenMVS (`conda-forge::openmvs`) + `texrecon` 설치 및 `--mode check` | `micromamba create -y -p /opt/openmvs -c conda-forge openmvs` | ~60 s (최초 1회) |
| **Step 1** | 12차 단일 프레임 MapAnything 518 깊이 + World 정렬 | `python -m simai_ai.scene.reconstruct_tasks prepare <root>`<br>`python -m simai_ai.scene.reconstruct_tasks mapanything <root>`<br>`rt.align_to_calibration(<root>)` | ~10 s |
| **Step 2** | 다중 프레임($N$장) MapAnything 518 깊이 추론 (12차 단일 프레임만 있으면 자동 생략) | 프레임 폴더가 있을 때만 반복 실행, 없으면 단일 프레임 깊이로 자동 대체 | 0 s (단일) / ~45 s ($N=10$) |
| **Step 3** | Depth Anything 3 (1008px) 하이브리드 깊이 추론 (별도 가상환경 `/root/da3`, `numpy<2`) | `/root/da3/bin/python ai/experiments/static_scene/run_hybrid_depth.py predict --ma-run ... --prepared ... --adapter da3 --model ... --process-res 1008 --out ...` | ~20 s |
| **Step 4** | 변형 (0)~(4) 메쉬 융합(`tm.geometry`/`tm.fuse_mesh`) · 정제(`RefineMesh`) · 텍스처(`texrecon`/`TextureMesh`) · LOO · 지표 | `python -m experiments.geometry_bench.run_bench --mode full --variants 0,1,2,3,4 ...` | ~400 s |
| **Step 5** | MapAnything 마스크 조건 A/B/C/D 비교 · TSDF/후처리 면 손실 분해 · 카메라별 구멍 분류 PNG(`-527` 호환) | `python -m experiments.geometry_bench.run_bench --mode hole-analysis --ma-single-root ... --run-gpu-mask-infer` | ~75 s |
| **Step 6** | 결과 압축 (`s12_geometry_bench_results.tar.gz`) | `tar -czf ... full_variants hole_analysis check_dryrun_report.json` | ~10 s |
| **합계** | **업로드 → 전체 실행 → 다운로드 → Pod Stop** | | **약 9~10분 (~566 s)** |

---

## 3. GPU Pod 접속 및 실행 명령 (SSAFY 망: `simai-ec2` 경유)

### 3.1 입력 묶음 업로드 (노트북 → Pod)
```bash
scp -J simai-ec2 -P <POD_SSH_PORT> \
  C:/Users/SSAFY/simai-local/geometry-bench/s12_geometry_bench_input.tar.gz \
  root@<POD_PUBLIC_IP>:/workspace/
```

### 3.2 Pod 안에서 단일 명령으로 변형 (0)~(4) 전체 실행
```bash
ssh -J simai-ec2 -p <POD_SSH_PORT> root@<POD_PUBLIC_IP>

cd /workspace
tar -xzf s12_geometry_bench_input.tar.gz
cd /workspace/S15P31A101

# 1) 드라이런 확인 (--check)
bash ai/experiments/geometry_bench/gpu_run_all.sh --check

# 2) 전체 변형 (0)~(4) 실행 (12차 단일 프레임 기준; 다중 프레임 폴더가 없으면 Step 2 자동 건너뜀)
bash ai/experiments/geometry_bench/gpu_run_all.sh \
  /workspace/s12_geometry_bench_input \
  /workspace/s12_geometry_bench_out
```

### 3.3 결과 다운로드 및 Pod 즉시 종료 (노트북)
```bash
scp -J simai-ec2 -P <POD_SSH_PORT> \
  root@<POD_PUBLIC_IP>:/workspace/s12_geometry_bench_out/s12_geometry_bench_results.tar.gz \
  C:/Users/SSAFY/simai-local/geometry-bench/
```
다운로드 직후 RunPod 콘솔에서 **Pod 를 즉시 Stop** 한다.
