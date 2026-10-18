# gateway/rtsp_probe — Tapo C110 RTSP 수신 측정 도구

노트북에서 Tapo C110 한 대의 RTSP 를 받아 명세 3.3 카메라 사양 표의 값(해상도·fps·키프레임 주기·비트레이트·동시 접속·RTCP)과
3.9·12.7 에 필요한 수신 특성(도착 간격, PTS, `camera_to_gateway_gap`)을 잰다(S15P31A101-9). 결과는 JSON 보고서와 짧은 원본
녹화로 남기고 커밋하지 않는다.

| 명세 | 이 도구에서 |
|---|---|
| 3.3 카메라 사양 | 해상도·코덱·프로파일, fps(도착·PTS), 키프레임 주기, 비트레이트, 동시 접속 1·2·3개, RTCP Sender Report |
| 3.3 RTSP 접속 규칙 | 측정은 세션 하나. 끝나거나 실패하면 반드시 닫는다. 동시 접속 시험만 일부러 여러 개를 연다 |
| 3.9 촬영 시각 | RTCP 시각 유무와 SR 사이 카메라 시각–RTP 대응의 흔들림, 녹화·중계와 같은 추정기로 본 `수신 − 추정 촬영 시각` |
| 12.7 누락 | `camera_to_gateway_gap`: 도착 간격이 공칭 간격의 1.5배(`simai_recorder.manifest.GAP_FACTOR`)를 넘은 횟수·최대, PTS 빈칸 |
| 19.1 미결 사항 | "공유기 환경에서 카메라 1대 10분 RTSP 측정" 을 이 도구로 한다 |

## 구조

`ai/` 와 분리된 독립 uv 프로젝트다. RTSP 수신은 녹화 도구와 같은 코드(`gateway/recorder` 의 `RtspSource`, PyAV, RTSP over TCP,
디코딩 없이 압축 프레임 단위)를 저장소 안 경로 의존으로 그대로 쓴다. 그래서 측정값은 운영 수신 경로(녹화·영상 중계)에서 보는 값과 같다.
촬영 시각 추정기(`capture_time`), I0 헤더·세그먼트 쓰기(`i0`, `segment`), 계정 읽기(`config`)도 녹화 도구 것을 쓴다.

```
src/simai_rtsp_probe/
  capture.py   측정 루프(세션 하나, 다시 붙지 않음), 짧은 원본 녹화, 동시 접속 시험
  report.py    관측 → 보고서 수치 (순수 계산, 모르면 null)
  cli.py       simai-rtsp-probe measure / concurrency, 보고서 쓰기, 계정 가리기
tests/
  fake_camera.py  시험용 RTSP 카메라 흉내 (RTSP over TCP, H.264 RTP, RTCP SR, 손실·역행·키프레임 없음·세션 제한)
  test_report.py  보고서 계산 단위 시험
  test_e2e.py     카메라 흉내 → 실제 PyAV RTSP 수신 → 보고서 (카메라 없이)
```

RTCP 시각은 FFmpeg 이 RTSP 수신 중에 패킷에 붙이는 값(`rtcp_sr` 새 SR 표시, `prft` 그 SR 로 환산한 프레임 카메라 시각)으로 읽는다.
녹화 도구의 `SourcePacket.camera_time_us`·`sender_report` 로 넘어온다(녹화·중계는 이 값을 쓰지 않는다. C110 기본은 `RECEIVE_ESTIMATED`).

## 설치와 시험

`gateway/rtsp_probe/` 에서 실행한다(저장소 전체가 있어야 한다. `gateway/recorder` 경로 의존).

```sh
uv sync
uv run pytest
```

시험은 카메라 없이 돈다. 외부 RTSP 서버 바이너리(mediamtx 등)는 쓰지 않는다. `tests/fake_camera.py` 가 로컬에서 RTSP 서버를
띄우고, 시험 안에서 libx264 로 만든 막대 무늬(사람 이미지 없음, 파일로 남기지 않음)를 RTP 로 보낸다. 실제 FFmpeg RTSP 클라이언트가
접속하므로 Digest·Basic 인증, interleaved RTP/RTCP, PTS·RTCP 시각 처리가 운영과 같은 경로로 시험된다. 약 15초 걸린다.

