# 이벤트 클립 업로더: 관제 중 경보 직후 앞뒤 영상 재생 (S15P31A101-748)

사용자 결정(2026-11-16, A안): 업로더를 붙여 관제 중 경보 직후 이벤트 상세에서 앞뒤 영상을 바로 재생한다.

관련 명세·규칙: 5.1(이벤트 클립 링 버퍼, 앞뒤 20초), 11.3(`media_clip`), 11.5(14일 보관·CRITICAL 자동 보존), 12.3(I1 `media_clip`·
`media_clip_retention`), 12.6(`POST /api/uploads/sign`, `GET /api/clips/{id}`), 14(비공개 저장소·서명 주소·열람 기록), 16(클립 얼굴 흐림
안 함·자동 삭제는 EC2), AGENTS 규칙 5(LIVE 만 실제 알림)·6(추가만)·계약 우선. 앞 문서: `server-event-clips.md`(-261, EC2 수신·조회),
`S15P31A101-738-split-side-work.md`(클립 링 버퍼를 검출 프로세스로), `S15P31A101-724-replay-no-live-alerts.md`·`S15P31A101-736-replay-test-switch.md`.

**계약·DB 변경 없음.** `media_clip`·`media_clip_retention` 1.0.0(-169), EVENT_CLIP 서명(-69), EC2 수신·목록·재생 주소(-261)가 이미 있다.
빠져 있던 것은 Tier 0 의 서명 클라이언트·업로드 스레드·I1 송신 등록과 화면의 「영상 준비 중」이다.

## 1. 문제

Tier 0 는 링 버퍼로 클립을 잘랐지만 업로더 없이 조립돼(`EventClipService(uploader=None)`) 잘라낸 클립을 메모리에서 버리고
`blocked_no_uploader` 만 셌다. I1 클라이언트도 `media_clip` 을 보낼 수 있는 종류로 두지 않았다. 그래서 11-04 이후 `media_clip` 0건이다.

## 2. 흐름

```
판정 전이(LIVE·CRITICAL/HIGH 발생·재개·등급 변경) ─▶ EventClipService(링 버퍼·자르기·재다중화)
  └─ enqueue(ExportClip, ClipDraft)  ── 편지함에 넣기만(판정 lock·검출 명령 스레드를 막지 않음)
        ▼  업로드 스레드 tier0-clip-upload (BackgroundClipUploader → ClipUploader)
     POST /api/uploads/sign {purpose: EVENT_CLIP, event_id, clip_id, content_type, size_bytes, sha256}  (Tier 0 토큰)
        ◀ object_key = event-clips/<event_id>/<clip_id>.h264 (서버가 정함), upload_url(5분), headers(형식·크기·해시 서명)
     PUT upload_url (S3, 서명된 헤더 그대로)
     media_clip ─▶ I1 신뢰 큐 ─▶ EC2 MediaClipIngestor(발급과 키·형식·크기·해시·이벤트 대조, 발급 used_at) ─▶ media_clip 한 행
관리자 이벤트 상세 ─▶ GET /api/events/{id}/clips(5초마다) ─▶ 재생 누름 ─▶ GET /api/clips/{id}(서명 GET, 조회 기록) ─▶ MP4 로 감싸 재생
```

| 경로 | 클립 링·자르기 | 서명·PUT | media_clip I1 송신 |
|---|---|---|---|
| 단일(`process_split=false`) | 판정 프로세스 | 판정 프로세스의 업로드 스레드 | `I1ClipRecordSink` → `I1Publisher.send_event` |
| 분리(기본, -738) | 검출 프로세스 | 검출 프로세스의 업로드 스레드 | 검출 → out 큐 `("side", "media_clip", payload)` → 판정 쪽 `RemoteClips` 중계 스레드 → `send_event`(msg_id·순서는 판정 쪽, `event_frame_ready` 와 같은 틀) |

## 3. 서명 경로(EC2)

이미 있는 `POST /api/uploads/sign`(`UploadSignService`, -69)을 그대로 쓴다. 이벤트 프레임 서명(`EventFrameSigner`)과 같은 틀이다.

