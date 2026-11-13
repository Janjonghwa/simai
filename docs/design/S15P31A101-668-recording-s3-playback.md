# 관제 녹화의 작업 구간을 S3 에 올려 지난 작업 화면에서 다시 보기

S15P31A101-668. 관제 중 노트북 게이트웨이가 남긴 4대 녹화(-554) 중 **그날 작업 구간만** 관제가 끝난 뒤 S3 에 올리고(14일 보관),
지난 작업 화면의 카메라 칸에서 실제 카메라 영상으로 다시 본다. 관련 명세: 3장 데이터 흐름, 5.1 상시 영상 조각, 11.3 `video_segment`,
11.5 보관과 개인정보, 12.6 업로드 서명, 14장 조회 기록. 계약 묶음 v7.46.0, DB V68(V67 은 -669).

## 1. 설계

### 1.1 사용자 결정(2026-11-13, 바꾸지 않는다)

| 항목 | 결정 |
|---|---|
| 올리는 범위 | 그날 작업 구간 = 세팅 완료(`work_unit.setup_completed_at`) → 작업 종료(`actual_ended_at`). 작업 시작(`actual_started_at`)이 세팅 완료보다 이르면 작업 시작부터. 시험으로 켰다 끈 관제 전체는 올리지 않는다 |
| 올리는 때 | **관제가 끝난 뒤.** 관제 중 노트북 업로드는 I0 중계·CCTV 원격 시청이 쓴다 |
| 저장 위치 | 지금 S3 버킷(시드니) 그대로. **14일 뒤 삭제.** EC2 디스크에는 두지 않는다 |
| 형식 | 재인코딩 없이 mp4 로 포장만(H.264 그대로) |
| 얼굴 | 외형 규칙과 같이 관측 원본 그대로(AGENTS 규칙 8). 외부 LLM 으로 보내지 않는다 |

### 1.2 이미 있는 것(재사용)

`video_segment`(-330, V15)가 이미 "카메라 하나 × 촬영 구간 하나의 영상 조각" 표이고, 14일 삭제 작업(`VideoPurgeService`),
관리자 재생 주소·조회 기록(`GET /api/videos`·`/api/videos/{id}`, `video_access_log`), 지난 작업 화면의 영상 칸(`ReplayVideoWall`·`syncVideo`)까지 있다.
빠진 것은 **조각을 만드는 쪽**뿐이다(-330 은 Tier 0 가 올린다고 정했지만 Tier 0 업로드는 구현되지 않았다).
그래서 새 표를 만들지 않고 **게이트웨이 녹화를 `video_segment` 의 두 번째 생산자**로 붙인다. 다른 점은 출처(`origin`)와 작업(`work_id`) 칸뿐이다.

### 1.3 흐름

```
[관제 끝: simai-gateway.ps1 stop (관제 모드였을 때만)]
   │ manifest 마감(기존) → 업로드 도구를 백그라운드로 띄움 (-NoRecordingUpload 또는 .env SIMAI_RECORDING_UPLOAD=off 면 안 띄움)
   ▼
[노트북 simai-recorder upload]  C:\simai-local\recordings\live-* 중 14일 안, 아직 끝나지 않은 녹화마다
   1. 녹화 범위 = 카메라별 frames.jsonl 첫·마지막 촬영 시각 (manifest ended_at_ms 가 null 이어도 파일에서 계산)
   2. GET /api/devices/recordings/windows?from&to (기기 토큰)  ──►  EC2: 범위와 겹치는 작업 구간 [start, end]
        ◄── {windows:[{work_id,start_ms,end_ms}], open_work}  (open_work=true 면 아직 안 끝난 작업이 겹침 → 이 녹화는 다음에)
   3. 구간과 겹치는 프레임을 카메라·epoch 별로 고르고 키프레임에서 조각을 자른다(최대 5분)
   4. 조각마다: PyAV 로 mp4 포장(-c copy 와 같음, 재인코딩 없음, PTS = 촬영 시각) → sha256
        POST /api/uploads/sign {purpose: RECORDING_SEGMENT, segment_id, content_type: video/mp4, size_bytes, sha256}
        ◄── PUT 서명 주소(5분, 형식·크기·해시가 서명에 묶임)
        PUT  ──► S3(시드니) recording-segments/{segment_id}.mp4
        POST /api/devices/recordings/segments (recording_segment 1.0.0)  ──►  EC2: 발급·작업 구간 확인 → video_segment 한 행(origin GATEWAY_RECORDING)
   5. 성공한 조각은 녹화 폴더의 upload-state.json 에 적고 임시 mp4 를 지운다. 모두 끝나면 state 를 완료로
   ※ 조각 사이마다 관제 모드 파일(gateway-mode.txt 가 "관제 …")을 보고, 관제가 다시 시작됐으면 멈춘다(다음 stop 때 이어서)
[관리자 지난 작업 화면]
   GET /api/videos?from&to  →  조각 목록(기존)   →  재생 시각을 덮는 조각마다 GET /api/videos/{id} → 서명 GET(5분) → <video>
   재생 시각·재생/멈춤·배속을 3D 다시 보기와 같은 시각으로 맞춘다. 조각이 없거나 실패하면 지금 3D 다시 보기(목 화면)
[EC2 10분 주기 삭제 작업(기존)]  delete_after(= 조각 첫 프레임 촬영 시각 + 14일) 지난 객체를 지우고 deleted_at
```

