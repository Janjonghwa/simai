# G0 검출 기준선: YOLO26-pose 크기별 지연·정확도 (S15P31A101-18)

Tier 0 관측(명세 5.1)의 검출·keypoint 모델 크기를 고르기 위한 G0 측정(명세 15.1)이다.
**결정(2026-10-17, 사용자): Tier 0 검출 기준선은 YOLO26x-pose, TensorRT FP16, imgsz 1280.**
운영 GPU 는 일본 AP-JP-1 H100 80GB 다. 모델 크기별 정확도 순서(n < s < m < l < x)는 Ultralytics 공식 벤치마크에 이미 있고 이 폴더의 COCO 500장 측정도 같다(x 가 가장 높음).
H100 에서 x·1280·FP16·카메라 4대 동시 입력은 38 ms(카메라당 약 26 FPS, EXP-02 H6 측정, docs/experiments/EXP-02_design.md 6.4)로 Tier 0 목표(카메라당 10~15 FPS)를 넘는다.
아래 노트북 RTX 4050 6GB 측정은 병목 분석·도구 검증용 참고값이며 모델 선택 근거로 쓰지 않는다(명세 6.6: 지연 판정은 시연 GPU 값으로 한다).

## 범위

| 항목 | 이번에 한 것 | 하지 않은 것 (이유) |
|---|---|---|
| 크기 | YOLO26-pose n / s / m / l / x | |
| 지연 | 2304×1296(C110 stream1) 입력, imgsz 640·1280, batch 1(카메라 1대)·4(4대 동시), FP16, p50/p95, VRAM | 디코딩·전송·추적 포함 끝단 지연(capture_to_risk). Tier 0 파이프라인이 생긴 뒤 G0 기반 측정에서 잰다 |
| 백엔드 | PyTorch FP16, TensorRT FP16(dynamic, 최대 batch 4) | |
| NMS 경로 비교 | NMS 사용(one-to-many 헤드 + 후처리 NMS)과 NMS-free(one-to-one 헤드) 모두 | NMS 를 엔진 안에 넣는 export(`nms=True`). 같은 one-to-many 출력에서 NMS 위치만 다르다(측정하지 않음). 후처리 시간을 줄일 후보라 H100 측정 때 필요하면 추가한다 |
| 정확도 | COCO val2017 사람 keypoint 500장, OKS AP·PCK@0.1·관절 저신뢰율·사람 재현율 | 교실 상단 시점·책상 뒤 무릎·발목 누락(명세 G0 항목). 교실 녹화와 주석 GT 가 생긴 뒤 같은 지표로 잰다 |
| GPU | 노트북 RTX 4050 Laptop 6GB (LOCAL) | RunPod H100 (다음 GPU 세션) |

정확도 수치는 공개 데이터(`input.source = DATASET`)의 모델 비교값이다. 제품 성능이나 G0 합격 근거로 쓰지 않는다(AGENTS.md 규칙 7).
크기 사이의 상대 차이와 NMS 경로 차이를 보는 데만 쓴다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `det_common.py` | 공용: 재현 정보 수집, 백분위, 가중치·TensorRT 엔진 로드, 결과 기록 |
| `prepare_coco_subset.py` | COCO val2017 부분집합 ID 를 고르고 그 이미지만 받는다 |
| `export_engine.py` | TensorRT FP16 엔진 하나를 별도 프로세스로 만든다 |
| `bench_latency.py` | 크기·경로·imgsz·batch 별 지연·VRAM (`--backend` 는 `pt` 또는 `engine`) |
| `eval_coco.py` | 크기·경로별 OKS AP·PCK·관절 저신뢰율·사람 재현율 (`--backend` 는 `pt` 또는 `engine`) |
| `conf_sweep.py` | 저장된 예측으로 사람 conf 임계값별 재현율·PCK·정밀도 근사 |
| `summarize.py` | Run 들을 마크다운 표로 묶는다(`runs/summary_<커밋>.md`) |
| `queue_4050.sh` | 위 순서 전체를 한 번에 돌린다 |

