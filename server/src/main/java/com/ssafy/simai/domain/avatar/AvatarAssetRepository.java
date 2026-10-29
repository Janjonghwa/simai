package com.ssafy.simai.domain.avatar;

import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

/**
 * avatar_asset 버전 기록(V14 avatar_asset 표, 추가만).
 * <ul>
 * <li>(world_id, stream_epoch, appearance_version) 으로 한 번만 기록한다.</li>
 * <li>재접속·10초 주기 재알림으로 같은 키가 다시 오면 처음 기록을 유지하고, 해시가 다르면 경고를 남긴다.</li>
 * <li>다른 자산 키가 이미 쓰는 파일 주소(mesh·texture·surface_source)는 덮어쓰지 못하게 거부한다(AGENTS 규칙 6).</li>
 * </ul>
 */
@Repository
public class AvatarAssetRepository {

    public enum RecordResult {
        STORED,
        DUPLICATE
    }

    private static final Logger log = LoggerFactory.getLogger(AvatarAssetRepository.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Map<AvatarAssetKey, AvatarAssetRecord> cache = new ConcurrentHashMap<>();
    private final Map<String, AvatarAssetKey> uriOwners = new ConcurrentHashMap<>();

    public AvatarAssetRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    @Transactional
    public synchronized RecordResult record(AvatarAssetRecord asset) {
        Optional<AvatarAssetRecord> existing = find(asset.key());
        if (existing.isPresent()) {
            warnIfHashDiffers(existing.get(), asset);
            return RecordResult.DUPLICATE;
        }

        checkDistinctFileUris(asset);

        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("worldId", asset.key().worldId())
                .addValue("streamEpoch", asset.key().streamEpoch())
                .addValue("appearanceVersion", asset.key().appearanceVersion())
                .addValue("msgId", asset.msgId())
                .addValue("workerId", asset.workerId())
                .addValue("identityRevision", asset.identityRevision())
                .addValue("previousAppearanceVersion", asset.previousAppearanceVersion())
                .addValue("calibrationVersion", asset.calibrationVersion())
                .addValue("modelVersion", asset.modelVersion())
                .addValue("meshFormat", asset.meshFormat())
                .addValue("meshUri", asset.meshUri())
                .addValue("meshSha256", asset.meshSha256())
                .addValue("meshSizeBytes", asset.meshSizeBytes())
                .addValue("vertexCount", asset.vertexCount())
                .addValue("faceCount", asset.faceCount())
                .addValue("jointCount", asset.jointCount())
                .addValue("jointParentsSha256", asset.jointParentsSha256())
                .addValue("pelvisJointIndex", asset.pelvisJointIndex())
                .addValue("rigSource", asset.rigSource())
                .addValue("textureUri", asset.textureUri())
                .addValue("textureSha256", asset.textureSha256())
                .addValue("surfaceMaskUri", asset.surfaceMaskUri())
                .addValue("surfaceRatios", asset.surfaceRatios().toString())
                .addValue("faceRatios", asset.faceRatios().toString())
                .addValue("facePolicy", asset.facePolicy())
                .addValue("inputProvenance", asset.inputProvenance().toString())
                .addValue("payload", asset.payload().toString())
                .addValue("createdAt", Timestamp.from(asset.createdAt()))
                .addValue("sentAt", Timestamp.from(asset.sentAt()))
                .addValue("receivedAt", Timestamp.from(asset.receivedAt()));

        List<Integer> inserted = jdbc.queryForList("""
                INSERT INTO avatar_asset (
                    world_id, stream_epoch, appearance_version, msg_id, worker_id,
                    identity_revision, previous_appearance_version, calibration_version, model_version,
                    mesh_format, mesh_uri, mesh_sha256, mesh_size_bytes, vertex_count, face_count,
                    joint_count, joint_parents_sha256, pelvis_joint_index, rig_source,
                    texture_uri, texture_sha256, surface_mask_uri,
                    surface_ratios, face_ratios, face_policy, input_provenance, payload,
                    created_at, sent_at, received_at
                ) VALUES (
                    :worldId, :streamEpoch, :appearanceVersion, :msgId, :workerId,
                    :identityRevision, :previousAppearanceVersion, :calibrationVersion, :modelVersion,
                    :meshFormat, :meshUri, :meshSha256, :meshSizeBytes, :vertexCount, :faceCount,
                    :jointCount, :jointParentsSha256, :pelvisJointIndex, :rigSource,
                    :textureUri, :textureSha256, :surfaceMaskUri,
                    CAST(:surfaceRatios AS jsonb), CAST(:faceRatios AS jsonb), :facePolicy,
                    CAST(:inputProvenance AS jsonb), CAST(:payload AS jsonb),
                    :createdAt, :sentAt, :receivedAt
                )
                ON CONFLICT (world_id, stream_epoch, appearance_version) DO NOTHING
                RETURNING appearance_version
                """, params, Integer.class);

        if (inserted.isEmpty()) {
            find(asset.key()).ifPresent(found -> warnIfHashDiffers(found, asset));
            return RecordResult.DUPLICATE;
        }

        cacheAsset(asset);
        return RecordResult.STORED;
    }

    public Optional<AvatarAssetRecord> find(AvatarAssetKey key) {
        AvatarAssetRecord cached = cache.get(key);
        if (cached != null) {
            return Optional.of(cached);
        }
        List<AvatarAssetRecord> rows = jdbc.query("""
                SELECT world_id, stream_epoch, appearance_version, msg_id, worker_id,
                       identity_revision, previous_appearance_version, calibration_version, model_version,
                       mesh_format, mesh_uri, mesh_sha256, mesh_size_bytes, vertex_count, face_count,
                       joint_count, joint_parents_sha256, pelvis_joint_index, rig_source,
                       texture_uri, texture_sha256, surface_mask_uri,
                       surface_ratios::text AS surface_ratios, face_ratios::text AS face_ratios,
                       face_policy, input_provenance::text AS input_provenance, payload::text AS payload,
                       created_at, sent_at, received_at
                FROM avatar_asset
                WHERE world_id = :worldId AND stream_epoch = :streamEpoch AND appearance_version = :appearanceVersion
                """,
                new MapSqlParameterSource()
                        .addValue("worldId", key.worldId())
                        .addValue("streamEpoch", key.streamEpoch())
                        .addValue("appearanceVersion", key.appearanceVersion()),
                (rs, i) -> mapRow(rs));
        if (rows.isEmpty()) {
            return Optional.empty();
        }
        AvatarAssetRecord loaded = rows.getFirst();
        cacheAsset(loaded);
        return Optional.of(loaded);
    }

    public void evictCache() {
        cache.clear();
        uriOwners.clear();
    }

    private void checkDistinctFileUris(AvatarAssetRecord asset) {
        Set<String> ownUris = new HashSet<>();
        for (String uri : new String[] {asset.meshUri(), asset.textureUri(), asset.surfaceMaskUri()}) {
            if (uri == null) {
                continue;
            }
            if (!ownUris.add(uri)) {
                throw new InvalidI1Message("avatar_asset 안 파일 주소가 서로 겹친다");
            }
            Optional<AvatarAssetKey> owner = findOwnerOfUri(uri);
            if (owner.isPresent() && !owner.get().equals(asset.key())) {
                throw new InvalidI1Message("파일 주소를 다른 자산 버전과 같이 쓴다");
            }
        }
    }

    private Optional<AvatarAssetKey> findOwnerOfUri(String uri) {
        AvatarAssetKey cachedOwner = uriOwners.get(uri);
        if (cachedOwner != null) {
            return Optional.of(cachedOwner);
        }
        List<AvatarAssetKey> rows = jdbc.query("""
                SELECT world_id, stream_epoch, appearance_version
                FROM avatar_asset
                WHERE mesh_uri = :uri OR texture_uri = :uri OR surface_mask_uri = :uri
                LIMIT 1
                """,
                new MapSqlParameterSource("uri", uri),
                (rs, i) -> new AvatarAssetKey(
                        rs.getString("world_id"),
                        rs.getLong("stream_epoch"),
                        rs.getInt("appearance_version")));
        if (rows.isEmpty()) {
            return Optional.empty();
        }
        AvatarAssetKey owner = rows.getFirst();
        uriOwners.putIfAbsent(uri, owner);
        return Optional.of(owner);
    }

    private void cacheAsset(AvatarAssetRecord asset) {
        cache.put(asset.key(), asset);
        uriOwners.put(asset.meshUri(), asset.key());
        if (asset.textureUri() != null) {
            uriOwners.put(asset.textureUri(), asset.key());
        }
        if (asset.surfaceMaskUri() != null) {
            uriOwners.put(asset.surfaceMaskUri(), asset.key());
        }
    }

    private static void warnIfHashDiffers(AvatarAssetRecord existing, AvatarAssetRecord incoming) {
        if (!Objects.equals(existing.meshSha256(), incoming.meshSha256())
                || !Objects.equals(existing.textureSha256(), incoming.textureSha256())) {
            log.warn("avatar_asset 같은 키에 다른 sha256 이 와서 처음 기록을 유지함: world_id={} stream_epoch={} appearance_version={}",
                    incoming.key().worldId(), incoming.key().streamEpoch(), incoming.key().appearanceVersion());
        }
    }

    private AvatarAssetRecord mapRow(ResultSet rs) throws SQLException {
        Integer prev = rs.getObject("previous_appearance_version", Integer.class);
        return new AvatarAssetRecord(
                new AvatarAssetKey(
                        rs.getString("world_id"),
                        rs.getLong("stream_epoch"),
                        rs.getInt("appearance_version")),
                rs.getObject("msg_id", UUID.class),
                rs.getString("worker_id"),
                rs.getInt("identity_revision"),
                prev,
                rs.getString("calibration_version"),
                rs.getString("model_version"),
                rs.getString("mesh_format"),
                rs.getString("mesh_uri"),
                rs.getString("mesh_sha256"),
                rs.getLong("mesh_size_bytes"),
                rs.getInt("vertex_count"),
                rs.getInt("face_count"),
                rs.getInt("joint_count"),
                rs.getString("joint_parents_sha256"),
                rs.getInt("pelvis_joint_index"),
                rs.getString("rig_source"),
                rs.getString("texture_uri"),
                rs.getString("texture_sha256"),
                rs.getString("surface_mask_uri"),
                mapper.readTree(rs.getString("surface_ratios")),
                mapper.readTree(rs.getString("face_ratios")),
                rs.getString("face_policy"),
                mapper.readTree(rs.getString("input_provenance")),
                mapper.readTree(rs.getString("payload")),
                rs.getObject("created_at", OffsetDateTime.class).toInstant(),
                rs.getObject("sent_at", OffsetDateTime.class).toInstant(),
                rs.getObject("received_at", OffsetDateTime.class).toInstant());
    }
}
