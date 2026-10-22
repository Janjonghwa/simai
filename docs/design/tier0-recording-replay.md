# Tier 0 녹화 재생(file 입력)과 world_state·risk_event JSONL (S15P31A101-215)

gateway/recorder 가 만든 녹화 세션(카메라별 H.264 세그먼트 + 프레임 헤더 사이드카 + manifest)을 Tier 0 에 LIVE 와 같은 경로로
넣고, 판정 결과를 파일로 남긴다. 서버 I1·STOMP 없이 뷰어 3D 재생(S15P31A101-187 "world_state JSONL 파일 재생 Source")으로
넘기기 위한 입력·출력이다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 5.1 입력 추상화 | Tier 0 는 중계 스트림·녹화 파일·시뮬레이터 출력을 같은 형식으로 받는다 | 녹화를 I0 메시지로 다시 만들어 LIVE 의 I0Ingest 에 넣는다 |
| 12.7 I0 | (camera_id, stream_epoch, frame_sequence), 코덱 정보 → 키프레임, 재접속 | 세그먼트 = epoch, 세그먼트 사이는 연결 끝 |
| 21.3 시나리오 재생 | "녹화·시뮬레이터 입력을 파일 모드 Tier 0 에 넣어 … 시계는 주입식으로 고정해 결과가 매번 같게" | 입력 모드 이름 `file`, 시계는 녹화 시각 |
| 2장 원칙 10, 7장 출처, 7.4, 부록 E | REPLAY 출처는 같은 규칙으로 계산하되 실제 알림 채널로 나가지 않는다 | 판정 출처 REPLAY, I1·I0 제어 채널을 열지 않음 |
| 20.4 입력 대체 계약 | frame_source `FILE`, input_provenance | world_state.input_provenance.frames.source = FILE |
| 11.5 보관과 개인정보 | dev_test 녹화는 내부 개발·테스트에만 | dataset_id `recording-dev_test/<session_id>`, 출력도 커밋하지 않음 |
| 13.4 최소 리플레이(M3) | 시각 슬라이더로 world_track_state·risk_event 재생 | JSONL 두 파일이 그 재료 |

## 흐름

```
manifest.json (contracts/data/recording_manifest, 계약 검사)
  └─ RecordingReplay (simai_ai.io.sources)
       세그먼트마다 파일 크기·sha256·codec_info·프레임 줄(카메라·epoch·frame_sequence 연속·offset 범위·줄 수) 확인
       카메라마다 [codec_info → (header, .h264[offset:offset+size]) …] (+ 다음 세그먼트가 있으면 연결 끝), 카메라 사이는 수신 시각 순
  └─ FileInput.pump (services/tier0/file_input.py, 판정 스레드)
       재생 시계(ReplayClock)가 지난 메시지 → I0Ingest.receive (LIVE 와 같은 CameraIngest·PyAV CPU(기본)/NVDEC(선택)·latest-frame-wins)
판정 tick: DetectorFeed(source FILE) → 검출기 → MultiCameraLocalTracker → 카메라 간 연결 → World Track → 접지
           → ActiveConfig.evaluate(REPLAY) → LifecycleTracker(REPLAY)
  ├─ world_state.jsonl  : 봉투 그대로의 world_state 한 줄씩 (msg_id = 그 tick 의 evidence_ref)
  └─ risk_event.jsonl   : 확정 전이마다 risk_event(뷰어 projection) 한 줄, source REPLAY
```

