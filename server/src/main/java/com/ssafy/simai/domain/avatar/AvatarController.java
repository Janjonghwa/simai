package com.ssafy.simai.domain.avatar;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.net.URI;
import java.util.regex.Pattern;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

/**
 * 뷰어용 아바타 자산(메시·텍스처·표면 출처 지도) 인증 조회(GET /api/avatars/{worldId}/epochs/{streamEpoch}/versions/{appearanceVersion}/{file},
 * 명세 14장, docs/design/avatar-pose-stream.md, S15P31A101-325).
 * <ul>
 * <li>avatar_frame 의 mesh_url·texture_url·surface_source_url 이 가리키는 같은 출처 경로다.</li>
 * <li>관제 앱 로그인 계정(JWT: VIEWER·FOREMAN·ADMIN)만 호출할 수 있다(SecurityConfig 기본 체인).</li>
 * <li>등록된 avatar_asset 버전의 s3:// 객체 키를 찾아 짧게 사는 GET 서명 주소로 302 리다이렉트한다.</li>
 * </ul>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class AvatarController {

    private static final Pattern SAFE_SEGMENT = Pattern.compile("^[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,63}$");
    private static final String S3_SCHEME = "s3://";

    private final AvatarAssetRepository assets;
    private final ObjectStorage storage;

    public AvatarController(AvatarAssetRepository assets, ObjectStorage storage) {
        this.assets = assets;
        this.storage = storage;
    }

    @GetMapping("/api/avatars/{worldId}/epochs/{streamEpoch}/versions/{appearanceVersion}/{file}")
    public ResponseEntity<Void> download(
            @PathVariable String worldId,
            @PathVariable long streamEpoch,
            @PathVariable int appearanceVersion,
            @PathVariable String file) {
        if (!isSafeSegment(worldId) || !isSafeSegment(file) || streamEpoch < 0 || appearanceVersion < 0) {
            throw new BusinessException(ErrorCode.Avatar.AVATAR_ASSET_NOT_FOUND);
        }
        AvatarAssetRecord asset = assets.find(new AvatarAssetKey(worldId, streamEpoch, appearanceVersion))
                .orElseThrow(() -> new BusinessException(ErrorCode.Avatar.AVATAR_ASSET_NOT_FOUND));

        String objectUri = resolveFileUri(asset, file);
        if (objectUri == null) {
            throw new BusinessException(ErrorCode.Avatar.AVATAR_ASSET_NOT_FOUND);
        }
        String objectKey = extractObjectKey(objectUri);
        SignedRequest signed = storage.signGet(objectKey);
        return ResponseEntity.status(HttpStatus.FOUND)
                .location(URI.create(signed.url()))
                .header(HttpHeaders.CACHE_CONTROL, "no-store")
                .build();
    }

    private static String resolveFileUri(AvatarAssetRecord asset, String file) {
        return switch (file) {
            case AvatarPaths.MESH_GLB -> "GLB_SKINNED".equals(asset.meshFormat()) ? asset.meshUri() : null;
            case AvatarPaths.MESH_GLTF -> "GLTF_SKINNED".equals(asset.meshFormat()) ? asset.meshUri() : null;
            case AvatarPaths.ALBEDO_PNG -> asset.textureUri();
            case AvatarPaths.SURFACE_SOURCE_PNG -> asset.surfaceMaskUri();
            default -> null;
        };
    }

    private static String extractObjectKey(String objectUri) {
        if (!objectUri.startsWith(S3_SCHEME)) {
            throw new BusinessException(ErrorCode.Avatar.AVATAR_ASSET_NOT_FOUND);
        }
        int slash = objectUri.indexOf('/', S3_SCHEME.length());
        if (slash <= S3_SCHEME.length() || slash >= objectUri.length() - 1) {
            throw new BusinessException(ErrorCode.Avatar.AVATAR_ASSET_NOT_FOUND);
        }
        return objectUri.substring(slash + 1);
    }

    private static boolean isSafeSegment(String value) {
        return value != null && SAFE_SEGMENT.matcher(value).matches() && !value.contains("..");
    }
}
