# media_clip 이 EC2 에서 모두 버려지던 문제와 클립 업로드 편지함 (S15P31A101-754)

19차 GPU 세션(2026-11-16, 분리 경로)에서 발견. 앞 문서: `S15P31A101-748-event-clip-upload.md`(Tier 0 업로더), `server-event-clips.md`(-261, EC2 수신).
관련 명세: 5.1(이벤트 클립 링 버퍼), 11.3(`media_clip`), 12.3(I1), 12.7(`stream_epoch`). 계약 `messages/media_clip` 1.0.0.

## 1. 현상

- Tier 0: `media_clip_queued clip_id=… camera=cam_B` 등 업로드·I1 송신은 정상.
- EC2: `Tier0I1WebSocketHandler : I1 계약 위반이라 버림: msg_type=media_clip … reason=JsonNodeException` 이 클립마다, `media_clip` 0행.
  같은 시각 `event_frame_ready` 등 다른 메시지는 정상.
- Tier 0(검출 프로세스): `클립 업로드 편지함이 가득 차 …(cam_D) 를 버렸다` 1회.

## 2. 원인

### media_clip: EC2 가 stream_epoch 를 int 로 읽었다

| 단계 | 결과 |
|---|---|
| Tier 0 payload(`record.py` `to_payload`, 단일 `I1ClipRecordSink`·분리 `_SideClipSink`→`RemoteClips` 중계, `Envelope.make` 봉투) | 계약 예시와 칸·타입이 같다. 다른 점은 값뿐: `stream_epochs` 가 `[1763…000]`(13자리) |
| 그 값의 출처 | 노트북 중계 `gateway/relay` `epoch_store.reserve(camera_id, floor=세션 시작 UTC ms)`. 재시작·시계 역행에도 epoch 가 줄지 않게 첫 epoch 를 세션 시작 ms 로 잡는다(-83) |
| 계약 | `stream_epochs.items`: `integer, minimum 0`, 상한 없음(`common/defs` `frame_id.stream_epoch` 와 같음). 13자리 값은 계약에 맞다 |
| EC2 스키마 검사(`ContractSchemas`, networknt) | 통과 |
| EC2 행 만들기 `MediaClipIngestor.row`(옛 231행) `p.get("stream_epochs").values().forEach(v -> epochs.add(v.asInt()))` | Jackson 3 의 `asInt()` 는 int 범위를 넘는 수를 강제 변환하지 않고 `JsonNodeException` 을 던진다 → 처리기가 계약 위반으로 센다 |
| DB `media_clip.stream_epochs integer[]`(V1) | 읽기를 고쳐도 int4 라 넘친다 |

계약 시험(`test_event_clips.py`)·EC2 통합 시험(`MediaClipIntegrationTest`)은 epoch 0·12 로만 돌아 못 잡았다.
`event_frame_ready` 에는 epoch 칸이 없어 영향이 없었다.

**누구 잘못인가:** 계약은 맞고(상한 없는 정수), 보내는 쪽도 계약대로다. EC2 가 계약보다 좁게(int4) 받았다. 그래서 **계약은 바꾸지 않고**
EC2 를 고친다.

### 편지함: 업로드 스레드가 한 바퀴 시작에만 편지함을 비웠다

`BackgroundClipUploader.run_once` 는 편지함을 대기열로 옮긴 뒤 `ClipUploader.poll()` 로 대기열 전체를 차례로 올린다. 한 바퀴는 클립마다
서명(≤ 5 s)과 PUT(≤ 60 s)을 기다리므로, 대기열이 길거나 저장소가 느리면 몇 분이 걸린다. 그동안 잘린 새 클립은 편지함에만 쌓이고
대기열의 메모리 상한(넘으면 디스크 임시 저장소)으로 넘어가지 못한다. 편지함 256 MiB 를 넘는 순간 버린다. 한 이벤트에 4대가 같이 잘리므로
버려지는 것은 그 이벤트의 마지막 카메라(cam_D)가 된다.

## 3. 고친 것

| 곳 | 변경 |
|---|---|
| `server/.../clip/MediaClipIngestor.java` | `stream_epochs` 를 `long` 으로 읽는다(정수·int64 가 아니면 계약 위반 문구로 거부). `event_sequence`·`resume_count`·`frame_count` 도 `asInt()` 대신 `I1Json.integer`(int 를 넘으면 `JsonNodeException` 이 아니라 칸 이름이 든 계약 위반). INSERT 는 `CAST(… AS bigint[])` |
| `V82__media_clip_stream_epochs_bigint.sql` | `ALTER TABLE media_clip ALTER COLUMN stream_epochs TYPE bigint[]`. 값을 넓히기만 한다(기존 행 그대로) |
| `contracts/examples/media_clip.json` | 예시 `stream_epochs` 를 `[12]` → `[1760340000000]`(중계 실제 크기). 스키마·버전은 그대로. EC2 통합 시험 전부가 이 예시를 고쳐 보내므로 큰 epoch 를 늘 지난다 |
| `ai/services/tier0/clips/upload.py` | `ClipUploader.poll(between_jobs=None)`: 시도(서명·PUT) 앞마다 `between_jobs` 를 부른다(실패는 그 클립 시도와 따로 센다) |
| `ai/services/tier0/clips/async_upload.py` | 업로드 스레드가 `poll(between_jobs=self._drain)` 으로 시도 앞마다 편지함을 대기열로 옮긴다. 편지함 상한은 256 MiB 그대로 |
| `docs/design/db-tables.md` | `media_clip.stream_epochs` int[] → bigint[] |

