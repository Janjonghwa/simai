# gateway/recorder — 4대 동기화 녹화 도구

노트북에서 Tapo C110 4대의 RTSP 를 동시에 받아, 디코딩하지 않은 압축 프레임과 프레임별 촬영 시각 헤더를 파일로 남기고
녹화 manifest 를 만든다(S15P31A101-107). 명세 15.3 시나리오 데이터셋을 찍는 도구이고, 녹화 파일은 Tier 0 의
녹화 입력(FrameSource, 명세 20.4)으로 다시 읽는다.

| 명세 | 이 도구에서 |
|---|---|
| 3.3 RTSP 접속 규칙 | 카메라당 RTSP 세션 1개(over TCP). 다시 열기 전에 반드시 닫는다 |
| 3.9 촬영 시각 계약 | 프레임마다 `estimated_capture_time_ms`·`timestamp_source`·`timestamp_uncertainty_ms`. C110 기본 `RECEIVE_ESTIMATED` |
| 12.7 I0 프레임 식별 | 헤더는 I0 `frame_header` 그대로. 재접속마다 `stream_epoch` +1, 새 epoch 는 키프레임부터 |
| 11.5 보관과 개인정보 | RTSP 녹화는 동의·보관 기한 없이는 시작하지 않는다. 사람이 찍히면 동의서 ID·범위 필수. 수업 중 우연히 찍힌 영상은 `[consent] purpose = "dev_test"` 로 내부 개발·테스트에만 쓴다(동의서 ID 없이 가능, 보관 기한 필수, 시연·발표·보고서·공개 자료 사용 금지) |
| 21.1 저장소 구조 | 영상은 Network Volume 에, 저장소에는 manifest 만 ([`docs/data/recording-manifest.md`](../../docs/data/recording-manifest.md)) |

## 구조

`ai/` 패키지와 분리된 독립 uv 프로젝트다(`pyproject.toml`, `uv.lock`). 노트북 게이트웨이에는 PyAV 와 jsonschema 만 있으면 되고
`simai_ai`(Tier 0·Risk Engine)를 설치할 필요가 없다. `simai_ai` 를 import 하지 않으며 `ai/` 도 이 패키지를 import 하지 않는다.
두 쪽을 잇는 것은 파일 형식(아래)과 계약(`contracts/messages/frame_header`, `codec_info`, `contracts/data/recording_manifest`)뿐이다.
manifest 검증은 저장소 루트의 `contracts/` 스키마를 직접 읽는다(복사하지 않는다). 저장소 밖에서 돌리면 `SIMAI_CONTRACTS_DIR` 로 위치를 준다.
스키마를 찾지 못하면(저장소 밖 실행, 틀린 `SIMAI_CONTRACTS_DIR`) 모든 명령이 녹화·검사 전에 `설정 오류: ...` 를 알리고 종료 코드 2 로 멈춘다.

```
src/simai_recorder/
  sources.py       입력: RtspSource(실제 카메라), FileSource(로컬 영상), SyntheticSource(합성), FaultySource(지연·끊김 흉내)
  recorder.py      카메라별 녹화 루프(스레드 하나씩)와 4대 동시 세션
  capture_time.py  촬영 시각 추정 (RECEIVE_ESTIMATED)
  i0.py            I0 기록(codec_info, frame_header) 만들기. gateway/relay 와 같이 쓴다
  segment.py       세그먼트 쓰기·읽기 (녹화 형식)
  manifest.py      manifest 계산·검증(계약 스키마 + 스키마 밖 규칙)·해시 확인
  contract.py      contracts/data/recording_manifest.schema.json 검증 (오류 문장에 받은 값을 넣지 않음)
  config.py        세션 설정(TOML)과 카메라 계정(.env)
  clock.py         주입하는 시계 (코드가 현재 시각을 직접 읽지 않는다)
sessions/          세션 설정 예시 (example.toml: 실제 카메라, dry-run.toml: 카메라 없이 시험)
tests/             시험 (카메라 없이 돈다)
```

## 설치와 시험

`gateway/recorder/` 에서 실행한다.

```sh
uv sync
uv run pytest
```

시험은 카메라 없이 돈다. 시험용 영상은 그때그때 만든 막대 무늬 H.264 이고(사람 이미지 없음), 4대 동시 녹화 시험은
합성 스트림 3대 + 로컬 파일 1대에 지연·멈춤·끊김·재접속 실패를 넣어 실제 시계로 약 3초 돈다.
시험은 저장소의 `contracts/` 스키마와 예시(`contracts/examples/recording_manifest*.json`)를 읽는다.
다른 브랜치의 스키마로 돌리려면 `SIMAI_CONTRACTS_DIR=<경로>/contracts uv run pytest` 를 준다.