## 설계

### NMS 경로와 NMS-free 경로

YOLO26 은 학습 때 one-to-many 헤드와 one-to-one 헤드를 함께 두고, 기본 추론은 one-to-one 헤드(NMS-free, top-k 만)다.
ultralytics 8.4.178 에서 두 경로를 이렇게 고른다.

| 경로 | PyTorch (`predict`) | TensorRT (`export`) | 후처리 |
|---|---|---|---|
| `nms` | `nms=True` → `end2end=False`, one-to-many 헤드 | `nms=None` → one-to-many 원출력 | ultralytics 후처리의 torchvision NMS (IoU 0.7) |
| `e2e` | `nms=False` → `end2end=True`, one-to-one 헤드 | `nms=False` → one-to-one 헤드 + TopK 를 엔진에 포함 | conf 거르기만 |

실제로 어느 헤드로 돌았는지는 각 행의 `end2end_head`(로드된 모델의 `end2end` 속성)로 남긴다.

### 지연

- 입력: 부분집합에서 사람 주석이 가장 많은 COCO 이미지(000000004134, 사람 13명)를 2304×1296 으로 늘린 프레임 하나. 같은 프레임을 batch 장 넣는다.
- 한 번의 `model.predict(frames)` 를 `torch.cuda.synchronize()` 로 감싼 벽시계 시간. CPU 레터박스 리사이즈 + 추론 + 후처리가 들어간다.
  ultralytics 단계별 시간(전처리·추론·후처리)도 호출 기준(장당 값 × batch)으로 남긴다.
- warmup 30회 뒤 100회를 재고 최근접 순위 p50/p95/p99 를 낸다. 설정마다 시작·끝 GPU 온도를 남긴다.
- `predict` 는 rect 레터박스를 쓰므로 2304×1296 은 imgsz 640 에서 384×640, 1280 에서 736×1280 텐서가 된다(행의 `input_tensor_shape`). TensorRT 엔진은 dynamic 으로 내보내 같은 크기로 돈다.
- TensorRT 동적 프로파일은 `min 1×3×32×32 / opt 4×3×(384|736)×imgsz / max 4×3×imgsz×imgsz` 로 고정한다(`det_common.engine_profile`).
  ultralytics 기본값(opt = imgsz 정사각형, max = imgsz 의 2배)으로는 x·1280 빌드가 6GB 에서 메모리 부족으로 실패했고, 엔진 메모리도 커진다.
  Tier 0 입력은 카메라 해상도로 고정이라 opt 를 실제 입력에 맞췄다. 그래도 x·1280 은 빌드 중 일부 전술을 메모리 부족으로 건너뛰었으므로 4050 의 x·1280 TensorRT 값은 최적 엔진보다 느릴 수 있다.
- 엔진 내보내기는 `export_engine.py` 별도 프로세스로 한다. 같은 프로세스에서 내보낸 직후 재면 빌더가 남긴 메모리로 장치 메모리가 차서
  WDDM 이 공유 시스템 메모리로 넘기고 지연이 몇 배로 늘었다(m·1280 batch 4: 72 ms → 496 ms). 장치 메모리가 95% 이상 찬 행은 `vram_near_full` 로 표시한다.
- VRAM: `torch.cuda.max_memory_allocated`(PyTorch 텐서 최대치)와 측정 직후 `nvidia-smi memory.used`(장치 전체, TensorRT 엔진·CUDA 컨텍스트·다른 프로세스 포함).
- batch 4 의 `호출/s` 는 4대 동시 처리 때 카메라당 FPS 와 같다. 명세 G0 목표는 카메라당 12 FPS 이상이다.

### 정확도

- 데이터: COCO val2017 `person_keypoints_val2017.json`. 사람 주석이 1개 이상인 이미지 ID 를 정렬해 `random.Random(0)` 으로 섞은 앞 500장
  (관절 주석이 있는 비군중 사람 1,150명). ID 목록과 해시는 `runs/coco_subset_500_s0.json` 에 남는다.
