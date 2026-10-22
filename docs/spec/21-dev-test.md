# 21. 개발·테스트 기반

장비 없이도 개발하고, 같은 입력으로 반복 검증하며, 구간 계약이 어긋나지 않게 하는 공통 기반이다. 버전과 도구는 기본안이며 팀 합의로 바꿀 수 있다.

## 21.1 저장소 구조

하나의 저장소(monorepo)에 둔다.

```plain text
contracts/                 # 모든 구간 계약의 정본 (JSON Schema)
  common/                  #   공통 타입 (좌표, 시각, 상태, 출처)
  messages/                #   I0~I5 메시지 (12장)
  providers/               #   입력 대체 계약 (20.4)
  experiments/             #   scene_output, human_mesh_output, run_record
  data/                    #   데이터 형식 계약 (녹화 세션 manifest 등, 15.3)
  examples/                #   스키마별 예시 (CI 검증 대상), invalid/는 실패해야 하는 예시
  scripts/                 #   예시 검증 스크립트
ai/
  src/simai_ai/            # 기능별 패키지 (배포 위치와 무관)
    io/                    #   FrameSource, I0 수신, 녹화·시뮬레이터 입력
    detection/             #   YOLO26-pose 검출·keypoint
    tracking/              #   Local Tracker, Cross-view 연결, World Track Manager
    geometry/              #   삼각측량, 시간 보간, 좌표 어댑터, 관절 유효성 게이트
    contact/               #   ContactEstimator
    calibration/           #   자동 보정, PnP, CalibrationProvider
    risk/                  #   Risk Engine (Tier 0·EC2 컨테이너 공통)
    scene/                 #   정적 3D (EXP-01 채택분)
    hmr/                   #   인체 메시·외형 (EXP-02 채택분)
    vlm/                   #   VLM 근거·보고서
    providers/             #   WorldTrackProvider 등 입력 대체 계약 구현 (20.4)
    contracts/             #   contracts/ 스키마에서 생성한 타입 (손으로 고치지 않음)
  services/                # 배포 단위 = 기능 조립과 설정
    tier0/                 #   io → detection → tracking → geometry → contact → risk
    worker/                #   I5 작업 워커: EC2 에서 작업을 가져감 (scene, hmr, vlm)
  experiments/             # 채택 전 실험 코드
    static_scene/{configs,eval,reports}
    hmr/{configs,eval,reports}
  sim/                     # 시뮬레이터 (가상 교실·사람·카메라 사양은 설정값)
  tests/                   # src 구조를 따름. tests/risk/vectors/에 Risk Engine 공통 테스트 벡터
gateway/                   # 노트북 중계·현장 경보 앱 (Python)
server/                    # EC2 Spring Boot
front/                     # Three.js 뷰어, 반장 PWA
deploy/                    # Docker 이미지 (모델별), Docker Compose, RunPod 템플릿
docs/                      # 팀 규칙(git-convention·jira-convention)
  spec/                    # 기획서 정본(장별), requirements.md 는 요구사항 명세서 정본
  design/                  # 구현 설계 문서(티켓별: 관련 명세 절, 코드·시험 위치, 정한 수치와 근거, 버린 대안, 잔여 위험)
  data/                    # 데이터 manifest(공개 데이터 출처·라이선스·해시, 녹화 manifest 형식과 sessions/ 사본, 15.3)
  experiments/             # EXP 실험 설계와 색인(20장)
```

- **기능 단위로 나누고, 배포 단위는 조립만 한다.** Tier 0 서비스와 분석 워커는 배포 단위일 뿐이므로 `services/`에서 `src/`의 기능을 조립하고 설정만 둔다. 같은 기능을 다른 배포(EC2의 Risk Engine 컨테이너, 현장 프로파일)에서 재사용하기 위해서다.
- **의존 방향.**
  - `risk/`는 다른 기능 패키지를 import하지 않고 계약 타입만 입력으로 받는다. EC2 컨테이너에는 Risk Engine만 설치할 수 있어야 한다.
  - `experiments/`는 `src/`를 import할 수 있지만 `src/`와 `services/`는 `experiments/`를 import하지 않는다. 채택된 실험 코드는 리뷰를 거쳐 `src/`로 옮긴다(20.6).
  - `services/`에는 판정·추정 로직을 넣지 않는다.
- 영상, 가중치, 데이터셋, 대형 산출물은 저장소에 넣지 않는다. GPU 실행 사이트의 저장 위치나 녹화 보관 위치(3.12, 19.1)에 두고 저장소에는 파일 목록과 해시(manifest)만 둔다. 가중치는 저장소의 목록(원본·sha256)으로 시작 절차가 받는다.
- 기획서와 요구사항 명세서의 정본은 저장소 `docs/spec/`, `docs/requirements.md` 다. Notion 에는 정본 링크와 발표·제출용 내보낸 사본만 둔다. 명세를 바꾸는 MR 은 `docs/spec/README.md` 개정 이력에 한 줄을 더한다.

## 21.2 계약 우선