## 실행

### 카메라 없이 (합성·로컬 파일)

```sh
uv run simai-recorder record --config sessions/dry-run.toml --seconds 30
```

로컬 영상은 `kind = "file"` 로 넣는다(H.264 raw·MP4·MKV). 실시간 속도로 흘리고, 끊김을 흉내 내면 끊긴 자리에서
이어서 내므로 재접속 직후 키프레임 전 패킷을 버리는 경로까지 시험된다. 이런 세션의 manifest 는 `is_test_input: true` 다.

### 실제 카메라 (수동 실행)

1. 저장소 루트 `.env` 에 카메라 계정을 넣는다(`gateway/recorder/.env.example` 참고, 커밋 금지).
   `RTSP_USER`, `RTSP_PASS` 가 4대 공통이고, 카메라마다 다르면 `RTSP_USER_CAM_A` 처럼 쓴다.
2. 카메라 설정을 확인한다(명세 3.3): 4대 25 fps 고정, 워터마크·UPnP·동작 감지 끄기, 펌웨어 버전 기록.
3. `sessions/example.toml` 을 복사해 host·firmware·시나리오·동의(`[consent]`)를 채운다.
4. **녹화 중에는 Tapo 앱 실시간 화면이나 VLC 로 카메라에 접속하지 않는다.** C110 은 RTSP 동시 접속이 2세션이다.
5. 실행한다. 녹화 시간이 끝나거나 Ctrl+C 를 누르면 멈추고 manifest 를 쓴다.

```sh
uv run simai-recorder record --config sessions/my-session.toml --seconds 600
```

출력 기본 위치는 `ai/experiments/recordings/<session_id>/` 이고 `.gitignore` 의 `recordings/` 로 커밋되지 않는다.
끝나면 세션 디렉터리를 Network Volume(`manifest.storage.uri`)에 올리고, 올린 쪽에서 해시를 확인한다.

```sh
uv run simai-recorder verify <세션 디렉터리>     # manifest 의 sha256·크기와 파일 비교
uv run simai-recorder manifest <세션 디렉터리>   # 중간에 끊긴 세션은 파일에서 manifest 를 다시 계산
```

## 녹화 형식

```
<session_id>/
  session.json                        세션 설정 스냅숏 (계정 없음). manifest 를 다시 계산할 때 쓴다
  manifest.json                       contracts/data/recording_manifest.schema.json (설명 docs/data/recording-manifest.md)
  <camera_id>/
    events.jsonl                      접속·끊김·재접속 실패·키프레임 전 버림 기록
    epoch_NNNN.codec_info.json        I0 codec_info (contracts/messages/codec_info.schema.json)
    epoch_NNNN.h264                   H.264 Annex B. 받은 접근 단위를 받은 순서대로 이어 붙임
    epoch_NNNN.frames.jsonl           한 줄 = 한 프레임
```

`stream_epoch` 하나가 세그먼트 하나다. 접속에 성공할 때마다 epoch 가 하나 늘고(접속했지만 키프레임을 받기 전에 끊기면
그 epoch 의 파일은 없다), `frame_sequence` 는 epoch 안에서 0 부터 센다. `(camera_id, stream_epoch, frame_sequence)` 가
프레임의 유일 식별자다(명세 12.7).

`frames.jsonl` 한 줄(계약 `recording_manifest.schema.json#/$defs/frame_line`):

```json
{"header":{"camera_id":"cam_A","stream_epoch":0,"frame_sequence":0,"source_pts":0,"pts_timebase":"1/90000",
 "estimated_capture_time_ms":1792204852445,"timestamp_source":"RECEIVE_ESTIMATED","timestamp_uncertainty_ms":20.0,
 "keyframe":true,"received_at_ms":1792204852445},"offset":0,"size":873}
```

- `header` 는 I0 프레임 헤더 계약(`contracts/messages/frame_header.schema.json`) 그대로다. `source_pts` 를 모르면 null.
- `.h264[offset : offset+size]` 가 그 프레임의 압축 바이트(받은 그대로)다. 첫 키프레임에 SPS/PPS 가 없으면 파일 앞에 붙이므로
  첫 `offset` 이 0 보다 클 수 있다. 접근 단위를 먼저 쓰고 사이드카 줄을 쓰므로 중간에 멈춰도 줄이 가리키는 바이트는 파일에 있다.
