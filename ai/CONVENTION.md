# ai 코드 규칙

AI 파트(`ai/`)의 구조와 코드 규칙이다. 브랜치·커밋·MR은 `docs/git-convention.md`를 따른다.

## 구조

```
ai/
  src/simai_ai/      기능별 패키지 (배포 위치와 무관)
    io/              FrameSource, I0 수신, 녹화·시뮬레이터 입력
    detection/       YOLO26-pose 검출·keypoint
    tracking/        Local Tracker, Cross-view 연결, World Track Manager
    geometry/        삼각측량, 시간 보간, 좌표 어댑터, 관절 유효성 게이트
    contact/         ContactEstimator
    calibration/     자동 보정, PnP, CalibrationProvider
    risk/            Risk Engine (Tier 0·EC2 컨테이너 공통)
    scene/           정적 3D (EXP-01 채택분)
    hmr/             인체 메시·외형 (EXP-02 채택분)
    vlm/             VLM 근거·보고서
    providers/       WorldTrackProvider 등 입력 대체 계약 구현 (명세 20.4)
    contracts/       contracts/ 스키마에서 생성한 타입 (손으로 고치지 않음)
  services/          배포 단위 = 기능 조립과 설정
    tier0/           io → detection → tracking → geometry → contact → risk
    worker/          I5 작업 서버 (scene, hmr, vlm)
  experiments/       채택 전 실험 코드 (static_scene, hmr)
  sim/               시뮬레이터
  tests/             src 구조를 따름. tests/risk/vectors/에 Risk Engine 공통 테스트 벡터
```

## 의존 방향

- `risk/`는 다른 기능 패키지를 import하지 않고 계약 타입만 입력으로 받는다. EC2 컨테이너에 Risk Engine만 설치할 수 있어야 한다.
- `experiments/`는 `src/`를 import할 수 있지만, `src/`와 `services/`는 `experiments/`를 import하지 않는다. 채택된 실험 코드는 리뷰를 거쳐 `src/`로 옮긴다.
- `services/`에는 판정·추정 로직을 넣지 않는다. 조립과 설정만 둔다.
- 상류 입력(보정, World 상태)은 Provider 인터페이스로만 받는다. 오프라인용 삼각측량·추적을 따로 만들지 않는다.

## 코드

- Python 3.11, 의존성은 잠금 파일로 고정한다. 모델별 CUDA·Torch 환경은 `deploy/`의 Docker 이미지로 고정한다.
- 시계는 주입받는다. Risk Engine과 Tier 0 코드에서 현재 시각을 직접 읽지 않는다.
- GPU가 필요한 테스트는 pytest marker `gpu`로 표시한다. CI는 GPU 없이 돈다.
- 포맷터·린터와 이름 규칙은 첫 코드가 들어올 때 정한다(기본안: ruff).