- 예측: imgsz 640(COCO 원본이 640px 이하라 1280 은 업샘플만 된다), conf 0.001, max_det 100, 이미지 한 장씩.
- OKS AP: pycocotools `COCOeval(iouType="keypoints")`, `imgIds` 를 500장으로 제한.
- 운영점 지표(사람 conf ≥ 0.25 인 예측만):
  - GT 사람과 예측을 bbox IoU ≥ 0.5 로 탐욕 매칭한다.
  - PCK@0.1: 보이는 GT 관절(v>0)마다 거리 ≤ 0.1 × max(GT bbox 너비, 높이) 면 맞음. 매칭 안 된 사람의 관절은 틀림.
  - 관절 저신뢰율: 매칭된 사람의 보이는 GT 관절 중 예측 관절 신뢰도 < 0.5 비율. 하류(삼각측량)가 관절을 버리게 되는 비율의 근사다.
  - 무릎·발목만 따로 낸다(명세 G0 의 책상 뒤 하체 관절 항목을 교실 데이터로 재기 전 참고값).
  - 사람 재현율: 매칭된 GT 사람 / 비군중 GT 사람.

## 설치와 실행

측정 venv 는 저장소 밖에 둔다. CUDA 빌드 torch 가 필요해 `ai/uv.lock`(기본 설치)이 아니라 `uv pip` 으로 만든다.
`ai/pyproject.toml` 의 `detection` extra(ultralytics)는 그대로 쓰고, 측정 전용 패키지(pycocotools, TensorRT)는 venv 에만 넣는다.

```sh
# 저장소 최상위, Git Bash
uv venv ~/.venvs/simai-det --python 3.11
PY=~/.venvs/simai-det/Scripts/python.exe          # Linux 는 bin/python
uv pip install --python $PY torch torchvision --index-url https://download.pytorch.org/whl/cu128
uv pip install --python $PY -e "./ai[detection]" pycocotools nvidia-ml-py "tensorrt-cu12>=10" onnx onnxslim onnxruntime-gpu

# COCO 주석(241MB zip 에서 person_keypoints_val2017.json 만 푼다). 이미지는 prepare_coco_subset.py 가 500장만 받는다(약 79MB)
mkdir -p ai/experiments/recordings/coco && cd ai/experiments/recordings/coco
curl -LO http://images.cocodataset.org/annotations/annotations_trainval2017.zip
unzip -j annotations_trainval2017.zip annotations/person_keypoints_val2017.json -d annotations && cd -

# 전체 큐: 부분집합 → 지연(PyTorch) → TensorRT 엔진 사전 빌드 → 지연(TensorRT) → 정확도(PyTorch, TensorRT) → conf 임계값 분석 → 요약표
PY=$PY bash ai/experiments/detection_baseline/queue_4050.sh
```

- 가중치(`yolo26{n,s,m,l,x}-pose.pt`)는 ultralytics 공식 릴리스에서 `ai/experiments/recordings/weights/` 로 받고,
  TensorRT 엔진은 `recordings/weights/engines/yolo26{크기}-pose_{경로}_{imgsz}_b4_fp16.engine` 로 한 번 만든 뒤 다시 쓴다. 둘 다 커밋하지 않는다.
- 엔진은 GPU·드라이버·TensorRT 버전에 묶이므로 H100 에서는 다시 만든다. 4050 에서 엔진 하나에 2~7분 걸렸다(20개 약 1시간 30분).
- H100(Linux)에서는 `queue_4050.sh` 의 run_id 접두사(`4050`)만 바꿔 같은 스크립트를 돌린다. 측정 시각에 Tier 0 나 다른 Job 이 GPU 를 쓰지 않게 한다.
- `conf_sweep.py` 는 정확도 Run 의 저장된 예측으로 사람 conf 임계값별 재현율·PCK·정밀도 근사를 다시 센다(GPU 불필요). 결과는 `runs/<run_id>/conf_sweep.json`.