### 1.4 작업 구간 계산(EC2)

- 대상: `actual_started_at` 이 있고 `[구간 시작, actual_ended_at]` 이 질의 범위와 겹치는 `work_unit`.
  구간 시작 = `least(setup_completed_at, actual_started_at)`(세팅 완료가 없으면 작업 시작), 끝 = `actual_ended_at`.
- 진행 중(`IN_PROGRESS`, 끝 없음)인 작업이 범위와 겹치면 `open_work: true`. 게이트웨이는 그 녹화를 미루고 다음 실행 때 다시 묻는다(작업 마무리를 관제 끈 뒤에 하는 경우).
- 현장 구분: 기기(`device`)에 현장이 묶여 있지 않고 MVP 는 현장 하나다(명세 16장). 모든 현장의 작업을 본다(남은 위험 참고).

### 1.5 시각 맞춤(핵심)

- **mp4 타임스탬프 = 촬영 시각.** 조각 안 프레임 i 의 PTS(ms) = `estimated_capture_time_ms(i) − segment_start_capture_ms`, 길이 = 다음 프레임과의 차(마지막은 공칭 40 ms).
  고정 fps 를 가정하지 않으므로 프레임이 빠져도(녹화 큐 넘침 버림·카메라 멈춤) 뒤 프레임 시각이 밀리지 않는다. mp4 는 가변 프레임 간격을 그대로 담고 브라우저도 그대로 재생한다.
- 조각 구간: `segment_start_capture_ms` = 첫 프레임 촬영 시각, `segment_end_capture_ms` = 마지막 프레임 촬영 시각 + 그 프레임 길이(= 다음 조각 첫 프레임 시각).
  그래서 같은 epoch 의 이웃 조각이 빈틈 없이 이어지고, 화면 `<video>.currentTime = (재생 시각 − segment_start_capture_ms) / 1000` 이 그대로 그 시각의 프레임이다.
- 촬영 시각은 중계가 Tier 0 로 보낸 I0 `frame_header` 의 값 그대로다(-554 녹화는 송신 기록을 그대로 저장). 3D 다시 보기(`/api/replay`)의 시각도 Tier 0 가 같은 값으로 매긴 것이라 **두 화면은 같은 시각축**이다.
- 오차 기준: ① 포장 반올림 0(ms 정수 → mp4 timescale 이 ms 를 정확히 표현). ② 멈춤·이동 뒤에는 브라우저가 그 시각의 프레임을 보이므로 1 프레임(40 ms) 이내.
  ③ 재생 중에는 기존 규칙대로 0.5 s 넘게 어긋날 때만 위치를 다시 맞추므로 0.5 s 이내(매 tick 맞추면 끊긴다). 멈춤 중에는 1 프레임 넘게 어긋나면 맞춘다(이번에 더함).
  ④ 실제 세계와의 차이는 촬영 시각 불확실성(`timestamp_uncertainty_ms` 20~45 ms, 명세 3.9)이며 두 화면에 똑같이 들어가 서로의 차이에는 들지 않는다.