- `contracts/`의 JSON Schema가 구간 필드 정의의 정본이다. 12장의 예시는 스키마로 옮긴 뒤 `contracts/examples/`에서 관리한다.
- 언어별 타입은 스키마에서 생성한다(기본안: Python pydantic, Java 클래스, TypeScript 타입). 생성 코드를 손으로 고치지 않는다.
- 스키마 변경은 12.1의 버전 규칙을 따르고 영향받는 파트에 알린다. I1 LIVE 계약 변경은 Lead 승인이 필요하다(20.4).
- 스키마로 표현할 수 있는 불변 규칙(예: 지지 표면 미확인 시 `ground_anchor_xyz`는 null)은 스키마로 막고, 위반 예시를 `contracts/examples/invalid/`에 둔다.

## 21.3 테스트 계층

| 계층 | 대상 | 실행 위치 |
|---|---|---|
| 단위 | 좌표 어댑터 왕복(4.2), 시간 모델 전이(6.1), 상태 전이, Risk Engine 규칙, 저널 offset 계산 | CI (CPU) |
| 계약 | 각 모듈이 만드는 메시지와 `contracts/examples/`의 스키마 검증, Provider 계약 시험(20.4) | CI (CPU) |
| Risk Engine 공통 벡터 | 같은 입력에 같은 이벤트가 나오는지. Tier 0 배포 이미지와 EC2 컨테이너 이미지 양쪽에서 실행(부록 D-G3-04) | CI (CPU) |
| 시나리오 재생 | 녹화·시뮬레이터 입력을 파일 모드 Tier 0에 넣어 기대 이벤트(정답 파일)와 비교. 시계는 주입식으로 고정해 결과가 매번 같게 | CI (시뮬레이터, CPU 가능 범위) + RunPod (실제 모델) |
| 통합 | Docker Compose로 EC2 스택 + 가짜 Tier 0 송신기 → 뷰어 수신, DB 제약(LIVE만 알림, 11.4) | CI 또는 로컬 |
| 장애 주입 | 강제 종료 복구, 저널 영속성, 재동기화, 현장 경보 재동기화, 하트비트(부록 D.4) | 노트북(시험)과 시연 환경 저장 위치(G3 실측, 3.12)에서 수동·주기 실행 |
| GPU | 모델 추론 정확도·지연(G0), EXP 벤치 | RunPod에서 수동 실행, 결과는 Run 기록으로 |
| 현장 측정 | LED ms 카운터 판·240fps 촬영(glass-to-alarm)·줄자 실측(15.1 측정 방법) | 개발 교실에서 수동 실행, 결과는 Run 기록으로 |

- 녹화 시나리오 재생은 Tier 0 의 파일 입력(`input.mode: file`, 시뮬레이터 출력은 `sim` 입력)으로 한다. 녹화 세션(15.3)을 LIVE 와 같은 I0 수신 경로에 넣고 판정 출처를 REPLAY 로 두며, I1·I0 제어 채널을 열지 않고(실제 알림 없음, 저널에 넣지 않음) 결과를 `world_state`·`risk_event` JSONL 로 남긴다. `world_state` 에는 판정 출처 필드가 없으므로 파일에서는 `input_provenance.frames.source` = FILE 로 LIVE 와 구분하고, 파일을 읽는 쪽은 이 출처를 지우거나 LIVE 입력과 섞지 않는다. EC2 옆 컨테이너의 운영 REPLAY 계산(3.2)과는 다른 개발·시험 경로다(`docs/design/tier0-recording-replay.md`).
- 회귀 시험 항목과 시험 ID(D-Gx-nn)는 부록 D 가 정본이다. 테스트 코드와 Run 기록은 이 ID 로 가리킨다.
- Risk Engine과 Tier 0는 시계를 주입받는다. 실제 시각에 기대는 테스트를 만들지 않는다.
- GPU가 필요한 테스트는 표시해서(예: pytest marker `gpu`) CI에서 제외한다.

## 21.4 CI와 리뷰 규칙

- CI 단계(기본안, GitLab CI): 정적 검사(lint·format) → 단위 → 계약·공통 벡터 → 통합 → 이미지 빌드. GPU 없는 러너에서 돌 수 있어야 한다.
- 브랜치·커밋·MR·Jira는 팀 규칙 `docs/git-convention.md`를 따른다(브랜치 `S15P31A101-{번호}-{설명}`, 커밋 `[S15P31A101-{번호}] 한국어 한 줄`, `dev`로 MR, squash merge). 셀프 merge를 허용한다.
- `contracts/`를 바꾸는 MR은 영향받는 파트에 알리고, MR 본문의 영향 범위에 적는다.
- EXP 실험의 채택 리뷰(20.1 Reviewer)는 MR merge와 별개다. merge는 코드 통합이고, 채택 판정은 20.6의 게이트를 따른다.

## 21.5 개발 환경

- 기본안: Python 3.11(의존성 잠금 파일로 고정), Java 21·Spring Boot 4(`server/`), Node LTS.
- 모델별 Python·CUDA·Torch 환경은 `deploy/`의 Docker 이미지로 고정하고, 이미지 다이제스트를 Run 기록의 `env_hash`로 쓴다(20.5). 다이제스트를 모르는 Run(로컬 이미지, 볼륨 환경)은 `env_hash` 를 다이제스트로 채우지 않는다(볼륨 환경은 잠금 파일 해시).
- 장비 없이 개발할 때는 시뮬레이터와 공개 데이터를 FrameSource로 쓴다(20.4).
