# 영상 조각 stream_epoch 를 int64 로 받기 (S15P31A101-767)

관련: S15P31A101-668(관제 녹화 S3 업로드·지난 작업 다시 보기), S15P31A101-330(`video_segment`), S15P31A101-754(같은 문제의 `media_clip` 수정, V82).
계약 `contracts/messages/recording_segment`·`video_segment`, `contracts/common/defs` 의 stream_epoch(상한 없는 0 이상 정수).

## 문제

노트북 중계는 카메라별 첫 `stream_epoch` 를 세션 시작 UTC ms(약 1.79e12)로 잡는다(`gateway/relay/src/simai_relay/epoch_store.py`,
재시작해도 epoch 가 줄지 않게). 계약과 게이트웨이는 이 값을 그대로 쓰지만 EC2 는 int32 로 가정했다.

- `RecordingService.register`: `stream_epochs[0].canConvertToInt()` 가 거짓이라 400 `INVALID_RECORDING_SEGMENT`.
- `VideoSegmentIngestor`(Tier 0 I1 경로): 같은 검사로 계약 위반 처리.
- 표 `video_segment.stream_epochs integer[]`(V15).

그래서 -668 관제 녹화 업로드는 S3 PUT 까지 끝난 뒤 등록에서 전부 거절됐다(운영 2026-11-13 ~ 11-16, 서명 241건·등록 0건,
`recording_upload_progress` live-20261116-130010 실패 221/221). 시험은 계약 예시의 작은 epoch(0, 12)만 써서 잡지 못했다.

## 고친 것

| 위치 | 내용 |
|---|---|
| `V84__video_segment_stream_epochs_bigint.sql` | `video_segment.stream_epochs` 를 `bigint[]` 로 넓힘(값 그대로) |
| `RecordingService.register` | `canConvertToLong`·`asLong`, `CAST(... AS bigint[])` |
| `VideoSegmentIngestor` | 같은 변경 |
| `RecordingSegmentIntegrationTest`·`VideoSegmentIntegrationTest` | 조각 본문에 실제 크기 epoch(1794801615041)를 넣고 저장값을 확인 |

`MediaClipIngestor` 는 -754 에서 이미 고쳤다.

## 복구(운영)

- 배포 뒤 게이트웨이에서 다시 올려야 한다. 거절된 조각은 녹화 폴더 `upload-state.json` 에 `rejected` 로 남아 다시 시도하지 않고,
  녹화 상태도 `done` 일 수 있다. 각 `C:\simai-local\recordings\live-*\upload-state.json` 을 지우거나 이름을 바꾼 뒤
  `simai-gateway.ps1 upload-recordings` 를 돌린다. 같은 segment_id·같은 키라 S3 객체는 덮어쓴다.
- 보관 14일은 첫 프레임 촬영 시각 기준이다. 11/13 녹화는 11/27 전에 다시 올려야 한다.
- 다시 올리지 않은 객체는 쓰이지 않은 발급으로 남아 발급 만료 14일 뒤 삭제 작업이 지운다.

## 남은 일

- 게이트웨이가 4xx 거절을 영구 실패(`rejected`)로 적어 서버 수정 뒤에도 자동으로 다시 시도하지 않는다. 서버 쪽 결함으로 거절된 경우를
  다시 시도할 수단(예: `--retry-rejected`)은 게이트웨이 몫으로 넘긴다.
- 등록 거절은 서버 로그에 남지 않아 원인을 노트북 로그로만 알 수 있었다. 거절 사유를 WARN 으로 남길지는 따로 정한다.