| 항목 | 값 | 출처 |
|---|---|---|
| 인증 | I1 과 같은 Tier 0 토큰(Bearer). EVENT_CLIP 은 TIER0·DEVICE 만 | `UploadPurpose.EVENT_CLIP` |
| 객체 키 | 서버가 정함: `event-clips/<event_id>/<clip_id><확장자>`. Tier 0 는 응답 키가 이 꼴이 아니면 쓰지 않는다(비재시도 오류) | `UploadSignService.key` |
| 형식 | `video/h264`(`h264-annexb`, 지금 Tier 0 출력) 또는 `video/mp4`. 다른 형식은 400 | `ClipFormat` |
| 크기 상한 | 256 MiB | `UploadPurpose.EVENT_CLIP` |
| 업로드 완료 확인 | 저장소가 서명에 묶인 형식·크기·SHA-256 만 받는다. Tier 0 는 PUT 이 2xx 일 때만 `media_clip` 을 보내고, EC2 는 발급 기록과 키·형식·크기·해시·이벤트가 같을 때만 쓴다 | `server-event-clips.md` |
| 보존 | 기존 정책: 14일(`delete_after`), CRITICAL 자동 보존, 삭제는 EC2 작업(16장 v1.3 수정 19) | 변경 없음 |

## 4. Tier 0 업로더

코드 `ai/services/tier0/clips/async_upload.py`(새), 조립 `service.py`(`_clip_io`·`_attach_clip_upload`), 분리 `split_side_work.py`.

- **판정 루프를 막지 않는다.** 단일 경로는 정리 주기가 판정 lock 을 잡고 `clips.poll()` 을 부르고, 분리 경로는 검출 프로세스 명령 스레드가
  부른다. 그 자리에서 서명·PUT(수 초~수십 초)을 하지 않는다. `enqueue`·보존 표시 상향은 편지함에 넣고 바로 돌아오고, `poll` 은 스레드를
  깨우기만 한다. 스레드(0.5 s 주기)가 편지함을 `ClipUploader` 로 넘기고 대기열을 돈다.
- **재시도·상한·큐 상한**은 기존 `ClipUploader`(-57) 그대로: 실패마다 1 s 부터 두 배(최대 60 s), 시도마다 새 서명 주소, 8번 실패면 임시
  저장소에 두고 10분마다(없으면 버림), 메모리 256 MiB 를 넘으면 임시 저장소로. 편지함도 256 MiB 상한(넘으면 버리고 `inbox_dropped`).
- **EC2 단절**: 서명 연결 실패·시간 초과·502~504 는 `SignerUnavailable` → 클립을 `<state_dir>/clip-spool` 에 먼저 쓰고 시도 상한에 넣지 않는다.
  401·403·409·400 은 다시 보내도 같아 비재시도로 표시한다(상한까지만 시도). 기동하면 임시 저장소에 남은 클립을 다시 올린다(`restore_spooled`).
- **I1 기록 출구**: `media_clip`·`media_clip_retention` 을 I1 신뢰 큐로(끊겨도 보관). 큐가 차면 업로더가 다음 poll 에 기록만 다시 낸다
  (다시 올리지 않는다). I1 클라이언트의 `UPSTREAM_TYPES`·`RELIABLE_TYPES`·`MESSAGE_SCHEMA_VERSIONS`(1.0.0)·발행기 `EVENT_TYPES` 에 등록했다.
- **켜는 조건**: LIVE 입력 + I1 + 설정 `event_clips_enabled`(기본 true). file 입력(REPLAY)·SIM·I1 없음이면 지금처럼 업로더 없이 센다.
- **상태**: `Tier0Service.status()["event_clips"]`(자르기·업로드·편지함·중계 수). 분리 경로는 검출 프로세스 하트비트 `side.clip_upload`.
- **정지**: 진행 중인 PUT 을 기다리지 않는다(최대 1 s, 데몬 스레드). 임시 저장소에 둔 클립은 다음 기동이 올린다. 메모리에만 있던 클립은 잃는다.

### 함께 고친 것: 자르기 전 상향의 event_sequence

HIGH 로 시작해 자르기 전에 CRITICAL 로 오르면 등급만 CRITICAL 로 바꾸고 `event_sequence` 는 HIGH 발생 전이로 남아 있었다. EC2 는 클립 등급이
그 전이의 등급과 같은지 보므로(`MediaClipIngestor`) 이 기록은 계약 위반으로 버려진다. 계약 설명("자르기 전 상향이면 그 전이")대로 상향 전이의
`event_sequence` 로 바꾼다(`EventClipService._escalate`).

