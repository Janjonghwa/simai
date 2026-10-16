# EXP-01 정적 배경 복원 — 실험 설계와 E-GATE-0 결과

- Jira: 에픽 S15P31A101-15, 스토리 S15P31A101-74(E-GATE-0) → -77(E-GATE-1) → -82(E-GATE-2) → -85(E-GATE-3·4)
- 기획서: §5.3, §20.2, §20.4(입력 대체 계약), §20.6(채택 게이트)
- 선행 PoC: `Moge-3-test/pocs/mapanything_room4`(RTX 4050 6GB, 2026-10-14). 실행 스크립트는 이 PoC를 옮겨 왔다.

## 1. 연구 질문

MapAnything Native의 결과를 교실 4대 고정 카메라 관측과 실측 평면도에 정합해, COMMITTED Scene에 쓸 수 있는 시각 배경(glTF)으로 만들 수 있는가?

## 2. 파이프라인

| 단계 | 내용 | 입력 | 출력 |
|---|---|---|---|
| 1 입력 | 빈 교실 프레임(카메라별 여러 장) 또는 사람 마스크 합성, 보정(CalibrationProvider) | FrameSource, 보정 | 이미지·K·pose |
| 2 추론 | MapAnything Native, 알려진 K·pose 입력(known 모드) | 1 | depth·point map·confidence·카메라 |
| 3 후처리 | confidence 필터 → 시점 간 깊이 정합 → 이상점 제거 → 표면(TSDF/Poisson) → 텍스처 → 단순화 | 2 | 메시 |
| 4 정렬 | World(m, Z-up) 정렬, 평면도 벽과 비교 | 3, 평면도 | World 메시 |
| 5 내보내기 | glTF(축 변환은 내보내기 어댑터 한 곳), `scene_output` 계약 | 4 | glTF, scene_output.json |

## 3. 게이트와 판정 기준(사전 등록)

| 게이트 | 확인 | 합격 기준 |
|---|---|---|
| E-GATE-0 | A40에서 공식 설정 재현, 가중치 비교, 정밀도, **알려진 K·pose 입력 사용 여부** | 공식 기본 설정 동작, 알려진 pose 배율을 출력이 따라감(입력 배율 대비 ±5%) |
| E-GATE-1 | 교실 실데이터 복원·정합, 실패 사례 | G4: 평면도 대비 벽 차이 5cm, 책상 상판 높이 ±5cm·위치 10cm, 공동 관측점 불일치 중앙값 5cm. MoGe-3 비교군, 미달 시 실측 도면 단순 형상 대안 판정 |
| E-GATE-2 | glTF·scene_output 계약, Three.js 표시 | 스키마 통과, 뷰어에서 World 좌표에 맞게 보임 |
| E-GATE-3·4 | 독립 재현, 실제 입력(G1_AUTO 보정) 재평가, Lead 판정 | 기획서 §20.6. 정답·시뮬레이션 입력만으로는 ADOPT 불가 |

## 4. 실행 환경

| 항목 | 값 |
|---|---|
| GPU | NVIDIA A40 46GB, 드라이버 595.91, CUDA 13.2(드라이버) |
| 데이터센터 | **ca-mtl-1(캐나다 몬트리올)**. 기획서 §3.12의 "미국 리전" 표기와 다름 → 기획서 수정 필요 |
| 환경 | Network Volume venv `/workspace/simai/envs/mapanything`, torch 2.7.1+cu126, map-anything @3d10cf7 |
| env_hash | 잠금 파일 `mapanything.lock.txt` sha256 `19393d30…` (E-GATE-0은 Docker 대신 venv, Docker 이미지는 E-GATE-1 전) |
| 가중치 | `/workspace/simai/weights/map-anything`(CC-BY-NC 4.0), `map-anything-apache`(Apache 2.0) |
| 설치 | `ai/experiments/static_scene/setup_runpod.sh` (약 3.5분) |
| 실행 | `ai/experiments/static_scene/run_mapanything.py`, 비교 `compare_runs.py` |
| 산출물 | `/workspace/simai/runs/exp01/<run_id>/` (metrics.json, cameras.json, raw.npz, fused.ply, *.glb, panels/). 로컬 사본은 `ai/experiments/static_scene/runs/`(커밋하지 않음) |

## 5. E-GATE-0 결과 (2026-10-16)

