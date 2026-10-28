# gateway/relay — 노트북 영상 중계

노트북에서 Tapo C110 4대의 RTSP 를 카메라별로 받아, 디코딩하지 않은 압축 프레임(H.264 접근 단위)마다 I0 촬영 시각 헤더를
붙인다(S15P31A101-80). `--send` 면 카메라마다 TLS 연결 하나로 RunPod Tier 0 에 보낸다(S15P31A101-83, `tls_sink.py`).
`--preview-port` 면 같은 압축 프레임을 127.0.0.1 WebSocket 으로 같은 노트북의 관제 앱 CCTV 화면에 넘긴다(S15P31A101-310, `preview.py`).

| 명세 | 이 도구에서 |
|---|---|
| 3.3 RTSP 접속 규칙 | 카메라당 RTSP 세션 1개(over TCP). 다시 열기 전에 반드시 닫는다 |
| 3.9 촬영 시각 추정·계약 | 녹화 도구와 같은 추정기(`simai_recorder.capture_time`). 프레임마다 `estimated_capture_time_ms`·`timestamp_source`·`timestamp_uncertainty_ms`, 원본 `received_at_ms`·`source_pts`. 카메라 PTS 시각선이 깨지면(C110 묶음 점프) 그 epoch 를 프레임 시계로 매기고 WARNING `pts_timeline_broken` 을 남긴다. 촬영 시각은 epoch 안에서 앞으로만 간다: 직전 값 + 1 ms 보다 작으면 그 값으로 올리고 불확실성을 올린 양만큼 키우며, 올린 epoch 는 끝날 때 `capture_time_raised`(frames·max_raise_ms) 한 줄을 남긴다(접속 직후 몰림에서 늘 생기므로 INFO, epoch 앞 60장 뒤에도 올렸거나 최대 올림량이 25 ms 를 넘으면 WARNING)([설계](../../docs/design/capture-time-broken-pts.md)) |
| 12.7 I0 프레임 식별 | 헤더는 I0 `frame_header` 그대로. 재접속마다 `stream_epoch` +1, 새 epoch 는 `codec_info` 먼저, 키프레임부터 |
| 12.7 전송, 14 보안 | 카메라별 TLS over TCP(공개키 고정 `simai_site_alert.pinning`, 지문 확인 뒤에만 기기 토큰), 길이 접두 레코드(`wire.py`), 재접속마다 codec_info → 키프레임. 주소·카메라별 포트는 EC2 조회(`simai_site_alert.endpoint`), 바뀌면 다시 붙음 |
| 12.7 재접속, stream_epoch | 접속마다 카메라별 epoch 를 max(저장된 최대 + 1, 세션 시작 UTC ms) 로 정하고 로컬 파일(`--epoch-state`, 기본 `~/.simai/relay-epochs.json`)에 먼저 남긴다(`epoch_store.py`, 임시 파일 fsync 뒤 교체). 재시작·시계 역행·같은 시작 ms 에도 줄지 않는다. 저장 파일을 잃고 시계까지 뒤로 갔다면 Tier 0 가 STREAM_REJECT 로 거절하고, 중계는 상태(`tier0_rejection`)와 오류 로그를 남긴 뒤 더 큰 epoch 가 올 때까지 다시 붙지 않는다 |
| 12.7 송신 큐, D-G0-11 | 카메라별 큐 상한(기본 25프레임). 넘치면 다음 키프레임까지 버리고 skipped 로 셈, 버린 범위는 SKIP_NOTICE 로 Tier 0 에 알려 손실과 섞이지 않게 함(Tier 0 에 닿았는지 알 수 없으므로 지금 epoch 의 버린 범위를 합친 구간 집합으로 들고 새 연결마다 다시 보냄. 서로 떨어진 구간이 1024 개를 넘으면 오래된 구간을 잘라 내고 `unknown_before` 로 알려, Tier 0 가 그 앞 빈칸을 손실이 아니라 구분 불가로 셈). 소켓 송신 버퍼 64 KiB |
| 3.10 장애별 표시, `laptop_heartbeat` | 1초마다 127.0.0.1 UDP(`--status-port`, 기본 47031, 0 이면 끔)로 현장 경보 단말에 카메라별 상태(`laptop_heartbeat.cameras[]` 항목)와 Tier 0 영상 연결이 살아 있는 카메라를 알린다(`status_report.py`, S15P31A101-91). 단말이 '영상 단절 → 관제 중단'을 판단하고 하트비트를 채운다. 형식은 `simai_site_alert.relay_status` |
| 3.3 RTSP 접속 규칙, 16장 관제 화면 실시간 영상 | CCTV 로컬 미리보기(`--preview-port`, 기본 꺼짐): 수신 루프가 Tier 0 로 넘기는 I0 기록을 그대로 나눠 받아(RTSP 세션을 더 열지 않음, 디코딩·재인코딩 없음) 127.0.0.1 에서만 듣는 WebSocket 으로 보낸다(EC2 를 거치지 않음). 형식 `contracts/messages/cctv_preview.schema.json`(접속 직후 hello → 카메라별 마지막 codec → 카메라별 다음 키프레임부터 이진 프레임 = u32 BE 헤더 길이 + I0 frame_header + Annex B). 브라우저 Origin 허용 목록(기본 `https://simai.live`, `localhost`·`127.0.0.1` 의 5173·4173, `--preview-origin` 으로 바꿈), 소비자 4개, 소비자마다 큐 50프레임·16 MiB, 넘치면 그 소비자의 그 카메라만 다음 키프레임까지 건너뜀. 상태 로그에 카메라별 `preview`(sent·dropped_overflow·waiting_keyframe)와 `미리보기 상태`(소비자 수 등). 설계 `docs/design/cctv-local-preview.md` |
| 12.7 누락 | `camera_to_gateway_gap`: 도착 간격이 공칭 간격의 1.5배를 넘은 횟수·최대(RTP 간격도 따로). `laptop_heartbeat.cameras[]` 항목을 만든다 |