## 카메라 설치일 측정 절차

### 준비

1. 공유기 환경(명세 3.5)에서 노트북과 카메라를 같은 망에 둔다. 노트북 시계가 NTP 로 맞는지 확인한다(`w32tm /query /status`).
2. 카메라 설정(명세 3.3 "카메라 설정"): RTSP 계정 만들기, fps 25 고정, 워터마크·UPnP·동작 감지 끄기. 펌웨어 버전을 적어 둔다.
3. 저장소 루트 `.env` 에 계정을 넣는다(`gateway/recorder/.env.example` 와 같은 이름, 커밋 금지).
   `RTSP_USER`, `RTSP_PASS` 가 공통이고, 카메라마다 다르면 `RTSP_USER_CAM_A`·`RTSP_PASS_CAM_A` 처럼 쓴다(`--camera-id` 기준).
4. **측정 중에는 Tapo 앱 실시간 화면·VLC 를 닫는다.** C110 은 동시 2세션까지라 다른 접속이 측정과 동시 접속 시험을 흐린다.
5. 원본 녹화(기본 10초)는 사람이 찍히지 않을 때만 남긴다(명세 11.5). 사람이 있으면 `--record-seconds 0` 을 준다.

### 1) 짧은 확인 (카메라마다 30초)

```sh
cd gateway/rtsp_probe
uv run simai-rtsp-probe measure --host 192.168.0.11 --camera-id cam_A --seconds 30 --note "공유기 2.4GHz, 설치 위치"
```

해상도 2304×1296, fps 25, 키프레임이 오는지 화면 요약으로 본다. 4대 모두 한 번씩 한다(카메라를 하나씩, 동시에 돌리지 않는다).

### 2) 10분 측정 (카메라 1대, 19.1 완료 조건)

```sh
uv run simai-rtsp-probe measure --host 192.168.0.11 --camera-id cam_A --seconds 600 --note "공유기 2.4GHz, 노트북–카메라 거리 m, 같은 망 기기 수"
uv run simai-rtsp-probe measure --host 192.168.0.11 --camera-id cam_A --path stream2 --seconds 120 --record-seconds 0   # 저화질(선택)
```

측정 중 끊기면 다시 붙지 않고 받은 데까지 보고서를 남긴 뒤 종료 코드 1 로 끝난다(끊김도 측정 결과다). 다시 돌려 재현되는지 본다.

### 3) 동시 접속 시험

```sh
uv run simai-rtsp-probe concurrency --host 192.168.0.11 --camera-id cam_A --max-sessions 3 --seconds 15
```

세션 1개, 2개, 3개를 차례로 열어 동시에 15초씩 받는다. 시도마다 열린 세션 수, 끝까지 받은 세션 수, 세션별 fps, 거절 오류를 남기고
`max_concurrent_sessions_ok`(모든 세션이 끝까지 받은 최대 수)를 적는다. 1차 측정 기대값은 2다(3번째 거절).

### 4) 보고서 위치

```
ai/experiments/recordings/rtsp-probe/<UTC 시각>z-<camera_id>-measure/
  report.json                      보고서 (계정 없음, 주소는 계정을 뺀 rtsp://host:port/path)
  <camera_id>/epoch_0000.h264      짧은 원본 녹화 (녹화 도구 세그먼트 형식, read_segment 로 읽힘)
  <camera_id>/epoch_0000.frames.jsonl, epoch_0000.codec_info.json
ai/experiments/recordings/rtsp-probe/<UTC 시각>z-<camera_id>-concurrency/report.json
```

`.gitignore` 의 `recordings/` 로 커밋되지 않는다(`tests/test_e2e.py::test_default_output_is_git_ignored`). 출력 위치는 `--out` 으로 바꿀 수 있다.
녹화 샘플은 확인이 끝나면 지우거나, 남길 거면 녹화 도구 절차대로 Network Volume 에 올린다.

### 5) 명세 3.3 수정안 작성