### 1.6 조각 자르기·포장(노트북)

- epoch(재접속 단위) 하나 안에서만 자른다. 조각 첫 프레임은 키프레임이고, 작업 구간 시작 직전의 마지막 키프레임부터 시작한다(구간 앞 최대 GOP 하나 더 들어감).
  조각 길이 5분(넘으면 다음 키프레임에서 자름), 크기 상한 128 MiB(서명 상한). 2304×1296 25 fps 녹화가 카메라당 약 8 MB/분이라 5분 ≈ 40 MB.
- 포장은 PyAV(녹화 도구가 이미 쓰는 의존성, 휠에 FFmpeg 8 포함)의 mp4 먹서로 압축 프레임을 그대로 넣는다(`-c copy` 와 같음). `+faststart` 로 moov 를 앞에 둔다(서명 주소 바로 재생).
  게이트웨이 노트북에 ffmpeg 실행 파일은 설치 스크립트(`simai-gateway.ps1 setup`)·번들 어디에도 없다 → **ffmpeg CLI 를 쓰지 않는다.**
- `segment_id` 는 UUIDv7 이되 시각 칸 = 조각 첫 프레임 촬영 시각, 나머지 = sha256(녹화 ID·카메라·epoch·첫 frame_sequence) 로 정해서 다시 돌려도 같은 조각은 같은 id·같은 키다.
  EC2 등록은 같은 id·같은 해시면 `DUPLICATE`(성공 취급)라 중간에 끊겨도 이어 올리기가 안전하다.
- `has_gaps` = 조각 안 `frame_sequence` 빈칸이 있음(지어내지 않는다).

### 1.7 인증·재시도·이어 올리기·디스크·속도

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 인증 | 기기 토큰(`SIMAI_DEVICE_TOKEN`, `Authorization: Bearer`, 헤더에만). 경로 `/api/devices/**`(기기 체인)·`POST /api/uploads/sign`(업로드 체인이 이미 기기 허용) | 중계·경보 단말이 쓰는 것과 같다. 새 비밀 값 없음 |
| 업로드 목적 | `RECORDING_SEGMENT`(기기만, `video/mp4` 만, 128 MiB, 키 `recording-segments/{segment_id}.mp4`) | Tier 0 전용 `VIDEO_SEGMENT` 와 주체·형식이 달라 따로 둔다. 기록 확인 때 목적으로 생산자를 구분 |
| 재시도 | 호출마다 네트워크 오류·5xx·429 는 1·2·4·8·16 s 간격 최대 5번. 4xx(그 밖) 는 그 조각 실패로 두고 다음 조각. PUT 은 시도마다 새 서명 | 서명 5분은 PUT 시작 때만 검사된다. 클립 업로더(ai/services/tier0/clips/upload.py)와 같은 방식 |
| 이어 올리기 | 녹화 폴더 `upload-state.json`(조각 id·상태). 끝난 녹화는 `done`, 작업 미종료는 `pending` | 노트북이 꺼져도 다음 stop 때 이어서 |
| 관제 중 미룸 | 시작 때와 조각 사이마다 `gateway-mode.txt` 가 관제 모드면 멈춤. `run`·`run-remote` 는 시작 전에 돌던 업로드를 끈다 | 사용자 결정 |
| 디스크 | 임시 mp4 는 녹화 폴더 `.upload/` 에 동시 업로드 수만큼만(조각당 약 40 MB), 올린 뒤 지움. 원본 녹화는 지우지 않는다(-554 보관 규칙 그대로) | 노트북 여유 공간 하한 20 GB(-554) 를 거의 쓰지 않음 |
| 속도 | 동시 2조각(`--parallel`). 시드니 왕복 약 150 ms 라 연결 하나는 창 크기에 묶인다. 8시간 × 4대 ≈ 16 GB → 상향 20 Mbps 면 약 1.8 시간 | 관제가 끝난 뒤라 중계와 다투지 않는다 |
| 오래된 녹화 | 첫 프레임이 14일보다 오래된 녹화는 건너뜀(EC2 도 거부) | 올리자마자 지울 것을 올리지 않는다 |

