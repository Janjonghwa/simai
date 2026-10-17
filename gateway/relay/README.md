# gateway/relay — 노트북 영상 중계

노트북에서 Tapo C110 4대의 RTSP 를 카메라별로 받아, 디코딩하지 않은 압축 프레임(H.264 접근 단위)마다 I0 촬영 시각 헤더를
붙인다(S15P31A101-80). RunPod Tier 0 로 보내는 TLS 전송은 S15P31A101-83 이 이 프로젝트의 전송부 자리(`sink.py`)에 붙인다.

| 명세 | 이 도구에서 |
|---|---|
| 3.3 RTSP 접속 규칙 | 카메라당 RTSP 세션 1개(over TCP). 다시 열기 전에 반드시 닫는다 |
| 3.9 촬영 시각 추정·계약 | 녹화 도구와 같은 추정기(`simai_recorder.capture_time`). 프레임마다 `estimated_capture_time_ms`·`timestamp_source`·`timestamp_uncertainty_ms`, 원본 `received_at_ms`·`source_pts` |
| 12.7 I0 프레임 식별 | 헤더는 I0 `frame_header` 그대로. 재접속마다 `stream_epoch` +1, 새 epoch 는 `codec_info` 먼저, 키프레임부터 |
| 12.7 누락 | `camera_to_gateway_gap`: 도착 간격이 공칭 간격의 1.5배를 넘은 횟수·최대(RTP 간격도 따로). `laptop_heartbeat.cameras[]` 항목을 만든다 |

## 구조

`ai/` 와 분리된 독립 uv 프로젝트다. 입력(RTSP·파일·합성), 촬영 시각 추정, I0 헤더·코덱 정보 만들기는 `gateway/recorder`
(`simai-recorder`, 저장소 안 경로 의존)를 그대로 쓴다. 녹화 파일의 헤더와 LIVE 헤더가 같은 코드에서 나온다.

```
src/simai_relay/
  relay.py        카메라별 수신 루프(CameraRelay, 스레드 하나씩)와 여러 대 동시 중계(RelaySession)
  link_stats.py   카메라 → 노트북 구간 측정: 상태, camera_to_gateway_gap, 수신 지연(수신 − 추정 촬영 시각)
  sink.py         I0 기록(I0CodecInfo, I0Frame)과 전송부 인터페이스(CameraSink). 시험용 MemorySink, 점검용 CountingSink
  config.py       설정(TOML)·카메라 계정(.env) 읽기, 로그의 비밀 값 가리기(RedactSecrets)
  cli.py          simai-relay run
configs/          example.toml(실제 카메라), dry-run.toml(카메라 없이)
tests/            시험 (카메라 없이 돈다)
```

카메라마다 스레드 하나와 CameraSink 하나가 있고 카메라 사이에 공유하는 잠금·큐가 없다. 한 카메라의 수신이 멈추거나
전송이 막혀도 다른 카메라는 계속 돈다(시험 `test_one_camera_stall_does_not_block_others`).

## 설치와 시험

`gateway/relay/` 에서 실행한다.

```sh
uv sync
uv run pytest
```

시험은 카메라 없이 돈다. 입력은 PyAV 로 메모리에서 인코딩한 막대 무늬 H.264(사람 이미지 없음)와 그때그때 만든 로컬 파일이고,
지연·멈춤·끊김·재접속 실패를 넣는다. 계정 시험은 로컬 RTSP 서버 흉내(401 응답)에 접속해 계정이 로그·오류·상태에 나오지
않는지 본다. I0 헤더·코덱 정보·하트비트 항목은 저장소의 `contracts/` 스키마로 검증한다(`SIMAI_CONTRACTS_DIR` 로 바꿀 수 있다).

## 실행

```sh
uv run simai-relay run --config configs/dry-run.toml --seconds 30      # 카메라 없이
uv run simai-relay run --config configs/my-cameras.toml                # 실제 카메라, Ctrl+C 로 멈춤
```

- 카메라 계정은 저장소 루트 `.env` 의 `RTSP_USER`/`RTSP_PASS`(카메라별 `RTSP_USER_CAM_A` …)에서만 읽는다
  (`gateway/recorder/.env.example`). 설정 파일에 쓰지 않는다.
- 지금은 전송부가 없어 `CountingSink` 로 개수만 세고, 카메라별 상태·측정을 `--report-interval` 초마다 로그에 쓴다.
- C110 은 RTSP 동시 접속이 2세션이다. 중계 중에 녹화 도구·Tapo 앱·VLC 를 같은 카메라에 붙이면 한도를 넘길 수 있다.

## 측정

| 값 | 뜻 |
|---|---|
| `camera_to_gateway_gap.count`·`max_ms` | 같은 epoch 안 도착 간격이 공칭 간격 × 1.5(`threshold_ms`, 녹화 manifest 와 같은 기준)를 넘은 횟수와 최대 도착 간격 |
| `camera_to_gateway_gap.pts_gap_count` | RTP 타임스탬프 간격이 같은 기준을 넘은 횟수(카메라가 보내지 않은 프레임) |
| `receive_delay_ms` | 수신 시각 − 추정 촬영 시각(최근 250프레임 p50·p95, 전체 max). 고정 지연 + 기준보다 늦게 온 만큼 |
| 하트비트 `camera_to_gateway_gap_max_ms` | 직전 하트비트 이후 도착 간격 최대(`laptop_heartbeat` 계약). 새 프레임이 없으면 비운다 |

## 하지 않는 것

- 디코딩·재인코딩·얼굴 처리, 영상 저장(녹화는 `gateway/recorder`).
- RunPod 전송(TLS, 길이 접두, 기기 토큰, 공개키 고정, 송신 큐 상한): S15P31A101-83.
- 비밀 값 기록. RTSP 계정은 입력 안에만 있고, 로그에는 camera_id 와 계정 없는 입력 설명만 쓴다. 로그 처리기에는 `.env` 계정 값을
  `***` 로 바꾸는 필터를 덧붙인다.
