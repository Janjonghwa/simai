# S15P31A101-652 14차 GPU 세션 운영 수정 묶음

14차 GPU 세션(2026-11-12, 로비, H100 80GB, 이미지 unified-7558a7a333d7)에서 나온 운영 문제 중 서로 독립인 여섯 가지를 한 티켓에서 고친다(3번 띠는 -647 이 화면에서 없애 빠졌다). 아바타 지연·-540 오탐·PPE·R3 는 다른 티켓(-646·-650·-651) 몫이라 손대지 않는다.

## 관련 명세

- 명세 3.2: Tier 0 우선, 단일 GPU 인스턴스, 이중 감시(관제 화면 장애 띠 FR-11).
- 명세 5.3·`docs/design/S15P31A101-605-map-supplement-capture.md`: 보조 촬영은 맵 형상·텍스처에만 쓴다. World·보정·판정에는 쓰지 않는다(규칙 1).
- 명세 6.2: 구성 묶음 적용은 판정 tick 경계에서 원자적으로 한다.
- 명세 12.5(I5), 계약 `messages/site_setup` 1.13.0.

## 1. 보강 촬영(-605)

### 1a. 사람 분할 가중치 전달 (`deploy/scripts/simai_bootstrap.py`)

- 원인: 보강 촬영과 FRAME 입력은 `SIMAI_SEG_WEIGHTS` 로 yolo26x-seg 를 읽는다(`reconstruct_tasks`). 그런데 부트스트랩이 이 값을 워커에 넘기지 않았다. 가중치 목록에서 이 파일은 아바타 묶음 항목(env `SIMAI_AVATAR_SEG_WEIGHTS`)이라, 세팅 가중치 단계도 받지 않았다. 그 결과가 `SEGMENTER_MISSING` 이었다.
- 바꾼 것:
  - 워커 환경에 `SIMAI_SEG_WEIGHTS` 를 기본으로 넣는다. 값은 목록 항목의 경로이고, 없으면 `weights/yolo/yolo26x-seg.pt` 다. 운영자가 준 값이 우선한다.
  - 굽기 자식은 워커 환경을 그대로 잇는다(`ReconstructProcess`). 장면 준비 모드(`run_scene_child`)에도 같은 기본값을 넣는다.
  - 세팅 가중치 단계(`_prepare_setup_weights`)가 이 항목을 함께 둔다. 통합 이미지에서는 구운 사본을 링크하므로 수 초 걸린다. 아바타를 띄우지 않아도 보강 촬영이 쓸 수 있다.
  - 운영자가 다른 경로를 주면 목록 항목은 받지 않는다.

### 1b. 정렬 관문 (`ai/src/simai_ai/scene/supplement_capture.py`)

- 14차 실측: 40/40 프레임이 등록됐다. 고정 카메라 재투영은 4.546 → 4.777 px 로 통과했다. 중심 잔차는 baseline 0.00001 m → 0.063 m 여서 `ALIGNMENT_WORSE` 로 거부됐다.
- 원인: baseline 은 고정 4대를 자기 자신에 맞춘 값이라 ≈0 이다. 그래서 "baseline + 2 cm" 는 사실상 절대 2 cm 기준이었다.
- 바꾼 것: 판정 순서는 등록 수 → 재투영 → 중심 잔차다.
  - 재투영이 주 판정이다. 기준은 그대로 `1.25 × baseline + 0.5 px` 다. 14차 값으로는 한계 6.18 px 에 실측 4.777 px 라 통과다.
  - 중심 잔차는 절대 안전선 `align_max_center_m = 0.08` m 로 바꿨다. baseline 은 기록용으로만 남긴다.
- 8 cm 근거:
  - 14차 실측 6.3 cm 는 재투영도 통과한 정상 묶음이다. MapAnything 묶음마다 생기는 스케일·중심 흔들림이 이 정도다.
  - 정렬 해(`solve_fixed_alignment`)의 상한은 10 cm / 5° 다. 8 cm 는 그 안쪽에서 정상 흔들림 위에 2 cm 정도 여유를 둔 값이다.
  - 보조 촬영은 맵 형상·텍스처에만 들어간다. 위치·판정 정본(Tier 0)에는 영향이 없다. 정렬이 실제로 틀어지면 재투영이 먼저 잡는다.