## 정한 것과 근거

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 입력 모드 이름 | `input.mode = "file"` | 명세 21.3 "파일 모드 Tier 0", frame_source `FILE`(20.4). `replay` 는 판정 출처(REPLAY) 이름과 겹친다 |
| 읽는 방법 | I0 재생(gateway/recorder README "Tier 0 에서 다시 읽기" 1) | 프레임별 촬영 시각·epoch·키프레임을 헤더 그대로 쓴다. VideoFileFrameSource(방법 2)는 시각을 start + index/fps 로 다시 만들어 도착 끊김·epoch 공백이 사라진다 |
| 메시지를 넣는 시각(release) | 헤더 `received_at_ms`(노트북 수신), 필드가 없을 때만 촬영 시각. 있는데 UTC ms 정수가 아니면(문자열·null·음수) 열기를 거절한다. 코덱 정보는 그 세그먼트 첫 프레임과 같은 시각 | LIVE 에서 Tier 0 가 프레임을 받는 시각에 가장 가깝다(중계 지연은 녹화에 없다). 손상된 수신 시각을 촬영 시각으로 메우면 release·T_lag·표본 선택이 바뀐다(AGENTS 규칙 2) |
| 촬영 시각·timestamp_source | 헤더 값 그대로(C110 은 RECEIVE_ESTIMATED) | 입력 종류(FILE)는 input_provenance 에 싣는다. timestamp_source 는 프레임별 추정 방법이라 FILE 로 바꾸지 않는다(지어내지 않음) |
| 시계 | `ReplayClock`: 원점 = 녹화의 첫 release 시각. `speed: "max"`(기본)는 판정 tick 마다 tick_ms 씩만 나아간다. 수는 실제 경과 × 배속 | 명세 21.3 "시계는 주입식". 최대 속도는 실제 시각에 기대지 않아 같은 녹화·설정이면 world_state 가 매번 같다(시험 `test_max_speed_replay_is_deterministic`) |
| 펌프 위치 | 판정 스레드에서 tick 직전 | 수신 스레드가 없다. 같은 스레드라 최대 속도에서 순서가 정해진다 |
| 보간 창 | `reorder_window_ms = 0` | 녹화는 frame_sequence 가 이어진다(열 때 확인). LIVE 와 같은 값 |
| 카메라 연결 상태(R7·health) | 세그먼트 코덱 정보가 들어오면 연결, 다음 세그먼트가 있는 세그먼트가 끝나면 끊김(I0Ingest.connection_lost). 마지막 세그먼트 끝은 끊김으로 바꾸지 않음 | 재접속 공백은 녹화에 실제로 있던 장애다. 녹화 끝은 카메라 장애가 아니다(프레임이 없으면 camera_stall_ms 가 공통 시각에서 뺀다) |
| 끝 판정 | 녹화를 다 넣고 디코더를 비운(I0Ingest.close) 뒤 추론 대기열이 비면 `Tier0Service.finished`. 진입점은 정상 종료(0) | 마지막 프레임까지 판정에 넘긴 뒤 끝낸다 |
| 판정 출처 | REPLAY (`service.INPUT_ORIGINS`) | 부록 E: 녹화 재생은 REPLAY 출처, 실제 알림 없음. RiskEvent·risk_event 줄의 source 가 LIVE 와 구분된다 |
| 실제 알림 채널 차단 | (1) 설정이 I1·I0 제어 채널을 만들지 않는다(환경 변수가 있어도) (2) 7.3 표(alerting.channels_for)가 REPLAY 에는 채널을 주지 않아 local_alert 신호가 생기지 않는다 (3) local_alert 계약·site_alerts 가 LIVE 만 받는다 | AGENTS 규칙 5. 시험은 판정 루프의 송신 자리(send_i0)를 가로채 0건을 확인하고, 출처만 LIVE 로 되돌리면 같은 경로에서 local_alert 가 나가는 것을 음성 대조로 둔다 |
| 저널 | REPLAY 전이는 Tier 0 저널에 넣지 않는다(judge 가 LIVE 일 때만 submit_step) | 저널·risk_event_transition 계약은 LIVE 전용이다(journal.py 가 거부). 재생은 다시 돌릴 수 있어 내구성 기록이 필요 없다 |
| 상태 위치 | `state_dir/replay/run-<UTC>-<8자>/` (저널·체크포인트·표시 파일·구성 보관소) | LIVE 의 `state_dir/journal`·`tier0.live` 와 섞이지 않는다. 실행마다 새 디렉터리라 이전 재생의 체크포인트 Track 을 되살리지 않는다(결과가 매번 같게) |
| world_state 줄 | `Envelope.make("world_state", payload, msg_id=그 tick 의 evidence_ref)`, sent_at = 재생 시계 | I1 과 같은 메시지를 파일에만 쓴다. 각 줄이 contracts/messages/world_state 를 통과한다 |
| risk_event 줄 | 확정 전이마다 `risk_event` 2.1.0 봉투, payload = 전이 뒤 상태의 뷰어 projection(rationale·proposed_severity null, acks 빈 목록, delivery 없음) | 뷰어(-187)가 이벤트도 그린다. risk_event_transition 은 source 가 LIVE 고정이라 REPLAY 를 실을 수 없다. risk_event 는 REPLAY 를 받고, delivery 는 LIVE 에만 있는 필드다 |
| 파일 쓰기 | 줄마다 flush, fsync 없음. 같은 이름 파일이 있으면 시작하지 않는다(종료 코드 2) | 덮어쓰지 않음(AGENTS 규칙 6). 실행 중에도 읽을 수 있게 |
| JSONL 을 켜는 입력 | file 입력만(`input.jsonl_dir`) | LIVE 판정 lock 안에서 디스크를 기다리지 않게 한다. SIM 은 필요하면 다음 티켓 |
| 시작 거절 | 녹화 manifest 계약 위반·세그먼트 파일 불일치·고른 카메라가 manifest 에 없음 → 4. 보정 경로 없음 → 설정 오류 2. 보정 문서를 못 읽음(외부 파라미터 R·t 없음 포함)·녹화 카메라가 보정에 없음 → 2. 고른 디코더(nvdec) 없음·검출기 없음 → 4 | 모르면 지어내지 않는다. 기존 종료 코드 체계(2 설정, 4 입력·검출기) 그대로 |
| 재생 중 입력 실패 | 녹화 읽기 실패·녹화 끝 디코더 비우기(PyAV flush DecodeError) 실패는 `FileInput.failed` 에 남기고 그 tick 에 재생을 끝낸다(finished). 진입점 종료 코드 4 | 남은 구간을 지어내지 않는다. 판정 스레드가 예외로 죽으면 진입점이 무기한 기다리므로 실패도 끝 신호로 잇는다. LIVE 의 tick 오류 지속 정책은 그대로 |
| 결과 기록 실패 | world_state·risk_event 쓰기·flush 가 실패하면(디스크·권한) `JsonlOutput.failed` 에 파일·줄 번호·이유를 남기고 그 뒤로는 쓰지 않는다. 이미 쓴 줄은 그대로, 쓸 수 있으면 `replay_failed.json` 에 같은 내용. 재생을 실패로 끝내고 종료 코드 5 | 위험 이벤트를 잃은 출력을 정상 완료(0)로 보고하지 않는다 |

