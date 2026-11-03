# 카메라 각도 헬퍼 계산 코어 — S15P31A101-445

4대 영상의 현재 겹침을 CPU에서 확인하는 독립 Python 3.11 uv 프로젝트다. 위치·자세 보정·위험 판정·LIVE 시작 허용 여부를 만들지 않는다. relay의 기존 미리보기에 연결하는 작업은 -446 범위다.

## 설치와 시험

이 폴더에서 실행한다. torch·torchvision은 CPU 전용 인덱스, LightGlue는 V0 환경과 같은 `5a650c444718caffc2669dc0094cde908f42dd73` 커밋으로 지정했다. upstream의 `opencv-python` 의존성은 uv에서 제외하고 `opencv-python-headless`로 cv2를 제공한다. 가중치는 내려받지 않는다.

```powershell
uv sync
uv run pytest                         # weights 표시 시험 제외
$env:SIMAI_CAMERA_HELPER_WEIGHT_TESTS='1'
uv run pytest -m weights              # 11차 실영상 + 로컬 가중치 수용 시험
uv run ruff check src tests
```

현재 제한 환경에서는 GitHub·PyPI 접속과 기존 V0 환경의 패키지 파일 읽기가 차단되어 `uv sync`·잠금 파일 생성·실측을 완료하지 못했다. `uv.lock`을 임의로 만들지 않았다. Lead가 이 worktree 안에 `uv sync`를 완료한 뒤 시험·측정을 이어가야 한다. 상세 결과는 [MEASURE.md](MEASURE.md).

의존성을 설치하기 전에도 표준 라이브러리 시험을 실행할 수 있다.

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

## -446에서 사용하는 API

```python
from pathlib import Path
from simai_camera_helper.judge import CoverageJudge, Params
from simai_camera_helper.matcher import CameraMatcher, load_intrinsics
from simai_camera_helper.message import CameraFrame, build_message

params = Params(image_width_px=1024, max_keypoints=1024)
matcher = CameraMatcher(
    Path("C:/Users/SSAFY/.cache/torch/hub/checkpoints/aliked-n16.pth"),
    Path("C:/Users/SSAFY/.cache/torch/hub/checkpoints/aliked_lightglue_v0-1_arxiv.pth"),
    params, load_intrinsics(Path("lens-intrinsics.json")), cpu_threads=4,
)
judge = CoverageJudge(params)  # source_id마다 하나, 라운드 사이 유지
# images: cam_A/B/C/D -> uint8 BGR ndarray; 영상이 없는 항목은 None.
pairs = matcher.match(images)
message = build_message(
    "gw-lobby", computed_at_ms,
    [CameraFrame(cid, latest_frame_ts_ms[cid]) for cid in images],
    pairs, judge,
)
```

시각은 호출자가 UTC ms로 준다. 영상 시각이 2초를 **초과**해 오래됐으면 STALE, 시각/영상이 없으면 NO_VIDEO다. 누락된 카메라가 낀 쌍은 UNKNOWN이고 수치·centroid는 null이다. 계산 중에도 미리보기 수신은 별도로 계속되어야 한다. `computed_at_ms` 시점의 최신 수신 상태를 전달하는 책임은 -446에 있다.

저장된 내부값이 없으면 `Params(intrinsics_source="DEFAULT", ransac_threshold_px=3.0)`와 `intrinsics=None`을 명시한다. DEFAULT는 수평 화각 90°·왜곡 0이라는 근사이며 현재 보정/실측값이 아니다. 저장된 렌즈 내부값 경로는 K·왜곡·영상 크기만 읽고 R·t·World는 읽지 않는다.

ALIKED 생성자가 로컬 경로를 받지 않아 모델 초기화 중에만 torch hub loader를 명시한 두 로컬 파일로 연결한다. 알 수 없는 요청은 오류로 거부하고 원래 loader를 복구한다. 프로세스 시작 때 다른 torch 모델 로딩 스레드보다 먼저 생성해야 한다. 가중치 누락·빈 파일·권한 오류는 모델 생성 전에 실패하며, 손상·호환되지 않는 가중치도 실패한다.

## 판정과 흔들림 방지

`.agy/helper-interface.md`의 필드명과 30/15·0.10 경계를 사용한다. 6쌍 모두 계산하되 특징점 추출은 카메라당 한 번이다. 해상도를 줄인 뒤 같은 비율로 K를 바꾸고 렌즈 왜곡을 편다. Essential RANSAC과 recoverPose의 양의 깊이를 모두 통과한 대응점만 inliers로 센다. centroid는 왜곡을 편 분석 영상의 폭·높이로 나눈 값이다.

`judge.py`와 `message.py`는 torch·numpy·cv2를 import하지 않는다. ENOUGH/WEAK 간선의 union-find로 성분을 구하며, UNKNOWN 간선을 모두 연결해도 하나가 될 수 없으면 DISCONNECTED를 확정한다. 가로 방향을 먼저 고르고 가장자리의 점이 있는 쪽으로 안내한다.