- 버린 대안: 묶음 크기를 40 → 6~8 장으로 줄이는 안, 세로 영상 크롭을 고치는 안. 효과를 녹화로 재야 하는 변경이라 이번 범위에서 뺐다(남은 위험).

### 1c. 요약의 파일별 결과 (계약 site_setup 1.13.0)

- 결함: 묶음이 거부되면 `used_media` 가 비었다. 그래서 등록된 프레임이 있던 파일까지 `NOT_REGISTERED` 로 덮였다. 14차 요약에서 휴대폰 영상이 "고정 카메라와 맞추지 못함"으로 보여 원인 판별을 흐렸다.
- 바꾼 것:
  - `supplement.media[].status` 에 `REGISTERED_NOT_USED` 를 더했다(마이너). 뜻은 "자리 잡은 프레임은 있지만 맵에 쓰지 않음"이고, 이유는 요약 `reason` 에 있다(관문·병합·굽기 실패·회수).
  - `NOT_REGISTERED` 는 frames_registered 0 인 파일만 뜻한다.
  - 워커가 APPLIED 뒤 굽기 실패·회수로 FALLBACK 으로 바꿀 때(`_fallback_summary`)도 USED 를 이 값으로 바꾼다.
  - 관제 앱 문구는 "붙지 않음 · 화면과 맞췄지만 품질 기준에 걸려 넣지 않음"이다.
- 배포 순서: EC2 를 먼저 올린다. 이전 EC2 는 jar 에 든 계약으로 job_callback·job_heartbeat 를 검사해 새 값을 거부한다.

## 2. 게이트웨이 heal (`gateway/ops/simai-gateway.ps1`)

- 11-09 미리보기가 죽었는데 heal 로그는 "이상 없음"이었다.
- 원인: 모드 창은 `powershell -NoExit` 라 안의 중계(uv → simai-relay)가 죽어도 창은 열린 채 남는다. heal 은 창 PID 만 봤다. PID 가 다른 PowerShell 에 재사용돼도 살아 있다고 봤다.
- 바꾼 것:
  - 순수 함수 `Test-ModeTreeAlive` 로 판정한다. 창마다 ① powershell/pwsh 가 있는지, ② PID 파일 뒤(+5 s)에 시작한 프로세스가 아닌지(PID 재사용) 본다.
  - 영상 중계·경보 단말 창은 ③ uv·python·simai-* 자식이 있는지도 본다. conhost 는 세지 않는다. 관제 터널 창은 끊겨 3 s 쉬는 동안 자식이 없을 수 있어 창만 본다.
  - 죽었으면 이전 모드로 다시 띄운다. 미리보기, 그리고 상태 파일에 Pod 주소가 있는 관제가 대상이다. 관제는 원래 알리기만 했다.
  - 30분 안에 3번 되살렸는데 또 죽으면 더 되살리지 않고 알린다(`notify-mode-flapping`). 근거: heal 주기는 5분이라 30분은 여섯 번이다. 절반 넘게 죽으면 원인이 안 풀린 것으로 본다.
- 시험: Pester 3.4(`simai-gateway.Tests.ps1`)로 판정 함수만 본다. 22개.

## 3. "관제 정지 · 판정 서버(Tier 0) 응답 없음" 띠: 해당 없음

처음에는 이 띠를 4 s 넘게 이어질 때만 띄우게 고쳤다(EC2 link-timeout 3 s + 4 s). 그런데 dev 에 먼저 들어간 -647 이 장애 띠(FailureBand)와 재세팅 배너를 화면에서 없앴다. 그래서 rebase 때 이 변경을 뺐다. 지금 관리자 화면에는 이 문구가 뜨는 곳이 없다. 다시 띠를 둔다면 같은 지연 규칙을 쓴다.

## 4. Tier 0 자동 재기동 (`simai_bootstrap.py` Supervisor)

