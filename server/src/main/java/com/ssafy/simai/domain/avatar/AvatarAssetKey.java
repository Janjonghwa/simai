package com.ssafy.simai.domain.avatar;

/** 아바타 자산 버전 고유 키(world_id, stream_epoch, appearance_version). 워커 재시작(stream_epoch 변경) 뒤 같은 버전 번호와 섞이지 않는다. */
public record AvatarAssetKey(String worldId, long streamEpoch, int appearanceVersion) {
}
