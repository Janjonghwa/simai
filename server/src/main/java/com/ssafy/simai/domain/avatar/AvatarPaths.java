package com.ssafy.simai.domain.avatar;

/**
 * EXP-02 아바타 포즈 스트림(I6) 및 뷰어 아바타 자산 REST 경로(명세 12.2·12.4·14, docs/design/avatar-pose-stream.md, S15P31A101-325).
 */
public final class AvatarPaths {

    /** I6 WebSocket(GPU 분석 워커 → EC2, 워커 토큰 인증). Nginx location /ws 가 그대로 서버로 넘긴다. */
    public static final String WORKER_WS = "/ws/worker/avatar";

    /** 관제 앱이 아바타 메시·텍스처·표면 출처 지도를 내려받는 같은 출처 REST 앞부분(JWT 인증). */
    public static final String AVATARS_API = "/api/avatars";

    public static final String MESH_GLB = "mesh.glb";
    public static final String MESH_GLTF = "mesh.gltf";
    public static final String ALBEDO_PNG = "albedo.png";
    public static final String SURFACE_SOURCE_PNG = "surface_source.png";

    private AvatarPaths() {
    }

    /** 계약 avatar_frame.schema.json 의 avatar_rest_path 형식(/api/avatars/{world_id}/epochs/{stream_epoch}/versions/{appearance_version}/{file}) */
    public static String assetPath(String worldId, long streamEpoch, int appearanceVersion, String file) {
        return AVATARS_API
                + "/" + worldId
                + "/epochs/" + streamEpoch
                + "/versions/" + appearanceVersion
                + "/" + file;
    }
}