- 14차: Tier 0 가 죽으면 감시 프로세스가 사이드카까지 다 내렸다.
- 바꾼 것:
  - 비정상 종료면 Tier 0 그룹만 SIGKILL 로 정리한다. 사이드카(워커·vLLM·아바타·PPE)는 그대로 두고 백오프 1·2·4·5·5 s 뒤 Tier 0 만 다시 띄운다. 해당 환경 변수는 `SIMAI_TIER0_RESTART_BACKOFF_S`·`_MAX_S` 다.
  - 죽은 동안은 기동 시각을 비운다. 그러면 죽은 Tier 0 가 남긴 running 표시 파일(15 s 신선)을 준비로 보지 않아, 사이드카를 새로 띄우지 않는다.
  - 다시 띄우지 않는 종료 코드: 0 정상(종료 신호·녹화 끝), 2 설정 오류, 3 재시작 복구 거부(운영 절차 필요), 5 file 결과 기록 실패. 다시 띄워도 결과가 같거나 운영자가 정한 종료다. 1(예외)·4(입력)·신호 종료는 다시 띄운다.
  - 연속 실패 상한: `SIMAI_SUPERVISE_MIN_UPTIME_S`(60 s) 안에 다시 죽는 일이 `SIMAI_TIER0_MAX_RESTARTS`(5)번을 넘으면 예전처럼 전체를 내리고 같은 코드로 끝낸다. 60 s 넘게 돌았으면 1부터 다시 센다.
  - 감시 프로세스가 받은 SIGTERM·SIGINT 는 예전처럼 워커 → vLLM → Tier 0 순서로 내린다.
- 5번 근거: 기존 vLLM 포기 상한(`SIMAI_SUPERVISE_MAX_FAST_FAILURES` 5)과 같다. 백오프 합은 17 s 에 Tier 0 기동 시간이 더해진다. 같은 원인으로 죽는 경우는 1~2분 안에 멈춘다.

## 5. 구성 묶음 적용 로그 (`ai/src/simai_ai/config/sync.py`)

- 14차: 적용 성공 기록이 없었다. 어떤 묶음·PPE 요구·R3 판으로 도는지 가리는 데 30분 넘게 걸렸다.
- 바꾼 것: tick 경계 전환 때 INFO 한 줄을 남긴다. 담는 값은 config_version·applied_at·보정·안전모/조끼 요구(정책 버전·작업 ID)·R3 규칙 버전·구역 수(구역 ID 기준)·작업면 수(형식 오류로 뺀 수)다. 읽기 실패한 항목은 `?` 로 두고 판정을 멈추지 않는다. config_ack 의 schema_version 줄은 -648 몫이라 건드리지 않았다.

## 6. GPU 세션 절차 (`deploy/README.md`)

"EC2 배포 동결" 절에 "GPU 세션 시작·끝 절차"를 더했다.
- 시작: `install -m 755` 로 deploy.sh·freeze.sh 설치 → `cmp` 확인 → 동결 on·status.
- 끝: 게이트웨이 미리보기 → Pod 서비스 내리기·결과 가져오기·Stop → 워커 토큰 폐기 → 동결 off → 건너뛴 배포 다시 실행 → Pod 임시 수정은 MR 로.

## 시험

- `ai/tests/deploy/test_simai_supervisor.py`: 재기동·백오프·상한·장기 실행 초기화·표시 파일 무시·분할 가중치. `test_simai_scene.py`: 장면 자식 환경.
- `ai/tests/scene/test_supplement_capture.py`: 관문 표(14차 실측 행 포함)·REGISTERED_NOT_USED. `ai/tests/services/worker/test_site_setup_supplement.py`: 굽기 실패 FALLBACK.
- `ai/tests/config/test_sync.py`: 적용 로그 한 줄.
- 계약: `validate_examples.py`, Python 생성 모델 `--check`, Java `ContractExamplesRoundTripTest`.
- front: `supplement.test.ts`.
- gateway: Pester.

## 남은 위험

- 보강 촬영 등록률 자체(세로 영상 68% 크롭, 깊이 허용 ±5%, 묶음 40장)는 그대로다. 14차 영상은 관문만 바뀌어도 통과할 값이지만, 다른 영상은 `TOO_FEW_REGISTERED` 가 날 수 있다. 다음 세션에서 같은 영상으로 다시 잰다.
- Tier 0 재기동은 Pod 에서 kill -9 로 실측하지 않았다(가짜 프로세스 시험만).
- heal 의 관제 되살리기는 마지막 Pod 주소를 쓴다. Pod 를 바꾸고 `gw.sh live` 를 다시 주지 않으면 옛 주소로 30분에 3번까지 띄운다.
- 계약 판 번호(v7.37.0, site_setup 1.13.0, job_callback 2.20.0, job_heartbeat 2.12.0)는 동시에 진행 중인 다른 MR 과 겹칠 수 있다. 머지 순서에 따라 Lead 가 맞춘다.