### 바꾼 기존 코드

- `JudgmentLoop`: 판정 출처를 `Origin.LIVE` 고정 대신 `tracker.origin` 으로, LIVE 가 아니면 저널에 넣지 않음, 확정 전이 사본 훅
  `record_transitions`.
- `DetectorFeed`: `provenance` 인자(녹화 dataset_id 를 싣는다).
- `codec_info_from_contract`·`frame_from_contract` 를 `services/tier0/live_input.py` 에서 `simai_ai.io.messages` 로 옮겼다
  (녹화 재생이 같이 쓴다. src 는 services 를 import 하지 않는다).
- `VideoFileFrameSource.from_manifest` 의 dataset_id 규칙을 `recording_dataset_id` 로 뽑아 같이 쓴다.

## 실행 (노트북)

`ai/` 에서. 검출기는 TensorRT 엔진이 없으면 `SIMAI_TIER0_WEIGHTS` 의 .pt 를 ultralytics CPU 로 연다(느리다, 운영 기준 아님).

```sh
cd ai
mkdir -p experiments/runs/replay-classroom
# 설정 파일 (상대 경로는 설정 파일 위치 기준). runs/ 는 .gitignore 대상이라 설정·상태·출력이 커밋되지 않는다
cat > experiments/runs/replay-classroom/tier0.json <<'JSON'
{
  "gateway_id": "tier0-replay",
  "state_dir": "state",
  "scene_version": "scene-0",
  "calibration_file": "<-214 보정 문서(외부 파라미터 포함) 경로>.json",
  "input": {"mode": "file",
            "session_dir": "../../recordings/rec-20261021-031045z-classroom-dev-test",
            "decoder": "cpu", "speed": "max",
            "jsonl_dir": "out-1"},
  "bootstrap": {"archive": "<구성 묶음 보관소>", "config_version": "<cfg>"},
  "gpu": {"reserve_mb": 0}
}
JSON
SIMAI_TIER0_WEIGHTS=<yolo26n-pose.pt 경로> SIMAI_TIER0_IMGSZ=640 \
  uv run --extra tier0 python -m services.tier0 --config experiments/runs/replay-classroom/tier0.json
```

- 끝나면 `services.tier0: 녹화 재생 끝 (N건, world_state …줄·risk_event …줄)` 을 찍고 종료 코드 0 으로 끝난다.
  재생 도중 입력 실패면 4, 결과 기록 실패면 5(`jsonl_dir/replay_failed.json` 에 실패 지점)다. 이때 출력은 불완전하다.
  다시 돌릴 때는 `jsonl_dir` 을 새 이름으로 바꾼다(있는 파일은 덮어쓰지 않고 종료 코드 2).
- 출력 `experiments/runs/replay-classroom/out-1/{world_state,risk_event}.jsonl`. dev_test 녹화에서
  나온 출력도 사람 위치를 담으므로 커밋하지 않고 시연·공개 자료에 쓰지 않는다(명세 11.5).
- 구성 묶음(bootstrap)이 없으면 world_state 만 나오고 위험 판정은 하지 않는다(health NO_ACTIVE_CONFIG).
- 보정 문서(외부 파라미터 포함)가 없으면 `시작하지 않았다: 보정 문서를 읽지 못했다 …` 로 종료 코드 2 다. 지금 교실 녹화는
  내부 파라미터 결과(`intrinsics-20261021-022940z.json`)만 있고 외부 파라미터가 없어(-214) 여기서 멈춘다.