## 기록 형식

Lead 결정에 따라 이 측정은 `contracts/experiments/run_record.schema.json`(EXP-01/02·INT-01 실험용)을 쓰지 않는다.
G0 측정 기록 형식은 S15P31A101-111(G0 기반 성능 보고)에서 정한다. 그때까지 `runs/<run_id>/metrics.json` 에 아래를 직접 남긴다(`runs/` 는 커밋하지 않는다).

| 필드 | 내용 |
|---|---|
| `run_id`, `jira_key`, `kind` | `lat-4050-{pt,trt}-fp16-<커밋>` / `acc-4050-coco500-{pt,trt}-<커밋>`, `latency` 또는 `accuracy` |
| `started_at`, `finished_at` | UTC epoch ms |
| `git` | `commit`, `branch`, `dirty_worktree`(이 폴더의 `*.py` 가 커밋과 다르면 true. true 인 Run 은 근거로 쓰지 않는다) |
| `env` | python, torch, torch CUDA, cuDNN, ultralytics, `uv_lock_sha256`(ai/uv.lock), `pip_freeze`(측정 venv 전체 목록)와 그 해시, `provider: LOCAL` |
| `env.gpu` | nvidia-smi 이름·드라이버·P-state·전력 한도·최대 클럭·온도·메모리, `ac_power`(Windows 배터리 상태 2 = AC), 배터리 % |
| `input` | `source: DATASET`. 지연은 COCO 이미지 ID·원본 크기·늘린 프레임의 sha256, 정확도는 부분집합 파일·이미지 ID 목록 해시·주석 파일 해시 |
| `config` | 명령행 인자 전체(+ 정확도 임계값) |
| `weights` | `<크기>_<경로>_<imgsz>` 마다 `.pt` 파일명·sha256, TensorRT 엔진 파일명·sha256·동적 프로파일·export 시간(그 Run 에서 만든 경우) |
| `rows` | 지연: 크기·경로·imgsz·batch·`end2end_head`·`input_tensor_shape`·`wall_ms`·`stage_ms`·`calls_per_s_p50`·`frames_per_s_p50`·검출 인원·VRAM·온도. 정확도: OKS AP 10종·PCK·저신뢰율(관절별 포함)·재현율 |

정확도 Run 은 `runs/<run_id>/dets_<크기>_<경로>_<imgsz>.json`(COCO 결과 형식 예측)도 남긴다.

## 결과


2026-10-17, 커밋 `480cd84`(`dirty_worktree=false`), RTX 4050 Laptop 6GB(드라이버 591.44, AC 전원), torch 2.11.0+cu128, ultralytics 8.4.178, TensorRT 11.4.0.
Run: `lat-4050-pt-fp16-480cd84`, `lat-4050-trt-fp16-480cd84`, `acc-4050-coco500-pt-480cd84`, `acc-4050-coco500-trt-480cd84` (노트북 `runs/`, 커밋 안 함).
장치 메모리가 95% 넘게 찬 행(`vram_near_full`)은 없다.
TensorRT 엔진 20개 중 x·1280 두 개는 측정 프로세스가 떠 있으면 빌드가 메모리 부족으로 실패해 `export_engine.py` 로 먼저 따로 만들었다
(그 뒤 `queue_4050.sh` 에 사전 빌드 단계를 넣었다). 엔진 sha256 과 프로파일은 각 Run 의 `weights` 에 있다.

### 지연 · TensorRT FP16 (벽시계 p50 / p95, ms)

2304×1296 프레임 기준. batch 4 = 카메라 4대 동시 한 번 처리.

