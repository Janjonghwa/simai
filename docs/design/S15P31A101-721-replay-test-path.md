# S15P31A101-721 녹화 재생 시험 경로: 사이드카 촬영 시각 재생·불확실성 기동 점검·걷기 확인 건너뛰기(설계만)

17차 GPU 세션(2026-11-15)은 14차 로비 녹화를 노트북 중계(`kind = "file"`)로 재생해 Tier 0 에 넣었다. 그때 세 문제가 났다.

1. 재생이 카메라마다 정확히 25 fps 로 내보내 실제 카메라(24.94~24.98 fps)와 속도가 달라, 작업 구간에서 카메라 사이 실제 촬영 시각이
   188~342 ms 어긋났다(-716 설계 문서 「재생 입력의 카메라 간 시각 어긋남」). Tier 0 는 헤더대로 20 ms 로 믿었다.
2. 재생 설정에 RTSP 예시의 `uncertainty_no_pts_ms = 45` 를 복사하면 Tier 0 보간 허용 25 ms 를 넘어 모든 view 가 버려지고 사람 0명이
   조용히 나온다(8차 발견과 같은 함정).
3. 녹화에 여러 명이 나와 세팅 걷기 확인을 통과하지 못해 Pod 코드 수정(워커 `camera_check_enabled` False)과 DB 수동 PASSED 로 진행했다.

이 MR 은 1·2 를 고치고, 3 은 방법과 필요한 계약 변경만 적는다.

## 관련 명세

- 3.9 촬영 시각 계약(`estimated_capture_time_ms`·`timestamp_uncertainty_ms`·`timestamp_source`), 12.7 I0 프레임 식별
- 5.2 Tier 0 다시점 보간(허용 불확실성, `ai/src/simai_ai/geometry/interpolation.py` `DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS = 25.0`)
- AGENTS 규칙 2(모르면 비운다), 5(LIVE 만 실제 알림), 7(관측과 추정 구분, 정답·시험 지표를 제품 성능으로 쓰지 않음)

## 1. 사이드카 촬영 시각 재생 (`timing = "sidecar"`)

코드 `gateway/relay/src/simai_relay/replay_sidecar.py`, 설정 연결 `config.py`, 헤더 `relay.py`, 시험 `tests/test_replay_sidecar.py`.

녹화 도구는 프레임마다 사이드카(`<영상>.frames.jsonl`) 한 줄에 I0 헤더를 남긴다. 재생은 프레임 k 를

    재생 시각 = anchor + 회차 × period + offset[k],  offset[k] = 사이드카 촬영 시각[k] − min(네 카메라 첫 촬영 시각)

에 내보내고 이 값을 `estimated_capture_time_ms` 로 쓴다. `timestamp_uncertainty_ms` 는 사이드카의 녹화 원본 값이다. 재생은 원본의
**상대** 촬영 시각을 그대로 옮기므로 카메라 사이 오차는 녹화 때의 추정 오차와 같고, 송신 지터(잠·스레드)는 예정 시각을 쓰니 들지 않는다.

- anchor: 네 카메라가 같이 쓰는 (단조 시계, 벽시계) 한 쌍. 처음 연 카메라가 지금 + 500 ms 로 정한다(파일 열기 수십 ms 보다 넉넉히,
  늦게 연 카메라가 앞 프레임을 몰아 보내지 않게). 카메라마다 따로 원점을 잡으면 스레드 시작 차이만큼 어긋나기 때문이다.
- period: 네 카메라 공통 순환 길이 = max(마지막 offset + 프레임 간격 중앙값). 카메라마다 프레임 수가 달라도(14980 vs 14986) 순환마다
  같은 시각에 처음으로 돌아가 정렬이 유지된다. 짧은 카메라는 회차 끝에서 기다린다.
- 원본 절대 시각과의 대응은 기동 로그 `replay_anchor anchor_wall_ms=… recording_ref_ms=…` 한 줄로 남긴다. I0 헤더 형식(`contracts/`)은
  바꾸지 않았다(`timestamp_source` 는 파일 재생 기본 `FILE`, `source_pts` 는 null 그대로).
- 사이드카보다 영상이 길면 남는 프레임은 촬영 시각을 모르므로 보내지 않는다(규칙 2). 다음 순환은 파일 처음(키프레임)부터라 디코딩이
  깨지지 않는다. 개수가 어긋나면 `sidecar_frame_count_mismatch` WARNING 한 번.
- 사이드카가 없으면 `sidecar_missing` WARNING 후 기존 fps 재생이다. 시각이 뒤로 가거나 같은 줄이 있으면 기동 거부(녹화 도구는 epoch 안
  단조를 보장하므로 깨진 사이드카다).
- 녹화 원본 불확실성이 25 ms 를 넘는 프레임(PTS 깨짐 직후 5 s 의 45 ms 등)은 그대로 둔다(숨기지 않는다). 비율을 기동 로그
  `sidecar_uncertainty_over_tier0` 로 알린다. 14차 cam_A 원본 사이드카는 132700 장 중 145 장(0.11 %).

