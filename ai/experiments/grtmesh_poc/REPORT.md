# grtmesh 텍스처링 PoC (S15P31A101-501)

**결론: 조건부, 지금 교체는 비추천.** grtmesh 는 노트북(RTX 4050, sm_89)에서 빌드되고 빠르며 4대 시점 재투영 MAE 가 기준선의 약 절반이다. 그러나 이 비교는 텍스처링만 본 것이고(깊이를 현재 GLB 에서 렌더), 결과 메시가 점박이 삼각형 수프이며, 운영 입력인 MapAnything 깊이로는 아직 재지 않았다.

Owner: Claude Sonnet(서브에이전트), Lead 정리. 2026-11-05.

## 환경
- WSL(sudo 없음) → micromamba env `grt`: CUDA 12.4.1, GCC 12, cmake 3.29, OpenCV, Eigen. sm_89 빌드 성공, 자체 회귀 시험 `grtmesh_regression` PASS.
- 앱 `multiview_rgbd`(범용 RGB-D 입력: `cameras.json` + `frames/000000/<cam>_color.jpg`·`<cam>_depth.png` uint16).
- 입력: 12차 세팅 보정(4대) + 현재 장면 `textured.glb`(job-24284ceb) 에서 카메라별 깊이 렌더, 1152×648, 1프레임.

## 수치 (4대 시점 재투영 MAE 0~1)
| | 기준선(현재 GLB) | grtmesh 0.02 m | 0.03 m | 0.04 m |
|---|---|---|---|---|
| MAE covered 평균 | 0.1711 | 0.0843 | 0.0883 | 0.0918 |
| MAE valid 평균 | 0.1821 | 0.1123 | 0.1160 | 0.1201 |
| cam_A / B / C / D (covered) | 0.185 / 0.143 / 0.179 / 0.177 | 0.078 / 0.088 / 0.103 / 0.069 | 0.079 / 0.090 / 0.108 / 0.075 | 0.081 / 0.092 / 0.120 / 0.075 |
| 덮임 평균 | 0.827 | 0.819 | 0.818 | 0.815 |
| 면 수 | 149,999 | 1,259,233 | 537,588 | 293,875 |
| 아틀라스 | GLB 내장 | 1152×2592 JPG 1장 | 같음 | 같음 |
| GPU 안 시간 | `bake` 약 178 s(11차 기록, 참고용) | 173 ms | 114 ms | 64 ms |
| 프로세스 벽시계 | 미측정 | 22.5 s | 11.6 s | 6.9 s |
| GPU 메모리 최대 | 미측정 | 2,418 MB | 1,590 MB | 1,332 MB |

- 0.02 m 단계별 ms: TSDF 14, MC 26, 잡음 제거 24, 텍스처 60, 인페인트 58, D2H 42.
- 0.02 m 출력 OBJ 160 MB + JPG 1 MB(기준선 GLB 10.9 MB).
- 지표는 `glb_enhance.render_view` 로 두 메시를 같은 방식으로 렌더(기준선 GLB 의 보강 표식은 메모리에서만 떼고 읽음).

## 해석과 한계
- 학습 시점 평가(평가 시점 사진으로 텍스처를 만듦)이고 grtmesh 면이 8배 많아 유리하다. 새 시점 품질은 모른다.
- 입력 깊이가 기준선 메시 렌더라 TSDF 가 기준선 기하를 재구성한 셈 → 메시 품질이 아니라 텍스처링·시간 비교다. 매끈한 렌더 깊이라 낙관적.
- 시각 품질 나쁨: 벽·바닥 점박이, 가구 둘레 구멍·떠 있는 조각(`compare/cam_A_photo_baseline_grtmesh-v02.jpg`).
- 이음새 보정(`cc_*`)이 1프레임에서 0 — 시간 EMA 전제인지 미확인.
- 126만 면·160 MB OBJ 는 뷰어에 그대로 못 올림 → 단순화·UV 보존 재굽기 필요.
- 현실적 후보(측정 안 한 가설): 기하는 기존 유지 + 텍스처 단계에만 grtmesh 면별 시점 선택·이음새 보정.

## 운영 통합에 필요한 일
1. 이미지에 CUDA devel 멀티스테이지 빌드(nvcc, cmake≥3.25, GCC 12, OpenCV dev, sm_89·sm_90).
2. 입력: MapAnything 깊이 PNG(mm, z 깊이, 0=무효) + 펴진 사진 + cameras.json(OpenCV 축, camera_to_world, bounds), 융합 전 카메라별 스케일 정렬(`run_hybrid_depth`).
3. Python 연결: `multiview_rgbd` 서브프로세스 → OBJ → GLB.
4. 후처리: 단순화(UV 보존), GLB 패키징, `simai` 메타 정합.

## 다음 확인(GPU 세션)
- MapAnything 깊이 입력으로 같은 표 재측정, 새 시점(가린 시점) 평가.

## 파일
- 저장소: `common.py`(보정·사진·깊이 렌더·OBJ 읽기), `prepare_input.py`, `evaluate.py`, `wsl/{mkenv,build,test,exp}.sh`, `run_record.json`(스키마 valid, `env_hash`·`config.sha256` 자리표시, `dirty_worktree` true).
- 저장소 밖: `C:/Users/SSAFY/simai-local/grtmesh-poc/`(소스, 입력, `out-v0{2,3,4}`, `eval*`, `compare/`), WSL `~/grtmesh` 빌드본.
- grtmesh 라이선스 PolyForm Noncommercial, 커밋 4개 초기 저장소.