| 크기 | 640·b1 nms | 640·b1 e2e | 640·b4 nms | 640·b4 e2e | 1280·b1 nms | 1280·b1 e2e | 1280·b4 nms | 1280·b4 e2e |
|---|---|---|---|---|---|---|---|---|
| n | 9.3 / 12.5 | 6.8 / 8.4 | 20.4 / 25.3 | 13.8 / 16.5 | 12.9 / 15.7 | 13.3 / 16.5 | 45.6 / 52.3 | 36.4 / 42.4 |
| s | 8.1 / 10.8 | 5.6 / 9.2 | 18.6 / 21.7 | 12.3 / 15.8 | 13.2 / 15.1 | 11.0 / 13.9 | 47.2 / 52.4 | 44.0 / 46.6 |
| m | 7.8 / 11.3 | 6.7 / 8.2 | 22.5 / 26.0 | 18.6 / 20.1 | 18.2 / 20.1 | 17.2 / 18.2 | 69.3 / 74.3 | 66.6 / 69.4 |
| l | 9.1 / 10.7 | 8.1 / 9.2 | 25.3 / 27.4 | 22.4 / 24.0 | 21.3 / 22.8 | 20.7 / 21.7 | 82.3 / 86.2 | 79.7 / 83.4 |
| x | 12.2 / 13.6 | 11.4 / 12.5 | 39.5 / 42.0 | 36.2 / 39.8 | 36.1 / 38.5 | 35.7 / 37.3 | 141.7 / 146.4 | 139.7 / 143.9 |

4대 동시(batch 4)의 카메라당 FPS(= 1000 / 벽시계 p50)와 단계별 시간:

| 크기 | 640 카메라당 FPS (nms / e2e) | 640 추론 p50 (e2e) | 1280 카메라당 FPS (nms / e2e) | 1280 추론 p50 (e2e) | 1280 전처리 p50 (CPU) | 1280 장치 메모리 (nms) |
|---|---|---|---|---|---|---|
| n | 49.1 / 72.7 | 4.9 ms | 21.9 / 27.5 | 11.4 ms | 21.0 ms | 483 MB |
| s | 53.7 / 81.6 | 4.7 ms | 21.2 / 22.7 | 20.1 ms | 20.0 ms | 659 MB |
| m | 44.4 / 53.9 | 10.1 ms | 14.4 / 15.0 | 43.3 ms | 19.9 ms | 865 MB |
| l | 39.5 / 44.7 | 13.8 ms | 12.2 / 12.6 | 56.3 ms | 20.2 ms | 911 MB |
| x | 25.3 / 27.6 | 27.5 ms | 7.1 / 7.2 | 115.1 ms | 20.6 ms | 1263 MB |

### 지연 · PyTorch FP16 (벽시계 p50 / p95, ms)

| 크기 | 640·b1 nms | 640·b1 e2e | 640·b4 nms | 640·b4 e2e | 1280·b1 nms | 1280·b1 e2e | 1280·b4 nms | 1280·b4 e2e |
|---|---|---|---|---|---|---|---|---|
| n | 28.8 / 39.3 | 23.2 / 35.0 | 34.9 / 45.0 | 28.7 / 38.8 | 23.5 / 34.3 | 22.6 / 26.6 | 49.9 / 66.4 | 45.6 / 69.1 |
| s | 26.8 / 39.1 | 20.7 / 23.5 | 32.5 / 45.4 | 25.8 / 28.4 | 28.9 / 37.6 | 23.9 / 30.8 | 66.6 / 69.1 | 63.7 / 66.5 |
| m | 25.5 / 35.8 | 22.9 / 30.9 | 39.0 / 46.0 | 32.4 / 34.7 | 29.1 / 33.4 | 28.4 / 31.5 | 125.5 / 130.9 | 122.3 / 126.2 |
| l | 36.0 / 46.4 | 29.5 / 33.1 | 45.8 / 55.9 | 38.7 / 40.7 | 35.2 / 40.3 | 34.5 / 36.0 | 153.2 / 157.9 | 149.8 / 154.0 |
| x | 31.4 / 39.4 | 36.5 / 48.9 | 69.3 / 74.6 | 66.4 / 68.0 | 61.4 / 63.2 | 61.5 / 62.9 | 269.2 / 276.6 | 268.8 / 272.4 |

