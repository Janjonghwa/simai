package com.ssafy.simai.domain.avatar;

import java.time.Instant;
import java.util.UUID;
import tools.jackson.databind.JsonNode;

/**
 * 기록된 avatar_asset 버전 한 줄(V14 avatar_asset 표, docs/design/avatar-pose-stream.md).
 */
public record AvatarAssetRecord(
        AvatarAssetKey key,
        UUID msgId,
        String workerId,
        int identityRevision,
        Integer previousAppearanceVersion,
        String calibrationVersion,
        String modelVersion,
        String meshFormat,
        String meshUri,
        String meshSha256,
        long meshSizeBytes,
        int vertexCount,
        int faceCount,
        int jointCount,
        String jointParentsSha256,
        int pelvisJointIndex,
        String rigSource,
        String textureUri,
        String textureSha256,
        String surfaceMaskUri,
        JsonNode surfaceRatios,
        JsonNode faceRatios,
        String facePolicy,
        JsonNode inputProvenance,
        JsonNode payload,
        Instant createdAt,
        Instant sentAt,
        Instant receivedAt) {

    public String meshFileName() {
        return "GLTF_SKINNED".equals(meshFormat) ? AvatarPaths.MESH_GLTF : AvatarPaths.MESH_GLB;
    }

    public String meshUrl() {
        return AvatarPaths.assetPath(key.worldId(), key.streamEpoch(), key.appearanceVersion(), meshFileName());
    }

    public String textureUrl() {
        return textureUri == null
                ? null
                : AvatarPaths.assetPath(key.worldId(), key.streamEpoch(), key.appearanceVersion(), AvatarPaths.ALBEDO_PNG);
    }

    public String surfaceSourceUrl() {
        return surfaceMaskUri == null
                ? null
                : AvatarPaths.assetPath(
                        key.worldId(), key.streamEpoch(), key.appearanceVersion(), AvatarPaths.SURFACE_SOURCE_PNG);
    }
}