### 잘라 붙인 파일: `simai-relay sidecar-cut`

재생 파일이 녹화 일부 구간을 이어 붙인 것이면(17차 `cut_hq.py`: 6~10분 + 52~62분 + 71~80분) 사이드카도 같은 구간으로 자른다.

- 구간은 UTC ms `START:END`(끝 미포함), 네 카메라에 같은 구간을 준다. 고르는 규칙 `START ≤ t < END` 는 `cut_hq.py` 의
  `i0 = 첫 t ≥ START, i1 = 첫 t ≥ END` 와 같아 같은 프레임이 나온다. 출력의 `first_index`·`end_index` 가 원본 프레임 번호다.
- 각 줄에 `replay_offset_ms = 앞 구간 길이(END − START) 합 + (t − 이 구간 START)` 를 더한다. 구간 사이 빈 시간(수십 분)은 건너뛰고,
  구간 경계가 카메라 공통이라 네 카메라 시각축이 같다. 원본 header 는 그대로 둔다. 결과 파일은 덮어쓰지 않는다.
- 재생은 `replay_offset_ms` 가 있으면 그것을 offset 으로 쓴다. 잘린 사이드카와 원본 사이드카를 한 재생에 섞으면 거부한다.
- 14차 사이드카로 확인: 8~10분 + 20~21분 두 구간 cam_A 2995+1496 장, cam_B 2995+1499 장, period 180017 ms, 4개 파일 자르기 약 2 s.

### 명세에 없어 정한 수치

| 값 | 근거 |
|---|---|
| anchor 앞당김 500 ms | 파일 열기·스레드 시작 수십 ms 보다 넉넉히, 기동 지연으로만 보인다 |
| 순환 끝 한 칸 = 간격 중앙값 | 마지막 프레임과 다음 회차 첫 프레임 사이도 한 프레임 간격이 되게. 사이드카 한 장뿐이면 40 ms |
| 남는 영상 프레임 버림 | 촬영 시각을 지어내지 않는다(규칙 2) |

### 버린 대안

- 가짜 PTS(offset ms, timebase 1/1000)를 붙여 기존 추정기에 맡기기: 추정기가 창 안 최소 (수신 − PTS) 를 원점으로 쓰므로 카메라마다
  원점이 송신 지터만큼 달라지고, 헤더 `source_pts` 에 파일에 없는 값이 들어간다.
- 카메라마다 자기 첫 프레임에서 시작: 스레드·파일 열기 차이만큼 어긋나고 순환마다 프레임 수 차이(최대 6장 = 240 ms)가 쌓인다.
- 원본 절대 시각을 그대로 capture_time 으로: Tier 0 는 LIVE 시각축(지금)을 기대한다(지난 시각은 늦은 프레임으로 버려진다).

## 2. 재생 불확실성 기동 점검

`config.py` `_check_tier0_uncertainty`, 시험 `test_file_uncertainty_over_tier0_limit_is_rejected_unless_allowed`.

실시간 파일(fps 재생)의 헤더 불확실성(raw H.264 는 PTS 가 없어 `uncertainty_no_pts_ms`, MP4·MKV 는 `uncertainty_ms`)이 25 ms 를 넘으면
`load_cameras` 가 `ConfigError` 로 기동을 멈춘다(CLI 는 「설정 오류」로 끝남). 일부러 넘기려면 `[replay] allow_uncertainty_over_tier0 = true`,
그때는 큰 WARNING 을 남기고 계속한다. 경고만 두지 않은 이유: 17차처럼 로그를 보지 않으면 사람 0명이 조용히 나오고 GPU 세션 시간을 잃는다.

- 25 ms 는 gateway 가 ai 를 import 하지 않아 `replay_sidecar.TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS` 에 따로 두고, 시험이 ai 소스의
  `DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS` 와 같은지 본다(`test_tier0_limit_matches_ai_interpolation_default`).
- RTSP 는 보지 않는다(정상 PTS 는 20 ms, 45 ms 는 PTS 없는 몇 장뿐). `realtime = false` 파일도 보지 않는다(페이싱 없는 점검용).
- 예시 `configs/file-playback.toml` 은 `[replay] timing = "sidecar"` 를 기본으로 두고 45 ms 복사 금지와 우회 키를 적었다.

## 3. 재생 시험 전용 걷기 확인 건너뛰기 (설계만, 이번 MR 에서 구현하지 않음)

### 지금 구조

- 완료 관문 `SetupCompletionGate.blockedReason` 은 카메라 확인 `PASSED` + 그 확인의 보정 = 세션 보정을 요구한다. 예외는 고른 맵 묶음을
  그대로 쓰는 재사용(-714, `reuse_source_session_id`)뿐이다. **녹화와 같은 카메라 자리의 기존 맵이 있으면 지금도 그 재사용으로 걷기 확인
  없이 완료할 수 있다**(17차는 새 맵이라 해당 없음).