PyTorch 는 batch 1·640 에서 크기와 상관없이 20~36 ms 로, GPU 연산이 아니라 커널 실행 오버헤드(Windows WDDM, eager 모드)에 묶인다(측정 중 GPU 사용률 40% 안팎).
크기 비교는 TensorRT 값으로 한다.

### 정확도 · COCO val2017 500장 · imgsz 640 (DATASET, 모델 비교용)

PyTorch FP16 값. TensorRT FP16 의 OKS AP 는 모든 행에서 PyTorch 와 0.001 이내로 같았다.

| 크기 | 경로 | OKS AP | AP50 | PCK@0.1 | 무릎·발목 PCK@0.1 | 관절 저신뢰율 | 사람 재현율 |
|---|---|---|---|---|---|---|---|
| n | nms | 0.587 | 0.832 | 0.805 | 0.785 | 0.058 | 0.850 |
| n | e2e | 0.587 | 0.827 | 0.783 | 0.776 | 0.055 | 0.824 |
| s | nms | 0.643 | 0.857 | 0.846 | 0.825 | 0.056 | 0.890 |
| s | e2e | 0.640 | 0.854 | 0.838 | 0.822 | 0.057 | 0.878 |
| m | nms | 0.703 | 0.892 | 0.869 | 0.859 | 0.056 | 0.898 |
| m | e2e | 0.700 | 0.894 | 0.865 | 0.861 | 0.056 | 0.886 |
| l | nms | 0.713 | 0.899 | 0.866 | 0.853 | 0.053 | 0.893 |
| l | e2e | 0.711 | 0.893 | 0.861 | 0.854 | 0.051 | 0.885 |
| x | nms | 0.726 | 0.906 | 0.879 | 0.879 | 0.058 | 0.898 |
| x | e2e | 0.728 | 0.910 | 0.866 | 0.871 | 0.054 | 0.884 |

크기 순서와 간격은 ultralytics 가 공개한 val2017 전체 pose mAP 와 같은 경향이다(500장 부분집합이라 절대값은 다를 수 있다).

### NMS 경로 vs NMS-free 경로

- 정확도: OKS AP(임계값 없이 전체 순위로 보는 지표)는 두 경로가 ±0.004 안이다.
  운영 임계값 conf 0.25 에서는 e2e 가 사람 재현율이 0.8~2.7%p 낮다(n −2.7, s −1.1, m −1.2, l −0.8, x −1.4).
- `conf_sweep.py` 로 임계값을 바꿔 보면 이 차이는 대부분 점수 분포 차이다. e2e 를 conf 0.2 로 내리면 nms·0.25 와 재현율이 같아진다
  (s: e2e·0.2 재현율 0.891 / 정밀도 근사 0.753, nms·0.25 0.890 / 0.761. n: 0.844 / 0.776 vs 0.850 / 0.793. x: 0.897 / 0.802 vs 0.898 / 0.814).
  같은 재현율에서 e2e 정밀도가 0~2%p 낮다. COCO 는 교실보다 덜 밀집해 있어, 명세의 "가림·밀집에서 누락이 늘면 정확도 우선" 판단은 교실 데이터로 해야 한다.
- 지연: e2e 는 NMS 후처리가 없어 TensorRT batch 4 후처리가 6.3~8.4 ms → 2.5~3.2 ms 로 준다. batch 4 벽시계 기준 640 에서 3~7 ms, 1280 에서 2~9 ms 빠르다.
  4050 에서 NMS 비용은 크기와 무관하게 거의 일정하므로 작은 모델일수록 차이가 크다.

### 해석

1. **4대 × 12 FPS 는 4050 TensorRT 에서도 imgsz 640 은 모든 크기, 1280 은 l 까지 통과한다.** 1280 의 x 는 7 FPS 로 미달이다.
   다만 1280·batch 4 벽시계의 20 ms 는 CPU 레터박스(2304×1296 4장 리사이즈)다. n·s 는 이 전처리가 벽시계의 절반에 가까워, Tier 0 에서는 디코딩한 GPU 프레임을 GPU 에서 리사이즈해야 크기 차이가 그대로 드러난다.