### 1.8 EC2

- `GET /api/devices/recordings/windows?from&to`(기기): 계약 `recording_windows` 1.0.0. 범위 24시간 이내.
- `POST /api/devices/recordings/segments`(기기): 계약 `recording_segment` 1.0.0 검사 → 발급(`RECORDING_SEGMENT`, 같은 기기·키·형식·크기·해시) 확인 →
  조각이 그 `work_id` 작업 구간 안인지(앞쪽은 GOP 하나 여유 30 s, 끝은 마지막 프레임 길이 여유 1 s) → `video_segment` 한 행(`origin = GATEWAY_RECORDING`, `gateway_id` = 기기 ID,
  `delete_after` = 첫 프레임 촬영 시각 + 14일) → 발급 `used_at`. 응답 `{segment_id, status: CREATED|DUPLICATE}`.
- 삭제: 기존 `VideoPurgeService` 가 `video_segment` 전체를 보므로 그대로 지운다. 쓰이지 않은 `RECORDING_SEGMENT` 발급도 만료 14일 뒤 객체를 지운다(목적 조건만 넓힘).
- 조회: 기존 `GET /api/videos`·`/api/videos/{id}` 그대로(관리자, 조회 기록). 응답에 `origin` 을 더한다.

### 1.9 화면

- 기존 `ReplayStage` → `useReplaySegments(range)` → `ReplayVideoWall` 경로 그대로. mp4 는 서명 주소를 `<video>` 에 바로 넣는다.
- 더함: 서명 주소(5분)가 만료되거나 `<video>` 오류가 나면 주소를 한 번 다시 받아 같은 재생 시각으로 이어 감, 다시 실패한 조각은 빼고 남은 조각이 없으면 3D 다시 보기(목 화면),
  멈춤 중 1 프레임 정밀 맞춤. 목업(`src/mocks`)에 예시 조각 목록·재생 주소.
- 다시 보기 범위(`works.ts replayRange`)는 -667 이 고치므로 건드리지 않는다. 영상 목록은 그 범위로 받는다.

## 2. 코드와 시험

| 파트 | 코드 | 시험 |
|---|---|---|
| 계약 | `contracts/messages/recording_windows`·`recording_segment` 1.0.0, 예시·반례 8개, 계약 묶음 v7.46.0, Python 모델 재생성 | `uv run contracts/scripts/validate_examples.py`, `ai: uv run pytest tests/contracts` |
| EC2 | `domain/recording/`(`RecordingController`·`RecordingService`), `UploadPurpose.RECORDING_SEGMENT`, `VideoService`(목록에 `origin`), `VideoPurgeService`(쓰이지 않은 녹화 발급 정리), V68 | `RecordingSegmentIntegrationTest`(구간 계산·open_work·등록·DUPLICATE·구간 밖·보관 지남·발급 다름·계약 위반·같은 ID 다른 내용·노트북만·mp4 만), `VideoPurgeIntegrationTest`(녹화 발급 정리) |
| 게이트웨이 | `gateway/recorder/src/simai_recorder/upload/`(plan·mux·client·runner), `simai-recorder upload`, `gateway/ops/simai-gateway.ps1`(stop 훅·`upload-recordings`·`upload-worker`·`logs upload`·status 한 줄), `gw.sh upload`·`stop --no-upload` | `gateway/recorder/tests/test_upload.py`(합성 녹화: 키프레임 자르기·5분 분할·이웃 조각 연속·빠진 프레임·잘린 사이드카·mp4 PTS = 촬영 시각·faststart·재시도·이어 올리기·관제 중 멈춤·open_work·거절·보관 지남·끄기 스위치), `simai-gateway.Tests.ps1`(훅 조건·업로드 인자) |
| 화면 | `features/history/replaySegments.ts`(멈춤 1 프레임 맞춤), `ReplayVideoWall.tsx`(만료·오류 때 주소 한 번 다시 받기, 실패 알림), `ReplayStage.tsx`(실패 조각 빼고 없으면 목 화면), `mocks/recordings.ts` | `replaySegments.test.ts`, `ReplayStage.test.tsx`(실패 → 목 화면), `mocks/recordings.test.ts` |

