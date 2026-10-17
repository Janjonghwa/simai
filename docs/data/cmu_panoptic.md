# CMU Panoptic Studio — 데이터 manifest

DATASET_GT CalibrationProvider·WorldTrackProvider 어댑터(`ai/src/simai_ai/providers/dataset_gt/panoptic.py`)의 입력이다.
데이터 파일은 저장소에 넣지 않는다. 이 문서에는 출처·라이선스·경로·해시만 둔다(명세 21.1, 20.4 규칙 7).

## 라이선스 (원문 확인: 2026-10-17)

| 출처 | 원문 |
|---|---|
| 데이터셋 홈페이지 http://domedb.perception.cs.cmu.edu/ "License" | "CMU Panoptic Studio dataset is shared only for research purposes, and this cannot be used for any commercial purposes. The dataset or its modified version cannot be redistributed without permission from dataset organizers." |
| 공식 toolbox https://github.com/CMU-Perceptual-Computing-Lab/panoptic-toolbox README | "Panoptic Studio Dataset is freely available for non-commercial and research purpose only." / "By using the dataset, you agree to cite at least one of the following papers." |

지키는 것:

- 연구·비상업 목적으로만 쓴다. 제품 성능 지표나 시연 영상에 쓰지 않는다.
- 원본과 변환본(어댑터 출력 JSONL 포함)을 팀 밖으로 재배포하지 않는다. 저장소에 커밋하지 않는다.
  시험용 표본(`ai/tests/providers/dataset_gt/panoptic_mini/`)은 형식만 흉내 낸 합성 값이며 데이터셋 값을 담지 않는다.
- 결과를 보고할 때 README 의 논문 중 하나 이상을 인용한다(예: Joo et al., "Panoptic Studio: A Massively Multiview
  System for Social Motion Capture", ICCV 2015).
- 이 데이터로 얻은 지표는 입력이 완벽할 때의 상한이다(`input_provenance.is_upper_bound = true`). 현장 데이터와
  분리해 평가한다(명세 15.4, 20.4 규칙 3·7).

## 받은 파일

시퀀스 `171204_pose1` 하나. 영상은 받지 않았다(3D 관절 정답과 보정만).
받는 방법은 toolbox `scripts/getData.sh` 의 주소와 같다: `http://domedb.perception.cs.cmu.edu/webdata/dataset/<seq>/<파일>`.

| 파일 | 크기(byte) | sha256 |
|---|---|---|
| `calibration_171204_pose1.json` | 252,047 | `4a3653c197b97b4a121a16f89e6b07eae5b1864531496c61eea0bf74c3f0900d` |
| `hdPose3d_stage1_coco19.tar` | 41,850,880 | `290333b2f2c770b26d49175ac3f05434b7e53380f6c274db68f5919732c3e1e0` |

tar 를 풀면 `hdPose3d_stage1_coco19/body3DScene_00000118.json` ~ `body3DScene_00027678.json` (27,561개)이 나온다.

| 위치 | 경로 |
|---|---|
| 개발 노트북 (저장소 밖) | `C:/Users/SSAFY/simai-data/datasets/cmu_panoptic/171204_pose1/` |
| Network Volume (올릴 때) | `volume://AP-JP-1/datasets/cmu_panoptic/171204_pose1/` (아직 올리지 않음) |

## 형식과 변환 요약

자세한 규칙은 어댑터 모듈 설명에 있다.

- 보정: HD 카메라 31대, 1920×1080, `K`, `distCoef`(OpenCV 5계수), `R`·`t`(x_cam = R·X + t, 센티미터).
- 3D 관절: `joints19` = 19 × [x, y, z, confidence], 센티미터, 돔 좌표(+Y 가 아래). HD 29.97 fps.
- World 변환: (x, y, z) cm → (x, z, floor_y_cm − y) / 100 m. `floor_y_cm` 기본 0.
  근거(171204_pose1 실측): 발목 관절 y 중앙값 −8.2 cm, p95 −7.2 cm. 변환 후 HD 31대 기준 발목 Z 중앙값 0.079 m.
- 접지·지지 표면·머리 꼭대기 정답이 없어 `ground_anchor_xyz`, `head_z` 는 비운다. 시각은 HD 프레임 번호와 임의 원점으로 정한다.

## 실제 데이터 시험

```sh
# ai/ 에서
SIMAI_PANOPTIC_DIR=<시퀀스 디렉터리> uv run pytest -m dataset
```
