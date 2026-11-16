# video_segment·녹화 조각 계약 예시의 stream_epoch 를 중계 실제 크기로 (S15P31A101-776)

앞 문서: `S15P31A101-754-media-clip-contract.md` 6절(넘긴 일), `S15P31A101-767-video-segment-epoch-bigint.md`.
관련 명세: 12.7(`stream_epoch`). 계약 `messages/video_segment` 1.0.0, `data/recording_segment` 1.0.0(스키마·버전 그대로).

## 1. 확인한 것

-754 6절이 넘긴 일(`video_segment`·녹화 업로드의 `stream_epochs integer[]`, `VideoSegmentIngestor` 의 int 거부)은 이 티켓을 시작할 때
**-767 이 이미 dev 에 머지**해 두었다.

| 곳 | -767 상태 |
|---|---|
| `V84__video_segment_stream_epochs_bigint.sql` | `video_segment.stream_epochs` → `bigint[]` |
| `VideoSegmentIngestor`, `RecordingService.register` | `canConvertToLong`·`asLong`, `CAST(… AS bigint[])` |
| `VideoSegmentIntegrationTest`, `RecordingSegmentIntegrationTest` | 13자리 epoch(1794801615041) 수신·저장 확인 |

그래서 새 Flyway 버전은 만들지 않았다(같은 칸을 두 번 바꾸게 된다).

## 2. 고친 것

| 곳 | 변경 |
|---|---|
| `contracts/examples/video_segment.json` | `stream_epochs` `[12]` → `[1760340000000]`(-754 의 `media_clip` 예시와 같은 크기, 조각 시작보다 앞) |
| `contracts/examples/recording_segment.json` | `stream_epochs` `[0]` → `[1794528900000]`(녹화 `live-20261113-091500` 의 세션 시작 UTC ms) |
| `docs/design/db-tables.md` | `video_segment` 행 비고에 `stream_epochs bigint[]`(V84) |

예시가 작으면 예시를 그대로 쓰는 시험·도구가 int 가정을 다시 통과시킨다. 두 EC2 시험은 지금 예시 값을 덮어쓰지만, 예시 자체도 실제 크기여야
나중에 덮어쓰기를 빼거나 다른 소비자가 붙어도 같은 결함을 잡는다.

## 3. 잔여 위험

- `recording_manifest*.json`·`frame_header*.json`·`codec_info.json` 등 다른 예시의 `stream_epoch` 는 아직 0·1·12 다. 이 값들을 int 로
  받는 EC2 수신기는 찾지 못했다(`stream_epoch` 를 읽는 곳 grep, `media_clip`·`video_segment`·녹화 등록은 long). 범위 밖이라 두었다.