- 서버는 세션이 녹화 재생인지 모른다. 노트북 중계 입력 종류(RTSP/FILE)는 relay 안(`describe()`)에만 있고 `laptop_heartbeat.cameras[]` 에는
  없다. Tier 0 도 재생 영상을 LIVE 로 판정하므로 **재생 시험의 이벤트가 LIVE 이벤트·실제 알림으로 나간다**(-716: R2 CRITICAL 현장 경보 27건).
  규칙 5 의 빈틈이며 3 보다 먼저 막아야 한다.
- `SetupReuseCandidates` 는 카메라 확인 통과 세션만 재사용 후보로 보인다.

### 제안

입력 종류의 정본을 relay 가 실제로 연 입력으로 둔다(사람이 켜는 토글은 운영에서 켜 둔 채 잊으면 실제 경보를 끄므로 쓰지 않는다).

1. 계약(마이너): `laptop_heartbeat.payload.cameras[]` 에 `source_kind`(`RTSP`|`FILE`|`SYNTHETIC`, 선택) 추가. relay `heartbeat_entry` 가
   `spec.source.kind` 를 싣는다. I1 LIVE 계약이 아니므로 Lead 승인 대상은 아니나, 하트비트는 Tier 0·EC2 둘 다 받으니 둘 다 모르는 키를
   무시하는지 확인한다(현재 `additionalProperties: false` 라 스키마 마이너 갱신 필수).
2. 서버: 세팅 세션 진행 중 받은 하트비트에서 세션 카메라가 **모두** `FILE`/`SYNTHETIC` 이면 세션에 `input_kind = REPLAY_FILE` 을 기록(Flyway
   열 추가, 덮어쓰지 않고 처음 본 값 유지). 카메라 확인 상태에 `SKIPPED_REPLAY` 를 두고, `blockedReason` 은 `input_kind = REPLAY_FILE` 일 때만
   그것을 통과로 본다. 하나라도 `RTSP` 면 `SKIPPED_REPLAY` 를 거부한다.
3. 알림(규칙 5): `input_kind = REPLAY_FILE` 세션으로 시작한 작업, 또는 하트비트가 FILE 을 보고하는 동안의 이벤트는 저장·화면 표시는 하되
   알림 채널(현장 경보 `local_alert`, 워치, 푸시)로 보내지 않는다. 현장 경보는 Tier 0 → 노트북 경로라 서버만으로 못 막으므로 Tier 0 도
   `laptop_heartbeat` 의 `source_kind` 로 같은 판단을 하거나 relay 가 FILE 입력일 때 현장 경보 단말에 「시험 입력」 상태를 보고해 소리를 막는다.
   이벤트에는 출처 표시(예: `input_provenance = REPLAY_FILE`)를 남겨 시험 지표가 제품 성능으로 쓰이지 않게 한다(규칙 7). RiskEvent 의
   `source` 는 Tier 0 계약상 LIVE 고정이라 새 값으로 바꾸지 않고 별도 필드로 둔다(계약 변경, `risk_event_transition`·`risk_event` 마이너).
4. 재사용 후보: `SKIPPED_REPLAY` 세션은 `SetupReuseCandidates` 에서 빼 LIVE 운영이 재생 시험 맵·보정을 고르지 않게 한다.
5. 워커 `camera_check_enabled` 는 바꾸지 않는다(서버가 건너뛸 때는 확인 요청을 보내지 않으면 된다).

필요한 계약 변경: `laptop_heartbeat`(cameras[].source_kind), `risk_event_transition`·`risk_event`(입력 출처 필드), 세션 조회 응답의 카메라 확인
상태 값(`SKIPPED_REPLAY`). 알림 억제 경로가 셋(EC2 알림·Tier 0 현장 경보·워치)이라 별도 티켓으로 나누고 3 → 2 순서로(알림 억제 먼저) 한다.

## 잔여 위험

- 영상 자르기가 사이드카와 같은 프레임을 골랐다는 보장은 자르는 스크립트(ffmpeg `-ss i0/25`, MP4 가 정확히 25 fps CFR 이라는 가정)에 있다.
  relay 는 프레임 수만 비교한다(어긋나면 WARNING). 재인코딩으로 프레임이 빠지면 그 뒤 시각이 한 칸씩 밀린다.
- 원본 사이드카의 촬영 시각 자체가 C110 수신 추정(±20 ms)이라 재생 정렬은 녹화 때 정렬보다 좋아지지 않는다.
- 3 이 구현되기 전까지 재생 시험은 여전히 LIVE 이벤트·실제 알림을 낸다(위 제안 3).

## 넘긴 일

- 3 의 구현(계약 → relay 하트비트 → 서버 세션·관문·알림 억제 → Tier 0 현장 경보 억제). 별도 티켓.
- 노트북의 재생 설정(저장소 밖, 예 `session16/rec14/hq/replay-hq.toml`)에서 공통 `uncertainty_no_pts_ms = 45` 를 지우고
  `[replay] timing = "sidecar"` 와 `sidecar-cut` 결과를 쓰게 바꾸는 일(fps 재생 그대로 두면 이번 MR 뒤 기동이 거부된다).
