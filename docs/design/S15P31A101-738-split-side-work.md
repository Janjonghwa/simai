# 프로세스 분리 곁가지 옮기기·기본 켬 (S15P31A101-738)

-728 의 검출·판정 프로세스 분리(설계 `S15P31A101-728-tier0-process-split.md`)는 1차에서 그림·압축 패킷이 필요한 곁가지(이벤트
프레임, 이벤트 클립, 관제 중 배경 누적·회전 복구, Re-ID)를 껐다. 이 티켓은 -728 설계 9절의 1~3 을 끝내고 `process_split` 을 기본
켬으로 바꾼다. Re-ID(9절 4)는 S15P31A101-744 가 옮긴다(`S15P31A101-744-split-reid.md`).

## 1. 18차 실측(2026-11-15, H100, 14차 녹화 재생)

| 칸 | 2~3명 판정 p95 | 판정 스레드 벽/CPU | 자식 재시작·IPC 유실 | 비고 |
|---|---|---|---|---|
| 분리 끔 | 100~140 ms | 1.5 | 해당 없음 | 느린 CPU 호스트(Xeon 8460Y+), 아바타 관문 차단 |
| 분리 켬 | 57~70 ms | 1.05 | 0·0 | 이벤트 프레임이 꺼져 R2 25건 VLM 근거 0건(끔: 57건 중 17건). 클립·배경 누적도 꺼짐 |

RunPod 은 호스트 CPU 를 고를 수 없어 느린 호스트에서도 판정 지연을 지키려면 분리가 필요하다. 다만 분리 켬에서 곁가지가 꺼지면 VLM
근거(이벤트 프레임)가 사라지므로, 곁가지를 검출 프로세스로 옮긴 뒤 기본 켬으로 바꾼다.

## 2. 관련 명세·코드·시험

| 명세 | 이 문서에서 |
|---|---|
| 3.1·3.2 Tier 0 서비스, 정본 단일 소유 | 이벤트 순번·msg_id·I1 송신·대상 전이 고르기는 판정 프로세스(3절) |
| 5.1 이벤트 클립 링 버퍼 | 압축 패킷은 검출 프로세스의 영상 수신에서 바로 링에 넣는다 |
| 5.4 이벤트 프레임·VLM 근거 | 프레임 링·JPEG·로컬 사본·서명·S3 PUT 은 검출 프로세스, `event_frame_ready` 는 판정 프로세스 I1 |
| 4.3·4.4 관제 중 배경 누적·회전 복구 | 누적은 검출 프로세스, 회전 복구는 판정 프로세스(검출 프로세스가 쓴 표본 파일을 읽음) |

| 구분 | 경로 |
|---|---|
| 곁가지 묶음·대리 객체 | `ai/services/tier0/split_side_work.py`(새 파일: `SideWorkSpec`, `ChildSideWork`, `RemoteEventFrames`, `RemoteClips`, `RemoteCleanBackground`) |
| 경계·응답 경로 | `ai/services/tier0/detect_process.py`(`send_side`·`side_request`·`on_side`·`on_new_generation`, file 입력 받기 스레드, 응답 seq 라우팅) |
| 나눠 쓴 함수 | `ai/services/tier0/event_frames.py`(`wants_frames`·`frame_requests`·`EventFrameService.request`·`EventFrameIo`), `ai/services/tier0/clips/service.py`(`on_triggers`·`critical_hold_key`) |
| 조립·기본값 | `ai/services/tier0/service.py`, `ai/services/tier0/settings.py`(`DEFAULT_PROCESS_SPLIT = True`), `ai/tests/conftest.py`(시험 묶음은 단일 경로 기본) |
| 시험 | `ai/tests/services/tier0/test_tier0_split_side_work.py`(새), `test_tier0_process_split.py`·`test_tier0_process_split_live.py`(수정) |

계약(`contracts/`)은 바꾸지 않았다. I1 메시지(`event_frame_ready` 1.1.0)는 지금과 같은 모양이고 검출 프로세스 안에서 같은 계약 검사를
거친다.