### 편지함 크기 근거

- 편지함에는 이제 **시도 하나(서명 5 s + PUT 60 s 상한, ≤ 65 s) 동안 새로 잘린 클립만** 쌓인다. 나머지는 대기열로 가고, 대기열은 메모리
  256 MiB 를 넘으면 `<state_dir>/clip-spool` 디스크로 내린다(-57·-748 그대로). 디스크도 못 쓰면 그때 버린다(기존 동작).
- 클립 하나 = 40 s × 대당 약 1 Mbps(명세 3.3 실측 0.86~1.1 Mbps) ≈ 5 MB. 넉넉히 4 Mbps 로 잡아도 20 MB.
- 256 MiB 는 65 s 안에 20 MB 클립 12개(이벤트 3건 × 4대), 실측 비트레이트면 50개 남짓을 받는다. 한 위험 구간은 클립을 한 번만 만들고
  (재개는 새 구간) 사후 구간 20 s 뒤에 4대를 한꺼번에 자르므로, 65 s 사이 새 이벤트 3건 이상이 겹쳐야 닿는다. 그래서 크기는 그대로 두고
  처리(시도마다 옮기기)만 바꿨다.

## 4. 시험

| 시험 | 본 것 |
|---|---|
| `ai/tests/services/tier0/test_tier0_clip_upload.py::test_single_path_media_clip_message_matches_contract_with_relay_epoch` | 단일 경로: 실제 업로드 스레드 → `I1ClipRecordSink` → `Envelope.make` → JSON 왕복 메시지(`media_clip` 2·`media_clip_retention` 2)를 계약 jsonschema 로 검증. 중계 epoch(세션 시작 ms)가 그대로. `media_clip` payload 가 계약 예시와 칸·JSON 타입이 같고 예시 epoch 가 int32 를 넘음 |
| `…::test_split_path_media_clip_message_matches_contract_with_relay_epoch` | 분리 경로: 검출 프로세스 `ChildSideWork` 업로드 → out 큐 → `RemoteClips` 중계 → 봉투. 카메라 4대 같은 검증 |
| `…::test_inbox_is_drained_before_every_attempt_so_clips_arriving_during_puts_are_not_dropped` | 대기열에 이벤트 1의 4대가 있고 PUT 마다 이벤트 2 클립이 하나씩 들어올 때(편지함 상한 = 클립 1.5개) 버림 0, PUT 중 편지함 ≤ 1, 8개 모두 기록. 옛 처리로 돌리면 3개를 버려 실패함을 확인 |
| `server/.../MediaClipIntegrationTest::중계가_세션_시작_ms_로_센_stream_epoch_와_구간_중_재연결_클립도_값_그대로_저장한다` | 실제 포트 I1 로 큰 epoch 두 개(`[e, e+1]`) 클립 → `media_clip` 행 `{e,e+1}`, invalid 지표 그대로, 목록 API 에 보임. 나머지 9개 시험도 큰 epoch 예시로 통과 |

EC2 시험은 로컬 임시 `timescale/timescaledb-ha:pg17-all` 컨테이너(빈 DB, V1~V82 적용)로 `domain.clip.*`(19개)·`IncidentSummaryIntegrationTest`
를 돌렸다. 고치기 전 `MediaClipIngestor` 로 돌리면 `MediaClipIntegrationTest` 10개가 모두 `reason=JsonNodeException` 으로 실패해 현장 로그와 같다.

## 5. 버린 대안

- **Tier 0 가 epoch 를 작게 바꿔 보내기**: 계약에 맞는 값을 보내는 쪽을 고치게 되고, `(camera_id, stream_epoch, frame_sequence)` 식별자(12.7)가
  다른 메시지와 달라진다.
- **계약에 `maximum` 추가**: 중계의 재시작 안전(세션 시작 ms floor)을 깨야 한다. I1 LIVE 계약 메이저 변경이다.
- **편지함 상한만 키우기**: 한 바퀴가 길어지는 한 결국 넘친다. 메모리만 더 쓰고 디스크 임시 저장소를 쓰지 못한다.
- **넘치면 enqueue 자리에서 디스크에 쓰기**: enqueue 는 판정 lock 안(단일 경로)·검출 명령 스레드(분리 경로)에서 불려 수십 MB 쓰기를 거기서 하면 안 된다.

## 6. 잔여 위험·넘긴 일

- **`video_segment`(V15)·녹화 업로드(`RecordingService`)도 `stream_epochs integer[]` 이고 `VideoSegmentIngestor` 는 int 를 넘는 epoch 를
  계약 위반으로 거부한다.** 지금 Tier 0·중계가 `video_segment` 를 보내지 않아 이번 범위에서 뺐다. 중계 epoch 로 보내는 날 같은 수정이 필요하다(후속 티켓).
- 19차 세션까지 버려진 `media_clip` 은 되살릴 수 없다(Tier 0 는 보낸 기록을 다시 보내지 않는다). 객체는 S3 에 남고 `upload_grant.used_at` 이 비어
  고아 정리가 지운다.
- 업로드 처리량 자체(이벤트가 계속 몰려 대기열이 늘면 디스크 임시 저장소가 쌓임)는 그대로다. 대기열·임시 저장 수는 `status()["event_clips"]` 로 본다.
- 배포 순서: EC2(V82 + 수신기)만 배포하면 된다. Tier 0 편지함 수정은 독립(옛 EC2 에서도 동작은 같다).