## 5. 재생 입력(FILE/SYNTHETIC) 처리

| 입력 | 이벤트 | 클립 | 근거 |
|---|---|---|---|
| Tier 0 file 입력(`input_mode=file`) | REPLAY | 만들지 않음(지금 그대로) | `clip_trigger_for` 는 LIVE 전이만, 계약 `media_clip.source` 는 LIVE 만 |
| 노트북 중계가 녹화 파일·합성 영상을 보냄(-724 시험 입력) | LIVE 로 기록·표시(-724 결정) | **올린다** | 아래 |

중계 재생 클립을 올리기로 했다(권장안). 근거:
- 규칙 5 는 알림 채널(현장 경보 소리·워치·Push)을 막는 규칙이다. 클립은 알림이 아니라 관리자가 상세에서 직접 여는 기록이다.
- 이벤트가 LIVE 로 기록·표시되는 동안 그 클립만 빼면 상세가 「준비 중」 뒤 빈칸이 되어 오히려 헷갈린다. 리허설(-736)에서 경보 → 상세 → 영상 재생
  흐름 전체를 확인하려면 올라가야 한다. 영상 내용은 실제로 들어온 입력 그대로라 지어낸 것이 없다.
- 재생 표시는 이벤트와 같이 따라간다: Tier 0 는 하트비트 내용을 읽지 않아 입력 종류를 모르고, 이벤트·클립 `source` 를 바꾸는 것은 I1 LIVE
  계약 의미 변경(Lead 결정, -724 남은 일 1)이다. 관제 화면에는 이미 「녹화 재생 중」 배지(-731·-736)가 뜬다. -724 남은 일 1 로 이벤트에 재생
  표시가 붙으면 클립은 그 이벤트 상세 안에 있으므로 같은 표시 아래 보인다(클립에 따로 칸을 두지 않는다).
- 보관은 같다(14일, CRITICAL 자동 보존). 첫 운영 배포 때 DB·저장소 초기화 결정(시연 전 리허설 데이터 정리)과도 충돌하지 않는다.

## 6. 화면(관리자 이벤트 상세)

`front/apps/admin/src/features/clips/`. 관제 경보 카드·이벤트 목록에서 여는 사건 상세 창의 「이벤트 클립」 칸(관리자만, 기존).

| 상태 | 표시 |
|---|---|
| 지금 위험 구간(재개 횟수)의 클립이 아직 없고, 이벤트가 바뀐 지(`updated_at`, 없으면 `raised_at`) 2분이 안 됨 | 「영상 준비 중」 |
| 클립 있음 | 카메라별 줄 + 「재생」(서명 GET → 브라우저가 MP4 로 감싸 재생, 기존) |
| 2분이 지나도 없음 | 지금처럼 「—」. 목록은 5초마다 다시 받으므로 늦게 와도 나타난다 |
| 목록 실패 | 지금처럼 「클립 목록을 받지 못했습니다」(준비 중을 띄우지 않는다) |

명세와 다른 점(보고): 과제 문구의 「없으면 지금처럼 사진(이벤트 프레임)만」에 해당하는 화면이 없다. 이벤트 프레임(`event_frame_ready`)은 VLM 근거
입력으로만 쓰이고 관리자 화면·EC2 조회 API 에 사진 표시가 없다. 이 티켓에서는 「없으면 지금처럼(—)」으로 두었다(남은 일 2).

## 7. 명세에 없어 정한 수치

| 항목 | 값 | 근거 |
|---|---|---|
| 서명 시간 제한 | 5 s | EC2 응답은 수십 ms. 넘기면 단절로 보고 임시 저장소에 둔다 |
| PUT 시간 제한 | 60 s | 카메라 클립 40초 × 약 4 Mbps ≈ 20 MB. 3 MB/s 로도 끝나게(이벤트 프레임 JPEG 의 5 s 는 너무 짧다) |
| 업로드 스레드 주기 | 0.5 s | 백오프 최소(1 s)보다 짧게. poll 이 깨우면 바로 돈다 |
| 편지함 상한 | 256 MiB | `RetryPolicy.max_queue_bytes` 와 같게(이벤트 3건 × 4대 × 20 MB 를 넘는 몰림) |
| 판정 쪽 중계 대기 | 256건 | 기록은 1 KB 남짓, 이벤트 하나에 4건. 차면 버리고 남긴다(올린 객체는 EC2 고아 정리, -72) |
| 「영상 준비 중」 창 | 2분 | 사후 20 s + 마감 여유 5 s + 업로드(4대 × 20 MB)·I1 왕복 |