보고서 값을 3.3 표에 옮기고 "공유기 환경 10분 측정 후 확정" 문구와 19.1 의 해당 항목을 정리하는 명세 MR 을 따로 연다
(`docs/spec/README.md` 갱신 규칙: 개정 이력 한 줄, 바뀐 절).

| 3.3 표 행 | 보고서 필드 |
|---|---|
| 해상도 | `stream.width`×`stream.height`, `stream.profile` (stream1·stream2 각각) |
| 프레임률 | `metrics.fps_by_pts`(카메라 시계), `metrics.fps_by_arrival`(노트북 도착) |
| 키프레임 주기 | `metrics.keyframe_interval_s.p50`·`max` |
| 비트레이트 | `metrics.bitrate_kbps_by_pts` (stream1·stream2) |
| RTSP 동시 접속 | concurrency 보고서 `max_concurrent_sessions_ok`, 거절된 세션의 `error` |
| 카메라 시각(RTCP) | `rtcp.present`, `rtcp.sr_interval_s_p50`, `rtcp.mapping_consistency_ms.step_p95`·`step_max`, `timestamp_source_recommendation`·`reason` |

함께 확인할 것: `metrics.arrival_interval_ms.p95`·`p99` 와 `receive_minus_estimated_capture_ms.p95` 로 녹화·중계의 촬영 시각 불확실성
잠정값(`CaptureTimeModel` 의 `uncertainty_ms` 20, `uncertainty_no_pts_ms` 45, 1차 측정 도착 지터 p95 약 41ms 기준)이 맞는지,
`metrics.camera_to_gateway_gap.count`·`pts.gaps` 로 10분 동안 끊김이 얼마나 있었는지. `timestamp_source_recommendation` 이
`CAMERA_STREAM_CANDIDATE` 여도 바로 바꾸지 않는다. 전환은 G0 LED 시험으로 정한다(3.9).

## 보고서 필드

- `stream`: 코덱·프로파일·해상도·SDP 선언 fps·PTS 시간 단위. 접속하지 못하면 null.
- `metrics`: 프레임 수, 측정 시간, fps(도착·PTS), 공칭 간격과 근거(`pts` / SDP `declared_fps`), 도착 간격 p50·p95·p99·최대,
  `camera_to_gateway_gap`, `pts`(PTS 없는 패킷, 간격 분포, 역행·반복, 빈칸·빠진 프레임 추정), 키프레임 수·첫 키프레임 전 패킷 수·주기,
  비트레이트(도착·PTS 기준), 수신 − 추정 촬영 시각 분포.
- `rtcp`: SR 유무·개수·간격, 카메라 시각 − 노트북 수신 시각 분포, 카메라 시계 설정 여부, SR 사이 대응 흔들림.
- `timestamp_source_recommendation`: SR 이 3개 이상 오고, 카메라 시계가 설정돼 있고, SR 사이 대응 흔들림 p95 가 5ms 이하일 때만
  `CAMERA_STREAM_CANDIDATE`, 그 밖은 늘 `RECEIVE_ESTIMATED`(이유는 `timestamp_source_reason`).
- `warnings`: 키프레임 없음, PTS 없음·역행·빈칸, 도착 끊김.
- `ended_by`: `completed` / `disconnected` / `stream_ended` / `open_failed` / `stopped`. 종료 코드는 `completed` 이고 프레임이 2개 이상일 때만 0.

모르는 값은 null 이다. RTCP 가 없으면 카메라 시각 관련 값은 모두 null 이고 0 으로 채우지 않는다. FFmpeg 은 RTSP 첫 패킷의 PTS 를
비우는 일이 있어 `pts.packets_without_pts` 가 1 일 수 있다(지어내지 않고 센다).

## 하지 않는 것

- 디코딩·화질 평가·얼굴 처리. 압축 프레임 크기·시각만 본다.
- 다시 접속. 측정 중 끊기면 거기서 멈춘다(녹화·중계는 다시 붙는다).
- 비밀 값 기록. 계정은 `.env` 에서만 읽고 보고서·출력·녹화 파일에 넣지 않는다. 보고서의 문자열에 계정 값이 섞이면 `***` 로 바꾸는 안전망도 있다.
- 영상·보고서 커밋. 출력은 `.gitignore` 된 곳에만 쓴다.
