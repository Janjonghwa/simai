-- media_clip.stream_epochs 를 bigint[] 로 넓힌다(S15P31A101-754, docs/design/S15P31A101-754-media-clip-contract.md).
-- 계약 stream_epoch 는 상한 없는 정수(common/defs frame_id)이고, 노트북 중계는 첫 epoch 를 세션 시작 UTC ms(약 1.76e12)로 잡는다
-- (gateway/relay epoch_store.reserve floor). int4 로는 담지 못해 19차 GPU 세션의 media_clip 이 모두 버려졌다. 값은 그대로 넓히기만 한다.
ALTER TABLE media_clip ALTER COLUMN stream_epochs TYPE bigint[];
COMMENT ON COLUMN media_clip.stream_epochs IS
    '클립에 들어간 I0 stream_epoch(계약 media_clip). 중계는 세션 시작 UTC ms 부터 세므로 int8';