- 세그먼트는 단독으로 디코딩된다(키프레임으로 시작). PyAV(`format="h264"`)와 OpenCV `VideoCapture` 모두 읽는다.
  사이드카 줄 순서 = 파일 안 접근 단위 순서 = 디코딩 순서다(C110·시험 영상은 B 프레임 없음).

### 촬영 시각 (명세 3.9)

C110 은 RTCP Sender Report 카메라 시각이 ±400ms 흔들려 쓰지 않는다. 노트북 수신 시각(`received_at_ms`)과 카메라 PTS 로 추정한다.

```
offset    = 최근 window_ms 동안 min(received_at_ms − pts_ms)     # 가장 덜 늦게 온 프레임 기준
estimated = pts_ms + offset − latency_ms
```

PTS 간격(카메라 RTP 시계)은 믿고 원점만 수신 시각에 맞추므로 Wi-Fi 도착 지터(-9 실측 p95 약 41ms)가 들어가지 않는다.
창 안 최솟값을 쓰므로 카메라·노트북 시계의 느린 표류를 따라간다. PTS 가 없으면 `received_at_ms − latency_ms`.
`latency_ms`(노출 → 수신 고정 지연)와 `timestamp_uncertainty_ms` 는 **G0 LED 시험 전 잠정값**이다(기본 0, 20, PTS 없음 45).
원본 `received_at_ms`·`source_pts` 를 함께 남기므로 G0 에서 값이 정해지면 사이드카를 다시 계산할 수 있다.
RTSP 입력은 RTCP SR 로 환산한 카메라 시각도 `SourcePacket.camera_time_us`(SR 전·파일·합성은 None)로 넘기지만 녹화·중계는 쓰지 않는다.
이 값의 유무·흔들림은 측정 도구 [`gateway/rtsp_probe`](../rtsp_probe/README.md) 가 잰다.

### Tier 0 에서 다시 읽기

방법은 두 가지다. 둘 다 연결돼 있다(1 은 S15P31A101-215, 2 는 S15P31A101-158).

1. **I0 재생(권장, Tier 0 파일 입력).** Tier 0 설정의 `input` 을 `{"mode": "file", "session_dir": <세션 디렉터리>, ...}` 로 두면
   `simai_ai.io.RecordingReplay` 가 세그먼트마다 `codec_info` → `(header, 압축 프레임)` 순서로 I0 메시지를 만들어 LIVE 와 같은 `I0Ingest` 에 넣는다.
   프레임별 촬영 시각·epoch·키프레임이 LIVE 와 같은 경로로 들어간다. 판정 출처는 REPLAY 이고 I1·I0 제어 채널을 열지 않으며(실제 알림 없음),
   `input.jsonl_dir` 을 주면 `world_state.jsonl`·`risk_event.jsonl` 을 남긴다(명세 21.3). 실행 방법·시계·종료 코드는
   [`docs/design/tier0-recording-replay.md`](../../docs/design/tier0-recording-replay.md). 읽는 순서는
   `simai_recorder.segment.read_segment` 와 같다(ai 는 gateway 를 import 하지 않으므로 형식만 따른다).
2. **VideoFileFrameSource.** `VideoFileFrameSource.from_manifest(<세션 디렉터리>, epochs={...})` 가 manifest 를 녹화 도구와 같은 판정(계약 스키마 + 스키마 밖 규칙)으로 검사하고
   카메라마다 `Recording(camera_id, path=epoch_NNNN.h264, start_time_ms=세그먼트 첫 추정 촬영 시각, fps=settings.nominal_fps)` 를 만든다.
   간단하지만 촬영 시각을 `start + index/fps` 로 다시 만들므로 도착 끊김·epoch 사이 공백이 사라진다. 프레임별 시각이 필요하면 1 을 쓴다.

## 하지 않는 것

- 디코딩·재인코딩·얼굴 처리. 원본 압축 프레임을 그대로 남긴다(사람이 찍힌 녹화는 동의 범위 안에서만 다룬다, 명세 11.5).
- 영상·사람 이미지 커밋. 출력은 `.gitignore` 된 곳에 쓰고, 저장소에는 manifest 만 둔다.
- 비밀 값 기록. RTSP 계정은 `.env` 에서만 읽고 `session.json`·`manifest.json`·`events.jsonl`·오류 메시지에 넣지 않는다.
- 노트북 → RunPod I0 전송. 이 도구는 녹화만 한다(LIVE 수신·헤더·전송은 [`gateway/relay`](../relay/README.md) 몫).