## 구조

`ai/` 와 분리된 독립 uv 프로젝트다. 저장소 안 경로 의존이 둘 있다: `gateway/recorder`(입력·촬영 시각·I0 헤더)와
`gateway/site_alert`(Tier 0 엔드포인트 조회·TLS 공개키 고정, S15P31A101-83). 그래서 저장소 전체를 받아 `gateway/relay/` 에서
`uv sync` 해야 한다. `cryptography` 는 개발 의존성이라 시험과 `bench-transport` 의 임시 인증서 만들기에만 쓴다(개발 의존성 없이
설치했으면 `--cert`·`--key` 를 준다). 입력(RTSP·파일·합성), 촬영 시각 추정, I0 헤더·코덱 정보 만들기는 `gateway/recorder`
(`simai-recorder`, 저장소 안 경로 의존)를 그대로 쓴다. 녹화 파일의 헤더와 LIVE 헤더가 같은 코드에서 나온다.

```
src/simai_relay/
  relay.py        카메라별 수신 루프(CameraRelay, 스레드 하나씩)와 여러 대 동시 중계(RelaySession)
  link_stats.py   카메라 → 노트북 구간 측정: 상태, camera_to_gateway_gap, 수신 지연(수신 − 추정 촬영 시각)
  sink.py         I0 기록(I0CodecInfo, I0Frame)과 전송부 인터페이스(CameraSink). 시험용 MemorySink, 점검용 CountingSink
  tls_sink.py     Tier 0 전송: 카메라별 TlsCameraSink(송신 스레드·송신 큐 SendQueue), EndpointWatcher(EC2 조회 주기 갱신)
  wire.py         I0 영상 연결 길이 접두 레코드 형식 simai-i0-video/1 (Tier 0 쪽은 ai/services/tier0/video_ingest.py)
  epoch_store.py  카메라별로 쓴 최대 stream_epoch 저장(재시작에도 epoch 가 줄지 않게)
  status_report.py 현장 경보 단말로 1초마다 상태 보고(loopback UDP)
  preview.py      관제 앱 CCTV 로컬 미리보기(127.0.0.1 WebSocket, PreviewSink 가 원래 CameraSink 를 감쌈)
  bench.py        G0 전송 비교 측정: 카메라별 연결 vs 단일 연결 다중화 (bench-transport)
  config.py       설정(TOML)·카메라 계정(.env) 읽기, 로그의 비밀 값 가리기(RedactSecrets)
  cli.py          simai-relay run
configs/          example.toml(실제 카메라), dry-run.toml(카메라 없이)
tests/            시험 (카메라 없이 돈다)
```

카메라마다 스레드 하나와 CameraSink 하나가 있고 카메라 사이에 공유하는 잠금·큐가 없다. 한 카메라의 수신이 멈추거나
전송이 막혀도 다른 카메라는 계속 돈다(시험 `test_one_camera_stall_does_not_block_others`).