## 8. 버린 대안

| 대안 | 버린 이유 |
|---|---|
| 정리 주기(판정 lock 안)에서 `ClipUploader.poll` 그대로 | PUT 이 최대 수십 초 판정 lock 을 잡는다(분리 경로는 검출 명령 스레드를 막는다) |
| 업로더에 lock 을 씌워 공유 | 스레드가 PUT 중 lock 을 쥐면 `enqueue` 가 기다린다. 편지함이면 넣는 쪽은 기다리지 않는다 |
| 분리 경로에서 클립 바이트를 판정 프로세스로 넘겨 거기서 업로드 | 20 MB × 4대를 IPC 로 넘긴다. 링·자르기가 있는 검출 프로세스에서 바로 올린다(이벤트 프레임과 같은 결정) |
| EC2 가 업로드 중 상태(미사용 발급)를 목록에 실음 | 자르기 전 25초 동안은 발급이 없어 시간 창이 어차피 필요하다. API·서버 변경 없이 화면 계산으로 충분하다 |
| 중계 재생 클립을 올리지 않음 | 5절 |

## 9. 시험

- `ai/tests/services/tier0/test_tier0_clip_upload.py`(새): 서명 요청 본문·토큰·서버 키만 받음·평문 http 거부·다른 클립 키 거부, 연결 실패·
  502 는 단절(임시 저장)·409/401 은 비재시도, PUT 이 막혀 있어도 넘기는 쪽은 바로 돌아옴(판정 lock 비차단), 실패 2번 뒤 백오프로 재시도해
  기록 한 번(`upload_attempts=3`, 시도마다 새 서명), I1 큐 가득이면 기록만 다시(재업로드 없음), 올린 뒤 CRITICAL 상향은 `media_clip_retention`,
  편지함 상한, I1 신뢰 큐 등록·버전, 분리 경로(검출 쪽 업로드 → 판정 쪽 중계, I1 큐 잠깐 가득, CRITICAL 자동 보존, 임시 저장소 비움),
  업로더 없는 분리 경로, 조립(`_clip_io` 켜는 조건, 단일 경로 업로더·분리 경로 중계·I1 없음).
- `test_event_clips.py`: 자르기 전 상향 클립의 `event_sequence` 가 상향 전이를 가리킴.
- `test_tier0_split_side_work.py`: 대역에 `on_side` 추가(RemoteClips 가 기록 중계를 등록).
- front `clips.test.ts`(준비 중 계산: 창 안·밖, 재개 구간, 시각 모름), `EventClips.test.tsx`(방금 난 이벤트는 「영상 준비 중」, 올라오면 클립으로).
- EC2: 변경 없음(서명 `UploadSignIntegrationTest`, 수신 `MediaClipIntegrationTest` 그대로).

## 10. 남은 일·잔여 위험

1. 실측 전: 다음 GPU 세션에서 실제 S3 로 PUT·`media_clip` 저장·재생까지 본다(18차 기준 카메라 비트레이트로 클립 크기·업로드 시간, 검출
   프로세스 GIL 부담). 로그 `media_clip_queued`, 상태 `event_clips.upload`.
2. 이벤트 상세에 근거 사진(이벤트 프레임) 표시는 없다(6절). 필요하면 EC2 조회 API 와 화면을 따로 만든다.
3. 이벤트·클립의 재생 입력 표시는 -724 남은 일 1(이벤트 `source`·입력 출처 표시, Lead 결정)을 따른다.
4. 정지·검출 프로세스 재시작 때 메모리에만 있던 클립(임시 저장 전)은 잃는다. 임시 저장소는 Pod 디스크라 Pod 를 지우면 사라진다(볼륨 없음).
5. 명세 12.6 API 표에 `GET /api/events/{id}/clips` 행 추가는 -261 남은 일 그대로(명세 정리).