## 3. 옮긴 경계

| 곁가지 | 검출 프로세스(`ChildSideWork`) | 판정 프로세스(대리 객체) |
|---|---|---|
| 이벤트 프레임(CRITICAL R1~R4) | `EventFrameBuffer`(검출 결과와 헤더가 맞은 그림, `DetectorFeed.observe_decoded`), `EventFrameService` 작업 스레드(JPEG 85·로컬 사본·서명·뒤 S3 PUT·계약 검사) | `RemoteEventFrames`: 판정 tick 안에서 `frame_requests`(LIVE·CRITICAL·R1~R4·최초 RAISED, event_id 당 한 번, 대상 World Track 을 본 카메라 우선) → 요청 명령. 검출 프로세스가 돌려준 결과를 중계 스레드가 `link.publisher.send_event` 로 I1 신뢰 큐에 넣는다(QueueFull 이면 같은 내용으로 기다림) |
| 이벤트 클립 | `EventClipService` + `PacketRingBuffer`. 검출 프로세스의 `LiveInput.bridge.attach_clips` 로 영상 수신에서 바로 받는다(영상 포트를 열기 전에) | `RemoteClips`: 전이에서 `clip_trigger_for`·`critical_hold_key` 로 뽑은 값만 보낸다(전이 객체는 읽기 전용 매핑을 품어 pickle 되지 않는다). 정리 주기의 `poll` 도 명령 |
| 배경 누적(-540) | `BackgroundCleanBackground` 그대로(결과 묶음·`status.json`·세팅 촬영 요청 `regenerate.request.json`·offload 작업자). 판정 쪽 보정 문서·입력 출처가 정해진 뒤 `bg_init` 명령으로 만든다 | `RemoteCleanBackground`: 보정 활성화(`bg_activate`)·스트림 변경 리셋(`bg_reset`)·회전 표본 크기(`bg_rotation_side`) 명령. 회전 복구가 읽는 기준 배경 목록·최근 서명은 검출 프로세스 상태 보고값 |
| 회전 복구 | 축소 표본 스냅샷을 요청받을 때 파일로 쓴다(`<state_dir>/clean_background/rotation-views/*.pkl`, 임시 파일 → `os.replace`) | `BackgroundRotationRecovery` 그대로(제안·드리프트 보고·판정 lock 콜백). `rotation_snapshot` 은 `side_request` 로 파일을 쓰게 하고 읽은 뒤 지운다 |

그림을 넘기지 않는다. 경계를 지나는 것은 작은 명령(이벤트 ID·시각·카메라 순서, ClipTrigger, 보정 문서)과 결과(event_frame_ready
payload, 상태 값)뿐이다. 회전 복구 입력(카메라당 최근 최대 10장 축소본, 960 px 기준 4대 약 80 MB)은 15초 주기의 회전 복구가 부를 때만
파일로 넘긴다(단일 경로도 회전 작업자 자식에 같은 양을 pickle 로 넘긴다).

## 4. IPC 큐·명령 형식

판정 → 검출 명령은 -728 의 같은 유계 명령 큐(256건)로 간다.

| 명령 | 형식 | file 입력 | LIVE |
|---|---|---|---|
| 곁가지 명령 | `("side", now_ms, kind, *args)` | 녹화 메시지와 같은 순서로 기다려 넣는다(`send_ordered`, 결정론) | 기다리지 않음. 상태를 바꾸는 명령(`clips`·`bg_*`)만 1초까지 기다린다. 못 넣으면 `cmd_dropped` |
| 곁가지 요청(읽기) | `("side", now_ms, kind, seq, *args)` → 답 `("side_reply", seq, ok, error)` | 순서에 끼어도 결과가 같은 읽기 요청만(회전 표본 파일) | 같다 |