Tier 0 영상 연결의 connect 가 Windows WinError 10014(일시 오류, 보안 프로그램 후보)로 실패하면 새 소켓으로 0.3 s 안에서 다시
시도하고, 그래도 안 되면 기존 재접속으로 간다(`simai_site_alert.winsock`, 경로 표와 V3 확인 방법은
`docs/design/gateway-winsock-retry.md`).

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
uv run simai-relay run --config configs/my-cameras.toml --send         # Tier 0 로 전송
uv run simai-relay run --config configs/my-cameras.toml --send --preview-port 47032   # + 관제 앱 CCTV 로컬 미리보기
uv run simai-relay bench-transport --seconds 20                        # 카메라별 연결 vs 다중화 (로컬)
uv run simai-relay bench-transport --mode per-camera --slow-camera none --seconds 1800   # 30분 연속 중계
```

- `--send` 는 `.env` 의 `SIMAI_EC2_BASE_URL`·`SIMAI_DEVICE_TOKEN`·`SIMAI_TIER0_PINNED_SPKI_SHA256`(선택 `SIMAI_LAPTOP_ID`,
  `SIMAI_TIER0_CA_FILE`)을 쓴다(현장 경보 단말과 같은 이름). 기기 토큰은 로그에서 `***` 로 가린다.

- 카메라 계정은 저장소 루트 `.env` 의 `RTSP_USER`/`RTSP_PASS`(카메라별 `RTSP_USER_CAM_A` …)에서만 읽는다
  (`gateway/recorder/.env.example`). 설정 파일에 쓰지 않는다.
- `--send` 가 없으면 `CountingSink` 로 개수만 센다. 카메라별 상태·측정(전송하면 `tier0_send`)을 `--report-interval` 초마다 로그에 쓴다.
- C110 은 RTSP 동시 접속이 2세션이다. 중계 중에 녹화 도구·Tapo 앱·VLC 를 같은 카메라에 붙이면 한도를 넘길 수 있다.

## Tier 0 가 stream_epoch 를 거절했을 때 (STREAM_REJECT)

상태 로그의 `tier0_send.tier0_rejection` 이 `{"reason": "stale_epoch", "stream_epoch": e, "current_epoch": c}` 이고
오류 로그에 "stream_epoch … 를 거절했다" 가 나오면, 그 카메라는 Tier 0 가 이미 본 epoch(c)보다 작은 epoch(e)로 보내고 있다.
같은 프레임 식별자가 다시 쓰일 수 있어 Tier 0 가 받지 않는 것이다. 보통 epoch 저장 파일(`--epoch-state`, 기본
`~/.simai/relay-epochs.json`)을 잃었거나 다른 노트북·다른 파일로 띄웠고, 노트북 시계도 뒤로 간 경우다.

1. 중계를 멈춘다.
2. 저장 파일에서 그 카메라 값을 `current_epoch` 보다 큰 값으로 고친다(예: `{"cam_A": c}`, 다음 epoch 는 c+1). 파일이 없으면
   그 내용으로 만든다. 값을 줄이지 않는다.
3. 중계를 다시 띄운다. 새 epoch 의 codec_info 부터 Tier 0 가 다시 받는다.

Tier 0 를 다시 띄워도 받게 되지만(Tier 0 가 epoch 기준을 잊는다), 그 카메라의 이전 epoch 와 식별자가 겹칠 수 있으므로 위
방법을 먼저 쓴다. 저장 파일이 깨져 있으면 중계가 시작하지 않는다(0 부터 다시 세지 않는다). 같은 방법으로 고친다.

## 측정

| 값 | 뜻 |
|---|---|
| `camera_to_gateway_gap.count`·`max_ms` | 같은 epoch 안 도착 간격이 공칭 간격 × 1.5(`threshold_ms`, 녹화 manifest 와 같은 기준)를 넘은 횟수와 최대 도착 간격 |
| `camera_to_gateway_gap.pts_gap_count` | RTP 타임스탬프 간격이 같은 기준을 넘은 횟수(카메라가 보내지 않은 프레임) |
| `receive_delay_ms` | 수신 시각 − 추정 촬영 시각(최근 250프레임 p50·p95, 전체 max). 고정 지연 + 기준보다 늦게 온 만큼 |
| 하트비트 `camera_to_gateway_gap_max_ms` | 직전 상태 보고(1초) 이후 도착 간격 최대(`laptop_heartbeat` 계약). 새 프레임이 없으면 비운다. 로그는 이 창을 비우지 않는다 |

## 하지 않는 것

- 디코딩·재인코딩·얼굴 처리, 영상 저장(녹화는 `gateway/recorder`). 미리보기도 받은 압축 프레임을 그대로 넘긴다.
- 비밀 값 기록. RTSP 계정은 입력 안에만 있고, 로그에는 camera_id 와 계정 없는 입력 설명만 쓴다. 로그 처리기에는 `.env` 계정 값을
  `***` 로 바꾸는 필터를 덧붙인다.