입력: 선행 PoC와 같은 사진 4장(SSAFY 라운지, 1920×1080, 90° 간격, `datasets/room4`). 실측 정답이 없으므로 **절대 정확도는 판정하지 않는다**.

| Run | 설정 | warm 추론 | GPU 최대 | 기준(C1) 대비 |
|---|---|---|---|---|
| C1 | 이미지만, bf16(공식 기본), CC-BY-NC | 783ms | 8.9GB | 기준. metric scale 2.94, 가로 화각 73.6° |
| C2 | C1 + Apache 가중치 | 850ms | 8.2GB | 카메라 간 거리 0.55~0.64배, 화각 88°. **스케일·화각 불일치(PoC와 같음)** |
| C3 | C1 + FP32 | 912ms | 5.6GB | 거리 0.998배, 회전 0.07°, 깊이 0.2% 차이. **bf16과 사실상 동일** |
| C4a | 알려진 K | 760ms | 9.0GB | 거리 0.99배, 화각 거의 같음 |
| C4b | 알려진 K + pose(×1.00) | 755ms | 9.0GB | 출력 pose가 입력과 2~8cm, 0.3° 이내 |
| C4c | 알려진 K + pose(×1.25) | 853ms | 9.0GB | **출력 거리 1.21~1.23배, 깊이 약 1.23배**(입력 배율 대비 97~98%) |
| C5 | 노트북 웹캠 4장(참고) | 744ms | 8.6GB | 4장 모두 같은 위치에서 찍혀 다시점 평가 불가. 참고용 |

PoC(4050, FP32)와 비교하면 공식 기본 설정(bf16)이 A40에서 동작하고 4장 추론이 2,450ms → 783ms로 빨라졌다. C3(FP32)의 metric scale 2.937은 PoC와 같다.

### 판정

- **알려진 K·pose 입력을 모델이 실제로 쓴다.** pose 이동량을 1.25배 늘리면 출력 스케일이 따라간다(97~98%). 보정의 미터 단위 pose로 복원 스케일을 고정할 수 있다 → 기획서 §16의 MapAnything Native 선택 근거 확인.
- **CC-BY-NC 가중치를 쓴다.** Apache 가중치는 스케일·화각이 맞지 않는다. 교육 프로젝트 조건에서 사용(부록 B).
- **bf16을 쓴다.** FP32와 결과 차이가 무시할 수준이고 더 빠르다.
- E-GATE-0 합격 기준(알려진 pose 배율 ±5%)을 충족한다. **E-GATE-0 통과(제안)** — 독립 재현은 E-GATE-3에서.

### 남은 위험

- 입력 pose를 2~3% 덜 따라간다. 보정이 정확해도 복원 스케일이 2~3% 작을 수 있어, 10m 교실에서 벽 위치가 수 cm~20cm 어긋날 수 있다 → E-GATE-1에서 평면도·실측으로 확인하고, 필요하면 후처리 정렬(4단계)에서 보정한다.
- 결과는 점군·픽셀 격자 메시다. 실제 표면 메시·텍스처 후처리는 아직 없다(E-GATE-1).
- 사람이 찍힌 영역(C1 사진 오른쪽 아래)은 신뢰도가 낮게 나오지만 배경에 섞인다. 빈 교실 촬영 또는 마스크 합성 필요.

## 6. 계약 관련 메모

- `run_record.schema.json`은 `input_provenance.calibration`을 필수로 요구하지만, 이미지만 넣는 Run(C1~C3)에는 보정 입력이 없고 known 모드는 이전 Run의 모델 추정 카메라를 쓴다. 지금 enum(G1_AUTO, MANUAL_PNP, CHARUCO, DATASET_GT, SIM)으로 표현할 수 없다 → `calibration`을 선택으로 바꾸고 `MODEL_ESTIMATE` 출처를 추가하는 마이너 개정 제안(별도 스토리). 그때까지 Run 기록은 각 Run의 `metrics.json`(git commit, env_hash, weights_hash, input_hash, GPU 정보 포함)이다.

## 7. 다음 (E-GATE-1, S15P31A101-77)

1. 교실 빈 상태 프레임 확보(공유기·카메라 설치 후, C110 4대)
2. 수동 PnP 보정(S15P31A101-100)의 K·pose로 known 모드 실행
3. 후처리(정합·표면·텍스처)와 평면도 비교, MoGe-3 비교군
4. Docker 이미지