게이트웨이 도우미 흐름: `stop`(관제 모드였을 때) → manifest 마감 → `Start-RecordingUpload` → 로컬 실행이면 숨긴 powershell 로, ssh 세션이면 로그인 사용자 세션 예약 작업
`simai-gateway-upload` 로 `simai-gateway.ps1 upload-worker` 를 띄운다(ssh 세션 프로세스는 세션이 끝나면 죽는다). `upload-worker` 는 중계 폴더에서
`uv run python -m simai_recorder upload --root C:\simai-local\recordings --mode-file C:\simai-local\gateway-mode.txt --env-file <저장소>\.env` 를 돌리고
`gateway-upload.log` 에 남긴다. 끝 코드: 0 끝남, 1 실패 조각 있음(다음에 다시), 2 설정 오류, 3 관제 중이라 멈춤. `run` 은 시작 전에 돌던 업로드를 끈다.

## 3. 14일 삭제

- 삭제 예정일은 EC2 가 정한다: `delete_after` = 조각 첫 프레임 촬영 시각 + 14일(게이트웨이 시계를 믿지 않는다). 업로드가 며칠 늦어도 녹화일 기준 14일이다.
- 기존 `VideoPurgeService`(10분 주기, 조각 하나씩 행 잠금·객체 먼저 삭제·`deleted_at`)가 출처와 상관없이 지운다. 기록 행은 남는다(조회 시 410 `SEGMENT_DELETED`).
- 서명만 받고 등록이 끝내 오지 않은 객체도 발급 만료 14일 뒤 지운다(`RECORDING_SEGMENT` 부분 색인 `upload_grant_unused_recording_idx`).
- 14일이 지난 녹화는 게이트웨이가 올리지 않고(`보관 기간(14일) 지남` 으로 done), EC2 도 410 `RECORDING_EXPIRED` 로 거부한다.
- 노트북의 원본 녹화는 이 티켓에서 지우지 않는다(-554 녹화 보관 규칙·디스크 하한 20 GB 그대로).

## 4. 명세에 없어 정한 수치

| 값 | 정한 것 | 근거 |
|---|---|---|
| 조각 길이 | 5분(다음 키프레임에서), 강제 끊기 9분·120 MiB | 서명 GET 5분과 재생 중 재수신 빈도, 카메라당 약 8 MB/분 → 약 40 MB. EC2 상한 10분·128 MiB 아래 |
| 구간 앞 여유 | 30 s(키프레임 거슬러 쓰기, EC2 같은 값) | C110 GOP 는 수 초. 구간 시작 프레임부터 재생되려면 앞 키프레임이 필요 |
| 조각 끝 프레임 길이 | 다음 프레임과의 차, 1 s 로 묶음(EC2 끝 여유 1 s) | 이웃 조각 연속. 긴 빈칸(카메라 멈춤)을 조각 끝에 넣지 않음 |
| 재시도 | 1·2·4·8·16 s 최대 5번, PUT 은 시도마다 새 서명 | 클립 업로더와 같음 |
| 동시 업로드 | 2조각 | 시드니 왕복 지연 보완, 노트북 임시 파일 80 MB 이내 |
| 멈춤 중 맞춤 | 40 ms(1 프레임) | 3D 다시 보기와 같은 프레임 |
| 재생 중 맞춤 | 0.5 s(기존 `DRIFT_S`) | 매 tick 이동은 영상이 끊긴다 |

## 5. 버린 대안