2. **n 은 속도 이점이 작다.** TensorRT batch 4 벽시계에서 640 은 n 과 s 가 같고(s 가 1~2 ms 빠르게 나옴), 1280 은 NMS 경로 1.6 ms, e2e 7.6 ms 차이다.
   추론만 보면 1280 에서 11 ms vs 20 ms 다. 반면 정확도는 s 가 OKS AP +5.3~5.7점, 사람 재현율 +3.9~5.5%p, 무릎·발목 PCK +4.0~4.5%p 높다.
3. **m 은 s 보다 정확도가 뚜렷이 높다**(OKS AP +6.0점, PCK@0.1 +2.4%p, 무릎·발목 PCK@0.1 +3.4%p). 대가는 1280·batch 4 추론 20 → 43 ms 다(4050).
   l 은 m 대비 OKS AP +1.0점에 추론이 30% 더 들고, x 는 m 대비 +2.3점에 2.7배가 든다.
4. **H100 환산은 비율로 하지 않는다.** EXP-02 에서 H100 은 x·1280·4대 동시가 38 ms(PyTorch)였고 4050 PyTorch 는 269 ms 다(약 7배). 작은 모델은 오버헤드 비중이 커 같은 배율이 나오지 않으므로 H100 에서 다시 잰다.

## 기준선 (2026-10-17 결정)

| 역할 | 모델 | 근거 |
|---|---|---|
| **Tier 0 기준선** | **YOLO26x-pose, TensorRT FP16, imgsz 1280** | 운영 GPU H100 80GB. 크기별 정확도는 공식 벤치마크와 이 측정 모두 x 가 가장 높다. H100 에서 1280·4대 동시 38 ms(카메라당 약 26 FPS)로 목표를 넘는다. 교실 2304×1296 에서 먼 자리 사람이 작아 1280 을 쓴다 |
| EXP-02 HMR 검출기 | YOLO26x-pose | 같은 모델(EXP-02 E-GATE-0 결정과 일치) |
| NMS 경로 | 잠정 NMS 경로(nms=True), conf 0.25 | 같은 conf 에서 사람을 덜 놓친다. NMS-free 는 conf 약 0.2 에서 재현율이 같아진다. 교실 밀집·가림 장면에서 다시 본다 |

4050 결과로 x 를 제외했던 이전 판단은 철회한다(노트북 6GB 한계일 뿐 운영 GPU 판단 근거가 아니다).
H100 에서 따로 크기 비교를 다시 하지 않는다. 다음 GPU 세션에서는 Tier 0 와 분석 워커(HMR·VLM)를 같은 H100 에서 동시에 돌릴 때 x 의 여유만 G0 동시 실행 항목으로 확인한다.

## 한계

- 노트북 RTX 4050 Laptop(WDDM, 6GB) 값이다. 지연 목표 판정에 쓰지 않는다(명세 6.6). 클럭·온도가 고정되지 않아 같은 설정을 다른 Run 에서 다시 재면
  batch 1·640 처럼 짧은 호출은 p50 이 최대 ±40%, 1280·batch 4 는 ±5% 안에서 흔들렸다. 작은 설정의 크기 간 차이는 이 범위 안에서 읽는다.
- 지연 입력은 COCO 이미지 한 장(사람 13명)을 늘린 프레임이다. 사람 수가 많아지면 NMS 후처리 시간이 는다.
- x·1280 TensorRT 엔진은 6GB 에서 일부 전술을 메모리 부족으로 건너뛰고 만들어졌다.
- 정확도는 COCO 부분집합(정면·수평 시점 위주)이다. 교실 상단 시점과 책상 가림은 반영되지 않는다.
- 모델 추론만 쟀다. 디코딩, I0 수신, 추적, 삼각측량을 포함한 capture_to_risk 는 Tier 0 파이프라인 이후 G0 기반 측정에서 잰다.