| kind | args | 검출 프로세스 동작 |
|---|---|---|
| `frames` | event_id, 기준 촬영 시각, 발생 시각, 카메라 순서 | `EventFrameService.request`: 링에서 최대 2대×2시점 참조 고정 → 작업 큐 |
| `clips` | ClipTrigger 목록, 보존 표시 열쇠 목록 | `EventClipService.on_triggers`(대기 시작 시각은 명령의 판정 시계) |
| `clips_poll` | 없음 | `EventClipService.poll` |
| `bg_init`·`bg_activate` | 보정 문서, 입력 출처 | 배경 누적 시작·활성화 |
| `bg_reset` | camera_id | `reset_camera` |
| `bg_rotation_side` | 긴 변 | `configure_rotation_samples` |
| `bg_dump_rotation`(요청) | seq, 파일 경로, 뺄 카메라 | `rotation_snapshot` 을 파일로 쓰고 답 |

검출 → 판정은 같은 out 큐에 `("side", "event_frame_ready", payload)` 를 `put_nowait` 한다(차면 `EventFrameService` 가 QueueFull 로 보고
같은 재시도). 상태 하트비트·step 응답에 `side`(계수), `bg_rotation_signatures`, `bg_reference`(바뀌었거나 10초가 지났을 때만)를 싣는다.

응답 라우팅: file 입력도 받기 스레드(`tier0-detect-ipc`)를 둔다. step·close_ingest·side_reply 응답은 seq 로 기다리는 쪽에 넘기고(기다리다
포기한 seq 는 버림), 곁가지 결과는 등록한 함수(`on_side`)로 보낸다. 그래서 재생이 끝난 뒤에도 event_frame_ready 가 늦지 않고, 회전 복구
스레드의 요청과 판정 스레드의 lockstep 이 같은 큐를 함께 쓴다. file 입력은 step 사이 간격이 판정 루프에 달려 있어 받기 스레드가 멈춤(10초)
으로 보지 않는다.

## 5. 재시작·종료

| 상황 | 동작 |
|---|---|
| LIVE 검출 프로세스 재시작 | 링·누적 표본은 새로 모은다(모르면 비움). 새 세대에 `bg_init`(지금 보정 문서)·`bg_rotation_side` 를 다시 보낸다(`on_new_generation`). 대기 중이던 이벤트 프레임·클립은 잃는다(검출 프로세스 메모리) |
| 끝난 세대의 응답 대기 | 세대가 바뀌거나 자식이 죽으면 기다림을 끝내고 seq 를 지운다(남는 대기 없음, 시험 c) |
| 정상 종료 | 검출 프로세스 `close` 에서 입력을 멈춘 뒤 이벤트 프레임 작업(진행 중 PUT 은 자기 시간 제한)·배경 누적을 닫는다. 판정 쪽 중계 스레드는 `event_frames.stop` 으로 멈춘다 |

## 6. 기본값

`settings.DEFAULT_PROCESS_SPLIT = True`. 자동 부팅(`deploy/scripts/simai_bootstrap.py` 가 쓰는 `tier0-live.json`)은 `process_split` 을
적지 않으므로 이 기본을 쓴다. 끄는 스위치는 그대로다: `SIMAI_TIER0_PROCESS_SPLIT=0` 또는 설정 `"process_split": false`. 입력을 바꿔 끼운
조립(`Tier0Service(feed=...)`)은 검출 프로세스를 띄우지 않으므로 분리로 보지 않는다.

시험 묶음은 `ai/tests/conftest.py` 가 기본을 끈다(시험마다 spawn 자식을 띄우지 않고, 대역 팩토리가 pickle 되지 않아도 되게). 분리 경로
시험은 환경 변수·설정으로 명시해 켠다.

## 7. 시험

- (a) file 재생 결정론: 같은 녹화를 최대 속도로 단일·분리 경로에 넣어 world_state 가 같고, event_frame_ready(카메라·촬영 시각·JPEG
  해시·크기·로컬 사본 경로), 배경 표본 수·내보내기 여부, 회전 복구 입력(서명·축소 표본 배열)이 같다. 장면의 R1 은 HIGH·REPLAY 라 근거
  사진 대상이 아니어서 대상 고르기 함수(`wants_frames`)만 바꿔 같은 경로를 태운다. 최대 속도 재생은 배경 작업이 못 따라가 시각에 따라
  표본을 버리므로 이 시험은 대기열을 넉넉히 두고 같은 프로세스 안 누적(offload 끔)으로 잰다.
