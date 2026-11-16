-- video_segment.stream_epochs 를 bigint[] 로 넓힌다(S15P31A101-767, media_clip 은 V82 S15P31A101-754).
-- 계약 stream_epoch 는 상한 없는 정수이고, 노트북 중계는 첫 epoch 를 세션 시작 UTC ms(약 1.79e12)로 잡는다
-- (gateway/relay epoch_store.reserve floor). int4 로는 담지 못해 관제 녹화 조각 등록(-668)이 모두 INVALID_RECORDING_SEGMENT 로 거절됐다.
-- 값은 그대로 넓히기만 한다.
ALTER TABLE video_segment ALTER COLUMN stream_epochs TYPE bigint[];
COMMENT ON COLUMN video_segment.stream_epochs IS
    '조각에 들어간 I0 stream_epoch(계약 video_segment·recording_segment). 중계는 세션 시작 UTC ms 부터 세므로 int8';