시작 상태는 UNKNOWN이다. 쌍 등급과 연결 상태는 각각 2라운드 연속 확인 후 바꾼다. 등급 변경 후보가 대기할 때는 이전 확정 수치·centroid도 함께 유지해 등급과 수치가 서로 다른 관측을 가리키지 않게 한다. 연결 상태도 확인 중에는 이전 상태가 남을 수 있으므로 이때 components는 현재 확정 간선의 성분을 나타낸다.

**Lead 확인 필요:** 공유 지시서에는 “NO_VIDEO/STALE 쌍은 UNKNOWN·수치 null”과 “등급·연결 상태는 2회 확인 후 변경”이 함께 있다. 현재는 영상 소실을 즉시 무효화하고, 복구와 실제 수치 등급 변경은 2회 확인한다. UNKNOWN 전환까지 늦추려면 소실 직후 메시지에 이전 정상 근거가 남는 상충을 먼저 정리해야 한다. -443 스키마는 이 worktree에 아직 없어 실제 계약 검증은 대기 중이다.

## 녹화 재생과 CPU 측정

```powershell
$env:CUDA_VISIBLE_DEVICES=''
uv run simai-camera-helper --matrix --out measurements/session11-cpu-new --rounds 20 --warmup 1
```

기본 입력은 지시서의 session11 `rec-calwalk/cam_{A,B,C,D}/epoch_0000.h264`, 내부값은 `lobby-bundle-rotfix/calibration.json`이다. 가중치는 위 두 파일을 기본으로 하며 CLI 인자로 다른 로컬 경로를 줄 수 있다. `--start`와 `--stride`는 키프레임 순번 기준이다. NONKEY skip으로 순차 디코딩하고 **선택한 한 라운드 4장만 보관**한다. 전체 영상이나 전체 키프레임 배열을 만들지 않는다.

`--matrix`는 960/1024 px × 1024/2048점 네 조합을 **새 프로세스로 순차** 실행한다. 모델 초기화 시간은 별도, warmup은 기본 1회 제외, 측정은 서로 다른 키프레임 20라운드다. `round_ms`는 resize·왜곡 보정·4회 추출·6쌍 매칭·Essential/recoverPose·판정·JSON 객체 생성을 포함한다. 디코딩 포함 시간도 별도로 남긴다. 최대 RAM은 프로세스 RSS를 20 ms 간격으로 표본화하고 Windows peak working set / Unix ru_maxrss와 최댓값을 취한다. 모델 로딩 RAM도 포함한다. 표본 수 20의 p95는 소규모 현장 시험 값이며 서비스 지연 보증이 아니다. torch/OpenCV 스레드는 초기값 4개이고 디코더는 카메라당 1개다.

각 출력 폴더에는 `rounds.jsonl`, `summary.json`, `run_record.json`이 생기고 최상위 `benchmark.json`에 네 조합을 모은다. 모델/의존성 로딩에 실패해도 실패 요약과 Run 기록을 남기고 종료 코드 1을 돌려준다. 이미 결과가 있는 `--out`은 덮어쓰지 않는다. Run은 VAL-01 도구 검증으로 기록하며 dirty worktree이므로 리뷰/성능 근거로 승격하지 않는다. Docker가 없어 `env_hash`는 비워 둔다.

원본 H.264에는 UTC 촬영 시각·카메라 간 동기화 정보가 없다. 오프라인 메시지는 **동시에 재생한 4장에 대한 고정 합성 시계**를 사용하며 `rounds.jsonl`의 `timestamp_basis`로 구분한다. 내부 메시지의 skew=0은 재생 배치 시계의 값이고 원본 촬영 skew가 아니다(`recorded_capture_skew_ms=null`). 이 출력을 LIVE API로 올리지 않는다. 실제 녹화의 시간 동기·LIVE 반영 지연을 이 시험으로 입증하지 않는다.

회전 수용 시험은 왜곡을 편 cam_C 키프레임에 `K R K^-1`로 yaw +10°/+20°를 적용한다. 점 이동의 RIGHT 부호는 별도 기하 시험에서도 확인한다. 실영상 시험은 등급 하락·cam_C 방향을 실제 출력으로 검증하도록 두었으나 아직 실행하지 못했다. 워핑은 새로 드러나는 배경을 만들 수 없으므로 실제 카메라 회전 성능과 구분한다. 실패하면 문턱값을 낮춰 통과시키지 않고 산출된 수치로 원인을 확인해야 한다.

상류 구현 참조: [고정 커밋 ALIKED](https://github.com/cvg/LightGlue/blob/5a650c444718caffc2669dc0094cde908f42dd73/lightglue/aliked.py), [고정 커밋 LightGlue](https://github.com/cvg/LightGlue/blob/5a650c444718caffc2669dc0094cde908f42dd73/lightglue/lightglue.py). 버린 대안은 전체 영상 적재, 자동 가중치 다운로드, 기존 extrinsics에 의한 매칭 게이트, 두 OpenCV 배포판 동시 설치다.