- Pod 에서도 디코더는 기본 `cpu`(-236), 검출은 엔진(`SIMAI_TIER0_ENGINE`)으로 돈다. LIVE 관제 시간에는 돌리지 않는다(GPU 는 Tier 0 LIVE 가 먼저).

시험: `uv run pytest tests/services/tier0/test_tier0_file_replay.py tests/io/test_recording_replay.py -q`
(합성 녹화: 프레임 번호를 화소로 새긴 H.264, SIM 장면·SIM 보정, 검출기·추적기 대역. SIM 상한 결과다).

## 버린 대안

- **VideoFileFrameSource(OpenCV) 로 읽기.** 시각을 start + index/fps 로 다시 만들어 녹화의 실제 촬영 시각·끊김·epoch 공백이
  사라지고, LIVE 의 디코딩·최신성 경로를 지나지 않는다.
- **녹화를 I0 TLS 로 흘려 LIVE 입력 그대로 받기.** 영상 포트·TLS·토큰이 필요하고 최대 속도 재생과 결정적 시계를 할 수 없다.
- **출처를 LIVE 로 두고 출력만 막기.** RiskEvent 가 LIVE 로 남아 출처 구분이 안 되고(규칙 5·7), 저장한 저널을 누가 outbox 에
  물리면 실제 알림으로 나갈 수 있다.
- **저널이 REPLAY 를 받게 바꾸기.** 저널·risk_event_transition 은 LIVE 전용 계약이다. 재생 결과는 JSONL 로 충분하다.
- **risk_event_transition 형식으로 쓰기.** source 가 LIVE 고정이라 REPLAY 줄이 계약을 통과하지 못한다.
- **재생 펌프 별도 스레드(실시간 배속).** LIVE 수신 스레드와 비슷하지만 순서가 시각에 따라 달라진다. 판정 스레드 하나로 충분하다.

## 잔여 위험

- 실시간 배속 재생은 판정 tick 이 실제 시간 tick_ms 마다라 녹화 시각으로는 tick_ms × speed 마다 표본을 뜬다. 배속이 크거나
  CPU 검출기가 느리면 latest-frame-wins 로 프레임을 많이 건너뛰고 World Track 이 이어지지 않을 수 있다(LIVE 과부하와 같은 모양).
  결과를 비교하려면 최대 속도를 쓴다.
- 최대 속도는 판정 tick 마다 카메라당 한 장(latest-frame-wins)이라 녹화 25 fps 중 10 Hz 만 판정에 쓴다(LIVE 판정 주기와 같음).
- 재생 시계는 Tier 0 와 노트북이 같은 시계라 시계 차이(SAME_CLOCK)로 본다. LIVE 의 시계 교환 불확실성은 재현되지 않는다.
- 녹화 헤더의 촬영 시각은 G0 전 잠정 추정값이다(gateway/recorder README). 재계산한 사이드카가 나오면 그것으로 다시 돌린다.
- world_state 계약에는 판정 출처 필드가 없다. 파일 안에서 REPLAY 결과임은 input_provenance.frames.source = FILE(모든 줄에
  싣는다)과 risk_event.source = REPLAY 로만 구분된다. 파일을 읽는 쪽(-187 뷰어 재생)은 이 출처를 지우거나 LIVE(I1·STOMP)
  입력과 섞지 않아야 한다.
- 상태 디렉터리(`state_dir/replay/run-*`)는 실행마다 남는다(작다). 지우는 도구는 없다.
- 교실 녹화(4대 × 약 4480 프레임, 2304×1296)를 CPU 로 디코딩·검출하는 처리 시간은 재지 않았다.

## 남은 일

- 교실 녹화 실제 실행: -214 의 외부 파라미터가 든 보정 문서(contracts/providers/calibration, 카메라 ID cam_A~cam_D)와 교실 구성 묶음
  (구역이 없으면 world_state 만)이 있어야 한다. 노트북 CPU(ultralytics 작은 모델) 또는 Pod(엔진, CPU 디코딩) 중 하나로 돌려 JSONL 을
  뷰어 재생(-187)에 넘긴다.
- `gateway/recorder/README.md` "Tier 0 에서 다시 읽기"(1 은 아직 없다)와 `docs/data/recording-manifest.md` "Tier 0 에서 읽기"에
  이 경로를 적는다(이 티켓 범위 밖 파일).
- -172 명세 정리: 3.2 운영 EC2 REPLAY 계산과 21.3 오프라인 Tier 0 파일 재생 구분. 이 문서의 파일 재생은 21.3 의 개발·시험
  경로이고, EC2 옆 컨테이너의 운영 REPLAY 계산을 Tier 0 로 옮기는 정식 제품 배치 결정이 아니다(결과를 I1 로 보내지 않는다).
