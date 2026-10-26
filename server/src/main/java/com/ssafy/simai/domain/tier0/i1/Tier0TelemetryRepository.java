package com.ssafy.simai.domain.tier0.i1;

import static com.ssafy.simai.domain.tier0.i1.I1Json.*;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.namedparam.SqlParameterSource;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;

/**
 * world_state·health 를 hypertable 에 쓴다(V1: world_track_state·skeleton_3d·system_health, docs/design/db-tables.md 4장).
 * JPA 대신 JDBC 를 쓴다. 10 Hz 로 트랙마다 한 행이고, geometry·다차원 배열·jsonb 를 그대로 넣어야 한다.
 *
 * <p>world_track_state 의 키는 (t_latest_global, world_id) 다. 카메라가 멈추면 t_latest_global 이 그대로인 채 tick 이 계속 오고
 * 트랙 상태(PREDICTED→LOST, σ 증가)는 바뀐다. 같은 키는 나중에 받은 다른 메시지로 덮어써 그 시각의 마지막 상태를 남긴다.
 * 같은 msg_id(재전송)는 바꾸지 않는다. system_health 는 키가 Tier 0 의 sent_at 이라 같은 키는 같은 메시지뿐이다(DO NOTHING).
 *
 * <p>JSON 값은 계약 타입대로인지 보고 넣는다. 틀리면 InvalidI1Message(칸 이름만, 값은 넣지 않음).
 * 배열은 PostgreSQL 배열 문자열로 만들어 CAST 한다. JSON null 은 원소 NULL 로 둔다(NaN 으로 바꾸지 않는다).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class Tier0TelemetryRepository {

    private static final String UPSERT_TRACK = """
            INSERT INTO world_track_state AS w (
                observation_at, world_id, msg_id, gateway_id, t_last_valid, observation_state, track_lifecycle,
                identity_confidence, identity_revision, split_from, aliases, frame_age_ms, position, position_xy_sigma,
                pelvis_xyz, ground_anchor_xyz, ground_anchor_sigma, surface_source, support_surface_id,
                support_surface_validity, contact_validity, z_validity, head_z, head_z_sigma, head_z_validity, velocity,
                cameras, calibration_version, scene_version, config_version, input_provenance, received_at)
            VALUES (
                :observationAt, :worldId, :msgId, :gatewayId, :tLastValid, :observationState, :trackLifecycle,
                :identityConfidence, :identityRevision, :splitFrom, CAST(:aliases AS text[]), :frameAgeMs,
                ST_MakePoint(:x, :y), CAST(:positionXySigma AS real[]),
                CAST(:pelvisXyz AS real[]), CAST(:groundAnchorXyz AS real[]), CAST(:groundAnchorSigma AS real[]),
                :surfaceSource, :supportSurfaceId,
                :supportSurfaceValidity, :contactValidity, :zValidity, :headZ, :headZSigma, :headZValidity,
                CAST(:velocity AS real[]),
                CAST(:cameras AS text[]), :calibrationVersion, :sceneVersion, :configVersion,
                CAST(:inputProvenance AS jsonb), :receivedAt)
            ON CONFLICT (observation_at, world_id) DO UPDATE SET
                msg_id = EXCLUDED.msg_id, gateway_id = EXCLUDED.gateway_id, t_last_valid = EXCLUDED.t_last_valid,
                observation_state = EXCLUDED.observation_state, track_lifecycle = EXCLUDED.track_lifecycle,
                identity_confidence = EXCLUDED.identity_confidence, identity_revision = EXCLUDED.identity_revision,
                split_from = EXCLUDED.split_from, aliases = EXCLUDED.aliases, frame_age_ms = EXCLUDED.frame_age_ms,
                position = EXCLUDED.position, position_xy_sigma = EXCLUDED.position_xy_sigma,
                pelvis_xyz = EXCLUDED.pelvis_xyz, ground_anchor_xyz = EXCLUDED.ground_anchor_xyz,
                ground_anchor_sigma = EXCLUDED.ground_anchor_sigma, surface_source = EXCLUDED.surface_source,
                support_surface_id = EXCLUDED.support_surface_id,
                support_surface_validity = EXCLUDED.support_surface_validity,
                contact_validity = EXCLUDED.contact_validity, z_validity = EXCLUDED.z_validity,
                head_z = EXCLUDED.head_z, head_z_sigma = EXCLUDED.head_z_sigma, head_z_validity = EXCLUDED.head_z_validity,
                velocity = EXCLUDED.velocity, cameras = EXCLUDED.cameras,
                calibration_version = EXCLUDED.calibration_version, scene_version = EXCLUDED.scene_version,
                config_version = EXCLUDED.config_version, input_provenance = EXCLUDED.input_provenance,
                received_at = EXCLUDED.received_at
            WHERE w.msg_id <> EXCLUDED.msg_id AND w.received_at <= EXCLUDED.received_at
            """;

    private static final String UPSERT_SKELETON = """
            INSERT INTO skeleton_3d (observation_at, world_id, joint_layout, joints, visible, validity, joint_source)
            VALUES (:observationAt, :worldId, :jointLayout, CAST(:joints AS real[][]), CAST(:visible AS boolean[]),
                    CAST(:validity AS text[]), CAST(:jointSource AS text[]))
            ON CONFLICT (observation_at, world_id) DO UPDATE SET
                joint_layout = EXCLUDED.joint_layout, joints = EXCLUDED.joints, visible = EXCLUDED.visible,
                validity = EXCLUDED.validity, joint_source = EXCLUDED.joint_source
            """;

    /** 덮어쓴 트랙에 이번에는 골격이 없으면 앞 메시지의 골격을 지운다(트랙과 골격이 다른 메시지에서 오지 않게). */
    private static final String DELETE_SKELETON = """
            DELETE FROM skeleton_3d WHERE observation_at = :observationAt AND world_id = :worldId
            """;

    private static final String INSERT_HEALTH = """
            INSERT INTO system_health (
                reported_at, gateway_id, msg_id, system_health, t_latest_global, pipeline_lag_ms,
                pipeline_lag_lower_bound_ms, capture_to_risk_ms, calibration_state, calibration_version, config_version,
                config_sync, gateway_link, cameras, tier0_warnings, ec2_world_state_drops, gpu_util,
                gpu_memory_used_ratio, active_site_alerts, active_site_alerts_as_of, laptop_clock_offset, received_at)
            VALUES (
                :reportedAt, :gatewayId, :msgId, :systemHealth, :tLatestGlobal, :pipelineLagMs,
                :pipelineLagLowerBoundMs, CAST(:captureToRiskMs AS jsonb), :calibrationState, :calibrationVersion,
                :configVersion, :configSync, :gatewayLink, CAST(:cameras AS jsonb), CAST(:tier0Warnings AS jsonb),
                CAST(:ec2WorldStateDrops AS jsonb), :gpuUtil, :gpuMemoryUsedRatio, CAST(:activeSiteAlerts AS jsonb),
                :activeSiteAlertsAsOf, CAST(:laptopClockOffset AS jsonb), :receivedAt)
            ON CONFLICT (reported_at, gateway_id) DO NOTHING
            """;

    private final NamedParameterJdbcTemplate jdbc;

    public Tier0TelemetryRepository(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * world_state 한 개의 트랙들. 쓰거나 덮어쓴 트랙만 골격을 맞춘다. 트랙이 없는 tick 은 행이 없고 저장으로 친다.
     * @return 바뀐 행이 하나라도 있거나 트랙이 없으면 true, 모두 재전송이라 그대로면 false
     */
    public boolean upsertWorldState(I1Envelope envelope, Instant receivedAt) {
        JsonNode payload = envelope.payload();
        OffsetDateTime observationAt = requiredTime(payload, "t_latest_global");
        JsonNode trackArray = payload.get("tracks");
        if (trackArray == null || !trackArray.isArray()) {
            throw new InvalidI1Message("world_state.tracks 가 배열이 아니다");
        }
        List<JsonNode> tracks = new ArrayList<>(trackArray.values());
        if (tracks.isEmpty()) {
            return true;
        }
        SqlParameterSource[] trackRows = tracks.stream()
                .map(track -> trackRow(envelope, payload, observationAt, track, receivedAt))
                .toArray(SqlParameterSource[]::new);
        // 골격 값도 쓰기 전에 모두 검사한다. 트랙만 쓰고 골격에서 계약 위반이 나면 트랜잭션째 되돌린다
        List<SqlParameterSource> skeletonRows = tracks.stream().map(track -> skeletonRow(observationAt, track)).toList();
        int[] changed = jdbc.batchUpdate(UPSERT_TRACK, trackRows);

        List<SqlParameterSource> upserts = new ArrayList<>();
        List<SqlParameterSource> deletes = new ArrayList<>();
        boolean any = false;
        for (int i = 0; i < tracks.size(); i++) {
            if (changed[i] == 0) {
                continue;
            }
            any = true;
            SqlParameterSource skeleton = skeletonRows.get(i);
            if (skeleton != null) {
                upserts.add(skeleton);
            } else {
                deletes.add(new MapSqlParameterSource()
                        .addValue("observationAt", observationAt)
                        .addValue("worldId", tracks.get(i).get("world_id").stringValue()));
            }
        }
        jdbc.batchUpdate(UPSERT_SKELETON, upserts.toArray(SqlParameterSource[]::new));
        jdbc.batchUpdate(DELETE_SKELETON, deletes.toArray(SqlParameterSource[]::new));
        return any;
    }

    /** @return 새로 썼으면 true, 같은 (sent_at, gateway_id) 가 이미 있으면 false */
    public boolean insertHealth(I1Envelope envelope, Instant receivedAt) {
        JsonNode p = envelope.payload();
        int rows = jdbc.update(INSERT_HEALTH, new MapSqlParameterSource()
                .addValue("reportedAt", utc(envelope.sentAt()))
                .addValue("gatewayId", envelope.sourceId())
                .addValue("msgId", envelope.msgId())
                .addValue("systemHealth", text(p, "system_health"))
                .addValue("tLatestGlobal", time(p, "t_latest_global"))
                .addValue("pipelineLagMs", integer(p, "pipeline_lag_ms"))
                .addValue("pipelineLagLowerBoundMs", integer(p, "pipeline_lag_lower_bound_ms"))
                .addValue("captureToRiskMs", json(p, "capture_to_risk_ms"))
                .addValue("calibrationState", text(p, "calibration_state"))
                .addValue("calibrationVersion", text(p, "calibration_version"))
                .addValue("configVersion", text(p, "config_version"))
                .addValue("configSync", text(p, "config_sync"))
                .addValue("gatewayLink", text(p, "gateway_link"))
                .addValue("cameras", json(p, "cameras"))
                .addValue("tier0Warnings", json(p, "tier0_warnings"))
                .addValue("ec2WorldStateDrops", json(p, "ec2_world_state_drops"))
                .addValue("gpuUtil", number(p, "gpu_util"))
                .addValue("gpuMemoryUsedRatio", number(p, "gpu_memory_used_ratio"))
                .addValue("activeSiteAlerts", json(p, "active_site_alerts"))
                .addValue("activeSiteAlertsAsOf", time(p, "active_site_alerts_as_of"))
                .addValue("laptopClockOffset", json(p, "laptop_clock_offset"))
                .addValue("receivedAt", utc(receivedAt)));
        return rows > 0;
    }

    private static SqlParameterSource trackRow(
            I1Envelope envelope, JsonNode payload, OffsetDateTime observationAt, JsonNode t, Instant receivedAt) {
        if (!t.isObject()) {
            throw new InvalidI1Message("world_state.tracks 원소가 객체가 아니다");
        }
        List<Double> position = vector(t, "position_xy", 2, true);
        return new MapSqlParameterSource()
                .addValue("observationAt", observationAt)
                .addValue("worldId", requiredText(t, "world_id"))
                .addValue("msgId", envelope.msgId())
                .addValue("gatewayId", envelope.sourceId())
                .addValue("tLastValid", time(t, "t_last_valid"))
                .addValue("observationState", text(t, "observation_state"))
                .addValue("trackLifecycle", text(t, "track_lifecycle"))
                .addValue("identityConfidence", text(t, "identity_confidence"))
                .addValue("identityRevision", integer(t, "identity_revision"))
                .addValue("splitFrom", text(t, "split_from"))
                .addValue("aliases", textArray(t, "aliases"))
                .addValue("frameAgeMs", integer(t, "frame_age_ms"))
                .addValue("x", position.get(0))
                .addValue("y", position.get(1))
                .addValue("positionXySigma", arrayLiteral(vector(t, "position_xy_sigma", 2, false)))
                .addValue("pelvisXyz", arrayLiteral(vector(t, "pelvis_xyz", 3, false)))
                .addValue("groundAnchorXyz", arrayLiteral(vector(t, "ground_anchor_xyz", 3, false)))
                .addValue("groundAnchorSigma", arrayLiteral(vector(t, "ground_anchor_sigma", 3, false)))
                .addValue("surfaceSource", text(t, "surface_source"))
                .addValue("supportSurfaceId", text(t, "support_surface_id"))
                .addValue("supportSurfaceValidity", text(t, "support_surface_validity"))
                .addValue("contactValidity", text(t, "contact_validity"))
                .addValue("zValidity", text(t, "z_validity"))
                .addValue("headZ", number(t, "head_z"))
                .addValue("headZSigma", number(t, "head_z_sigma"))
                .addValue("headZValidity", text(t, "head_z_validity"))
                .addValue("velocity", arrayLiteral(vector(t, "velocity", 3, false)))
                .addValue("cameras", textArray(t, "cameras"))
                .addValue("calibrationVersion", text(payload, "calibration_version"))
                .addValue("sceneVersion", text(payload, "scene_version"))
                .addValue("configVersion", text(payload, "config_version"))
                .addValue("inputProvenance", json(payload, "input_provenance"))
                .addValue("receivedAt", utc(receivedAt));
    }

    /** 골격이 없는 트랙이면 null */
    private static SqlParameterSource skeletonRow(OffsetDateTime observationAt, JsonNode t) {
        JsonNode s = t.get("skeleton");
        if (absent(s)) {
            return null;
        }
        if (!s.isObject()) {
            throw new InvalidI1Message("skeleton 이 객체가 아니다");
        }
        return new MapSqlParameterSource()
                .addValue("observationAt", observationAt)
                .addValue("worldId", requiredText(t, "world_id"))
                .addValue("jointLayout", text(s, "joint_layout"))
                .addValue("joints", joints(s))
                .addValue("visible", booleanArray(s, "visible"))
                .addValue("validity", textArray(s, "validity"))
                .addValue("jointSource", textArray(s, "joint_source"));
    }
}