| 대안 | 버린 이유 |
|---|---|
| 노트북에서 원격 재생(EC2 경유 스트리밍) | 노트북이 꺼지거나 관제 중이면 볼 수 없고, 관제 중 상향 대역을 다시 쓴다(사용자 결정: 관제 중 업로드 금지) |
| EC2 디스크 저장·EC2 경유 업로드 | 사용자 결정(EC2 디스크에 두지 않음). EC2 는 320 GB·버스트형이라 하루 16 GB 가 부담. 서명 PUT 으로 바이트가 EC2 를 지나지 않는다 |
| 관제 중 실시간 업로드(Tier 0 상시 조각, -330 원안) | 관제 중 노트북·Pod 대역을 같이 쓰고, 시험으로 켠 관제 전체가 올라간다. 사용자 결정은 작업 구간만·관제 뒤 |
| ffmpeg CLI `-c copy` | 게이트웨이 노트북에 ffmpeg 실행 파일이 없고 설치 스크립트에도 없다. PyAV 가 같은 먹서를 쓴다 |
| 고정 fps mp4 + 프레임 시각표 따로 등록 | 프레임이 빠지면 화면이 시각표를 해석해야 한다. mp4 PTS 자체를 촬영 시각으로 두면 `<video>` 만으로 맞는다 |
| 새 표(recording_video) | 기존 `video_segment`·삭제·재생 주소·조회 기록·화면이 그대로 맞는다. 출처 칸만 더함 |
| h264 Annex B 그대로 올리고 화면에서 감싸기(-301 방식) | 5분 조각을 브라우저가 통째로 받아 감싸야 해 무겁고 이동이 느리다. mp4 faststart 는 범위 요청으로 바로 재생 |

## 6. 남은 위험·넘긴 일

- **실기기 확인 필요**: 게이트웨이 노트북의 중계 venv(`gateway/relay`, `uv sync` 뒤)에서 `python -m simai_recorder upload` 가 PyAV 로 실제 2304×1296 녹화를 포장하는지,
  시드니 업로드 속도(조각 40 MB 당 시간), ssh(`gw.sh stop`)로 끌 때 예약 작업 `simai-gateway-upload` 가 사용자 세션에서 뜨는지, 관제 재시작 때 업로드가 멈추는지.
- **S3 CORS**: mp4 는 서명 주소를 `<video src>` 로 바로 쓰므로 CORS 가 필요 없지만(단순 GET·범위 요청), 버킷 정책이 Range GET 을 막지 않는지 운영 버킷에서 확인한다.
- **현장 구분 없음**: 기기에 현장이 묶여 있지 않아 모든 현장의 작업 구간을 본다(MVP 현장 하나). 현장이 둘이 되면 기기 ↔ 현장 연결이 필요하다.
- **카메라 ID**: 게이트웨이 녹화는 `cam_A`~`cam_D`, DB 시드 카메라는 `cam1`~`cam4` 다. 화면은 받은 ID 로 칸을 만들고 `useCameraNames` 로 번호를 붙이므로 동작은 하지만,
  Tier 0 상시 조각과 같은 카메라를 같은 칸으로 묶지는 않는다(Tier 0 상시 조각은 아직 없음).
- **작업 마무리를 관제 뒤에 하면**: 그 녹화는 `open_work` 로 남아 다음 stop(또는 `gw.sh upload`)에서 올라간다. 자동 주기 실행은 없다.
- **촬영 시각 품질**: 녹화 촬영 시각은 G0 LED 시험 전 잠정값(-315 깨진 PTS 보정 포함)이다. 영상·3D 다시 보기 사이에는 같은 값이라 차이가 없지만 실제 세계와는 20~45 ms 차이가 남는다.
- **Flyway 순서**: V68 은 -669(V67) 다음이다. -669 보다 먼저 머지되면 outOfOrder 꺼짐에서 V67 이 거부된다(Lead 가 순서 관리).
- **명세 정리 MR 대상**: 명세 11.5 원본 영상 행은 "관제 중 Tier 0 상시 조각 저장(v1.3 수정 36)" 이다. 이 티켓은 그 대신 "관제가 끝난 뒤 게이트웨이가 그날 작업 구간만 mp4 로 올림"
  을 구현했다(Tier 0 상시 조각 경로는 표·서버만 있고 생산자가 없다). 11.5·3장 데이터 흐름·5.1 상시 영상 조각·12.6(기기 REST 두 개, 업로드 목적 `RECORDING_SEGMENT`)을 이 문서대로 고친다.