- (b) 클립: 판정 쪽이 뽑아 보낸 값으로 검출 쪽 클립 서비스가 단일 경로와 같은 대상·중복 제거·자르기를 한다.
- (c) 자식 재시작: 새 세대에 배경 누적이 다시 시작되고(상태 파일 다시 씀) 회전 입력 요청에 답하며, 응답 대기·seq 가 남지 않는다.
- LIVE E2E(`test_tier0_process_split_live`): R1(HIGH, LIVE) 발생이 클립 대상으로 검출 프로세스에 가고, 꺼진 곁가지는 Re-ID 뿐이다.

## 8. 버린 대안

| 대안 | 버린 이유 |
|---|---|
| 판정 프로세스가 그림을 받아 지금 곁가지를 그대로 돌림 | 25 fps × 4대 × 9 MB 를 넘겨야 한다(-728 1절의 버린 대안과 같은 이유) |
| 전이 객체(EventTransition)를 그대로 보냄 | 상태에 읽기 전용 매핑(mappingproxy)이 있어 pickle 되지 않고, 판정 쪽에서 이미 다 고를 수 있다 |
| 회전 복구도 검출 프로세스로 | 제안·드리프트 보고·보류·시험 기간 판단이 판정 lock·보정 저장소에 묶여 있다(단일 소유) |
| 회전 표본을 표본마다 파일로 계속 씀 | 카메라당 5초마다 약 20 MB 를 쓴다. 15초 주기 요청 때만 쓴다 |
| 회전 표본을 out 큐로 직접 보냄 | 약 80 MB 의 unpickle 이 관측 받기 스레드(GIL)를 막는다. 파일은 회전 복구 스레드가 읽는다 |

## 9. 명세에 없어 정한 수치

| 항목 | 값 | 근거 |
|---|---|---|
| 판정 쪽 event_frame_ready 중계 대기 | 64건 | 이벤트 큐(8건)의 여러 배. 차면 버리고 남긴다(경보 경로와 무관) |
| LIVE 상태 명령 대기 | 1초 | 명령 큐는 주 스레드가 바로 비운다. 자식이 멈춘 경우에만 판정 쪽이 그만큼 기다린다 |
| 회전 표본 파일 요청 대기 | 20초 | 회전 복구 주기 15초보다 길게 |
| 기준 배경 목록 재전송 | 10초 | 상태 하나를 잃어도 판정 쪽 사본이 맞게 |

## 10. 남은 일·잔여 위험

- Re-ID(-728 9절 4): S15P31A101-744 에서 옮겼다(크롭·임베딩은 검출 프로세스, 판정 쪽 특징 사본·`update_sampling` 명령, file 재생 결정론 재정의). -738 은 -744 와 함께 머지해야 분리 기본 켬에서 Re-ID 가 꺼지지 않는다.
- 명세 3.1·3.2 의 "Tier 0 프로세스" 문구를 "Tier 0 서비스(검출·판정 두 프로세스)"로 고치는 명세 정리 MR(-728 9절 6).
- 판정 프로세스 스레드 CPU 묶음(-728 9절 5)은 그대로 남았다.
- 실측 전: 분리 켬에서 이벤트 프레임이 VLM 근거로 실제로 쓰이는지(event→ready 지연, R2 근거 비율)와 검출 프로세스 CPU(JPEG·배경 누적이 검출 루프와 GIL 을 나눔)를 다음 GPU 세션에서 확인해야 한다. 배경 누적은 기본 offload(손자 프로세스)라 검출 프로세스 GIL 부담은 큐 넣기 정도로 본다.
- LIVE 재시작 동안 대기 중인 이벤트 프레임·클립은 잃는다(재시작은 18차 0회).
- 회전 표본 파일은 요청마다 쓰고 지운다. 판정 프로세스가 읽기 전에 죽으면 파일이 남을 수 있다(같은 이름은 다음 요청이 덮어씀).
