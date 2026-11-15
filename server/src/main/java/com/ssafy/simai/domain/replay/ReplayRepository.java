package com.ssafy.simai.domain.replay;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import java.math.BigDecimal;
import java.sql.Array;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 리플레이 질의(docs/design/db-verification.md 5장 Q1~Q3). 기록을 다시 읽어 관제 앱이 실시간으로 받는 것과 같은 봉투 메시지로 만든다.
 * <ul>
 *   <li>world_state: world_track_state·skeleton_3d 의 한 tick(같은 observation_at)을 계약 world_state 하나로 되돌린다.
 *       값은 저장한 그대로이고, 트랙이 없던 tick 은 행이 없어 돌려주지 않는다(만들지 않는다).
 *   <li>risk_event: risk_event_update 의 전이 하나를 그 전이 직후의 이벤트 상태(I2 risk_event, /topic/events 와 같은 칸)로 만든다.
 *       raised_at·resume_count 처럼 앞 전이에 기대는 칸은 그 이벤트의 앞 전이들로 다시 센다(RiskEventRepository 와 같은 규칙).
 * </ul>
 * 봉투 source_id 는 ec2(관제 앱이 받는 I2 와 같음), msg_id 는 Tier 0 가 붙인 원래 msg_id 라 같은 구간을 다시 읽어도 같다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ReplayRepository {

    /** I2 risk_event 판(Tier0Relay 와 같다. escalations 2.1.0 은 아직 싣지 않는다) */
    static final String RISK_EVENT_VERSION = "2.0.0";

    private static final String TRACK_COLUMNS = """
            w.observation_at, w.world_id, w.msg_id, w.t_last_valid, w.observation_state, w.track_lifecycle,
            w.identity_confidence, w.identity_revision, w.split_from, w.aliases, w.frame_age_ms,
            ST_X(w.position) AS x, ST_Y(w.position) AS y, w.position_xy_sigma, w.pelvis_xyz, w.ground_anchor_xyz,
            w.ground_anchor_sigma, w.surface_source, w.support_surface_id, w.support_surface_validity, w.contact_validity,
            w.z_validity, w.head_z, w.head_z_sigma, w.head_z_validity, w.velocity, w.cameras, w.calibration_version,
            w.scene_version, w.config_version, w.input_provenance, w.received_at,
            s.joint_layout, s.joints, s.visible, s.validity, s.joint_source
            """;

    /**
     * 구간 안의 tick 을 step_ms 칸으로 나눠 칸마다 가장 이른 tick 하나만 고른다(칸 시작 시각에 가장 가까운 기록).
     * 칸 안의 tick 수는 Tier 0 촬영 시각의 흔들림에 따라 달라 100 ms 칸에도 둘이 들 수 있으므로, 솎지 않는 요청(ALL_TRACKS)은 따로 둔다
     */
    private static final String RANGE_TRACKS = """
            WITH ticks AS (
                SELECT DISTINCT observation_at,
                       floor((extract(epoch FROM observation_at) * 1000 - :fromMs) / :stepMs) AS bucket
                FROM world_track_state
                WHERE observation_at >= :from AND observation_at <= :to
            ), picked AS (
                SELECT DISTINCT ON (bucket) observation_at
                FROM ticks
                ORDER BY bucket, observation_at
                LIMIT :limit
            )
            SELECT %s
            FROM world_track_state w
            JOIN picked p ON p.observation_at = w.observation_at
            LEFT JOIN skeleton_3d s ON s.observation_at = w.observation_at AND s.world_id = w.world_id
            ORDER BY w.observation_at, w.world_id
            """.formatted(TRACK_COLUMNS);

    /** 구간의 모든 tick(step_ms 최소값). 칸으로 솎지 않는다 */
    private static final String ALL_TRACKS = """
            WITH picked AS (
                SELECT DISTINCT observation_at FROM world_track_state
                WHERE observation_at >= :from AND observation_at <= :to
                ORDER BY observation_at
                LIMIT :limit
            )
            SELECT %s
            FROM world_track_state w
            JOIN picked p ON p.observation_at = w.observation_at
            LEFT JOIN skeleton_3d s ON s.observation_at = w.observation_at AND s.world_id = w.world_id
            ORDER BY w.observation_at, w.world_id
            """.formatted(TRACK_COLUMNS);

    /** 시각 t 직전 1초 안의 마지막 tick(Q1). 그 tick 의 트랙만 쓰고 그 전 tick 의 트랙을 끌어오지 않는다 */
    private static final String TRACKS_AT = """
            SELECT %s
            FROM world_track_state w
            LEFT JOIN skeleton_3d s ON s.observation_at = w.observation_at AND s.world_id = w.world_id
            WHERE w.observation_at = (
                SELECT max(observation_at) FROM world_track_state
                WHERE observation_at > :at - interval '1 second' AND observation_at <= :at)
            ORDER BY w.world_id
            """.formatted(TRACK_COLUMNS);

    /**
     * 전이마다 그 직후 이벤트 상태. 앞 전이에 기대는 칸은 같은 이벤트의 앞 전이들(event_sequence 순)로 센다.
     * ACKNOWLEDGED 는 생명주기를 바꾸지 않으므로(Q2) 상태 행으로 내지 않지만 창 안에는 둔다.
     * 창은 후보 이벤트(relevant)의 전이만 센다. 후보를 시각으로 좁혀 질의 비용이 쌓인 이력 전체가 아니라 그 구간에 걸린 이벤트 수를 따르게 한다
     */
    private static final String EVENT_STATES = """
            WITH relevant AS (
                %s
            ), states AS (
                SELECT u.event_id, u.event_sequence, u.msg_id, u.gateway_id, u.transition, u.occurred_at, u.observation_at,
                       u.rule_id, u.rule_version, u.config_version, u.calibration_version, u.scene_version, u.source,
                       u.severity, u.confidence, u.verification, u.needs_check, u.confidence_reasons, u.world_ids, u.zone_id,
                       u.metrics, u.related_event_id, u.received_at, u.delivery_mode,
                       min(u.occurred_at) FILTER (WHERE u.transition = 'RAISED') OVER w AS raised_at,
                       count(*) FILTER (WHERE u.resumed) OVER w AS resume_count
                FROM risk_event_update u
                JOIN relevant r ON r.event_id = u.event_id
                WINDOW w AS (PARTITION BY u.event_id ORDER BY u.event_sequence
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
            )
            """;

    /**
     * 시각 t 에 활성인 이벤트(Q2): t 이전 마지막 생명주기 전이가 RESOLVED 가 아닌 것.
     * 후보는 t 뒤에 전이가 있거나(그 뒤에 끝났을 수 있다) 지금도 끝나지 않았거나 t 뒤에 EC2 마감된(S15P31A101-722, Tier 0 전이 없이 닫힘)
     * 이벤트다. t 에 활성이었던 이벤트는 셋 중 하나다
     */
    private static final String ACTIVE_AT = EVENT_STATES.formatted("""
                SELECT event_id FROM risk_event_update WHERE observation_at > :at
                UNION
                SELECT event_id FROM risk_event WHERE source = 'LIVE' AND lifecycle <> 'RESOLVED'
                UNION
                SELECT event_id FROM risk_event_server_close WHERE closed_at > :at
            """) + """
            SELECT * FROM (
                SELECT DISTINCT ON (event_id) * FROM states
                WHERE observation_at <= :at AND transition <> 'ACKNOWLEDGED'
                ORDER BY event_id, event_sequence DESC
            ) last
            WHERE transition <> 'RESOLVED'
            ORDER BY observation_at, event_id
            """;

    /** 구간 (from, to] 의 생명주기 전이(Q3). from 시각의 상태는 ACTIVE_AT(:at = from) 으로 따로 준다 */
    private static final String TRANSITIONS = EVENT_STATES.formatted("""
                SELECT DISTINCT event_id FROM risk_event_update WHERE observation_at > :from AND observation_at <= :to
            """) + """
            SELECT * FROM states
            WHERE observation_at > :from AND observation_at <= :to AND transition <> 'ACKNOWLEDGED'
            ORDER BY observation_at, event_id, event_sequence
            LIMIT :limit
            """;

    /**
     * Tier 0 health(1 Hz)가 gapMs 넘게 끊긴 구간. 구간 처음·끝도 경계로 넣어 앞뒤로 빈 곳도 센다.
     * Tier 0 가 health 를 보내는 동안 world 행이 없으면 사람이 없던 것이고, health 도 없으면 기록이 없는 것이다(FR-28 "데이터 없음")
     */
    private static final String HEALTH_GAPS = """
            WITH points AS (
                SELECT CAST(:from AS timestamptz) AS t
                UNION
                SELECT reported_at FROM system_health WHERE reported_at > :from AND reported_at < :to
                UNION
                SELECT CAST(:to AS timestamptz)
            ), edges AS (
                SELECT lag(t) OVER (ORDER BY t) AS gap_from, t AS gap_to FROM points
            )
            SELECT gap_from, gap_to FROM edges
            WHERE gap_from IS NOT NULL AND gap_to - gap_from > make_interval(secs => :gapMs / 1000.0)
            ORDER BY gap_from
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    ReplayRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /**
     * 고른 tick 들의 world_state 봉투(시간순). 많아야 limit 개(넘는지는 부르는 쪽이 limit + 1 로 물어 확인한다).
     * every 면 칸으로 솎지 않고 모든 tick 을 준다
     */
    List<ObjectNode> worldStates(Instant from, Instant to, long stepMs, boolean every, int limit) {
        List<TrackRow> rows = jdbc.query(every ? ALL_TRACKS : RANGE_TRACKS, new MapSqlParameterSource()
                .addValue("from", utc(from)).addValue("to", utc(to))
                .addValue("fromMs", from.toEpochMilli()).addValue("stepMs", stepMs).addValue("limit", limit),
                (rs, i) -> trackRow(rs));
        return toWorldStates(rows);
    }

    /** 시각 at 의 world_state 봉투. 직전 1초 안에 기록이 없으면 null */
    ObjectNode worldStateAt(Instant at) {
        List<TrackRow> rows = jdbc.query(TRACKS_AT, new MapSqlParameterSource("at", utc(at)), (rs, i) -> trackRow(rs));
        List<ObjectNode> states = toWorldStates(rows);
        return states.isEmpty() ? null : states.getFirst();
    }

    List<ObjectNode> activeEventsAt(Instant at) {
        return jdbc.query(ACTIVE_AT, new MapSqlParameterSource("at", utc(at)), (rs, i) -> riskEvent(rs));
    }

    /** 구간의 전이. 많아야 limit 개(넘는지는 부르는 쪽이 limit + 1 로 물어 확인한다) */
    List<ObjectNode> transitions(Instant from, Instant to, int limit) {
        return jdbc.query(TRANSITIONS, new MapSqlParameterSource()
                .addValue("from", utc(from)).addValue("to", utc(to)).addValue("limit", limit), (rs, i) -> riskEvent(rs));
    }

    /** [{from, to}] 기록이 없던 구간(UTC ms) */
    ArrayNode gaps(Instant from, Instant to, long gapMs) {
        ArrayNode out = mapper.createArrayNode();
        jdbc.query(HEALTH_GAPS, new MapSqlParameterSource()
                .addValue("from", utc(from)).addValue("to", utc(to)).addValue("gapMs", gapMs), rs -> {
            ObjectNode gap = out.addObject();
            gap.put("from", rs.getTimestamp("gap_from").getTime());
            gap.put("to", rs.getTimestamp("gap_to").getTime());
        });
        return out;
    }

    /** 같은 observation_at 의 행들을 world_state 하나로 묶는다. 행은 observation_at 순으로 온다 */
    private List<ObjectNode> toWorldStates(List<TrackRow> rows) {
        List<ObjectNode> out = new ArrayList<>();
        int start = 0;
        while (start < rows.size()) {
            int end = start;
            while (end < rows.size() && rows.get(end).observationAt() == rows.get(start).observationAt()) {
                end++;
            }
            out.add(worldState(rows.subList(start, end)));
            start = end;
        }
        return out;
    }

    private ObjectNode worldState(List<TrackRow> tick) {
        // 같은 시각을 나중 메시지로 덮어써 행마다 msg_id 가 다를 수 있다. 가장 늦게 받은 행의 메시지를 그 tick 의 메시지로 본다
        TrackRow latest = tick.getFirst();
        for (TrackRow row : tick) {
            if (row.receivedAt() > latest.receivedAt()) {
                latest = row;
            }
        }
        ObjectNode payload = mapper.createObjectNode();
        payload.put("t_latest_global", latest.observationAt());
        payload.put("calibration_version", latest.calibrationVersion());
        payload.put("scene_version", latest.sceneVersion());
        if (latest.configVersion() != null) {
            payload.put("config_version", latest.configVersion());
        }
        if (latest.inputProvenance() != null) {
            payload.set("input_provenance", mapper.readTree(latest.inputProvenance()));
        }
        ArrayNode tracks = payload.putArray("tracks");
        boolean uncertainWithoutHead = false;
        for (TrackRow row : tick) {
            tracks.add(row.track());
            uncertainWithoutHead |= "UNCERTAIN".equals(row.track().path("head_z_validity").asString(null))
                    && row.track().get("head_z").isNull();
        }
        ObjectNode envelope = envelope(schemaVersion(uncertainWithoutHead), "world_state", latest.msgId(), latest.receivedAt());
        envelope.set("payload", payload);
        return envelope;
    }

    /**
     * 받은 판(schema_version)은 저장하지 않는다. 판에 따라 뜻이 갈리는 칸은 head_z 하나라 값으로 고른다(계약 world_state 설명).
     * UNCERTAIN 인데 head_z 가 null 이면 1.x 규칙으로 보낸 것이고(2.x 는 UNCERTAIN 이어도 값을 싣는다), 그 밖은 지금 판 2.1.0 이다.
     * 2.1.0 에만 있는 split_from 은 1.x 메시지에 없으므로 두 규칙이 부딪히지 않는다
     */
    private static String schemaVersion(boolean uncertainWithoutHead) {
        return uncertainWithoutHead ? "1.4.0" : "2.1.0";
    }

    private ObjectNode envelope(String schemaVersion, String msgType, String msgId, long sentAtMs) {
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("schema_version", schemaVersion);
        envelope.put("msg_type", msgType);
        envelope.put("msg_id", msgId);
        envelope.put("sent_at", sentAtMs);
        envelope.put("source_id", I1Connection.EC2_SOURCE_ID);
        return envelope;
    }

    /**
     * 계약 world_track 으로 되돌린다. 필수 칸은 값이 없으면 null 로 싣고, 선택 칸은 값이 없으면 뺀다
     * (저장할 때 빠진 칸과 null 을 나누지 않았다. 선택 칸은 빼는 쪽이 어느 판에서도 계약에 맞다)
     */
    private TrackRow trackRow(ResultSet rs) throws SQLException {
        ObjectNode t = mapper.createObjectNode();
        t.put("world_id", rs.getString("world_id"));
        t.put("t_last_valid", rs.getTimestamp("t_last_valid").getTime());
        t.put("observation_state", rs.getString("observation_state"));
        t.put("track_lifecycle", rs.getString("track_lifecycle"));
        t.put("identity_confidence", rs.getString("identity_confidence"));
        t.put("identity_revision", rs.getInt("identity_revision"));
        putOptional(t, "split_from", rs.getString("split_from"));
        putTexts(t, "aliases", rs.getArray("aliases"), false);
        int frameAge = rs.getInt("frame_age_ms");
        if (!rs.wasNull()) {
            t.put("frame_age_ms", frameAge);
        }
        ArrayNode position = t.putArray("position_xy");
        position.add(rs.getDouble("x"));
        position.add(rs.getDouble("y"));
        putReals(t, "position_xy_sigma", rs.getArray("position_xy_sigma"), true);
        putReals(t, "pelvis_xyz", rs.getArray("pelvis_xyz"), true);
        putReals(t, "ground_anchor_xyz", rs.getArray("ground_anchor_xyz"), true);
        putReals(t, "ground_anchor_sigma", rs.getArray("ground_anchor_sigma"), false);
        putOptional(t, "surface_source", rs.getString("surface_source"));
        t.put("support_surface_id", rs.getString("support_surface_id"));
        t.put("support_surface_validity", rs.getString("support_surface_validity"));
        t.put("contact_validity", rs.getString("contact_validity"));
        t.put("z_validity", rs.getString("z_validity"));
        putReal(t, "head_z", rs.getObject("head_z"), true);
        putReal(t, "head_z_sigma", rs.getObject("head_z_sigma"), false);
        putOptional(t, "head_z_validity", rs.getString("head_z_validity"));
        putReals(t, "velocity", rs.getArray("velocity"), false);
        putTexts(t, "cameras", rs.getArray("cameras"), true);
        if (rs.getString("joint_layout") != null) {
            ObjectNode skeleton = t.putObject("skeleton");
            skeleton.put("joint_layout", rs.getString("joint_layout"));
            ArrayNode joints = skeleton.putArray("joints");
            for (Object joint : (Object[]) rs.getArray("joints").getArray()) {
                Object[] xyz = (Object[]) joint;
                if (xyz[0] == null) {
                    // 저장할 때 null 관절을 {NULL,NULL,NULL} 로 넣었다(I1Json.joints)
                    joints.addNull();
                } else {
                    ArrayNode point = joints.addArray();
                    for (Object c : xyz) {
                        point.add(real(c));
                    }
                }
            }
            ArrayNode visible = skeleton.putArray("visible");
            for (Object v : (Object[]) rs.getArray("visible").getArray()) {
                visible.add((Boolean) v);
            }
            putTexts(skeleton, "validity", rs.getArray("validity"), true);
            putTexts(skeleton, "joint_source", rs.getArray("joint_source"), true);
        }
        return new TrackRow(rs.getTimestamp("observation_at").getTime(), rs.getString("msg_id"),
                rs.getTimestamp("received_at").getTime(), rs.getString("calibration_version"), rs.getString("scene_version"),
                rs.getString("config_version"), rs.getString("input_provenance"), t);
    }

    /** 전이 직후 이벤트 상태(I2 risk_event, EventQueryRepository 와 같은 칸 규칙: 선택 칸은 값이 없으면 뺀다) */
    private ObjectNode riskEvent(ResultSet rs) throws SQLException {
        ObjectNode e = mapper.createObjectNode();
        e.put("event_id", rs.getString("event_id"));
        e.put("source", rs.getString("source"));
        e.put("gateway_id", rs.getString("gateway_id"));
        e.put("rule_id", rs.getString("rule_id"));
        e.put("rule_version", rs.getInt("rule_version"));
        String transition = rs.getString("transition");
        e.put("lifecycle", transition);
        e.put("verification", rs.getString("verification"));
        e.put("severity", rs.getString("severity"));
        e.put("confidence", rs.getString("confidence"));
        e.put("needs_check", rs.getBoolean("needs_check"));
        putTexts(e, "confidence_reasons", rs.getArray("confidence_reasons"), false);
        putTexts(e, "world_ids", rs.getArray("world_ids"), true);
        e.put("zone_id", rs.getString("zone_id"));
        e.set("metrics", mapper.readTree(rs.getString("metrics")));
        // 전이 기록에는 VLM 설명·상향 의견이 없다(EC2 가 따로 붙이는 값). 필수·null 허용 칸이라 null 로 싣는다
        e.putNull("rationale");
        e.putNull("proposed_severity");
        e.put("resume_count", rs.getInt("resume_count"));
        e.put("event_sequence", rs.getInt("event_sequence"));
        e.put("config_version", rs.getString("config_version"));
        e.put("calibration_version", rs.getString("calibration_version"));
        e.put("scene_version", rs.getString("scene_version"));
        Timestamp raisedAt = rs.getTimestamp("raised_at");
        if (raisedAt != null) {
            e.put("raised_at", raisedAt.getTime());
        }
        long occurredAt = rs.getTimestamp("occurred_at").getTime();
        e.put("updated_at", occurredAt);
        if ("RESOLVED".equals(transition)) {
            e.put("resolved_at", occurredAt);
        }
        putOptional(e, "related_event_id", rs.getString("related_event_id"));
        // 재시작 복원 표시(계약 delivery, 3.2): 재동기화 중 받은 전이는 AUDIT_ONLY, 그 뒤에도 활성이라 늦게 알린 것은 DELAYED_ACTIVE
        if ("LIVE".equals(rs.getString("source"))) {
            // 계약은 delivery 를 LIVE 이벤트에만 둔다
            e.put("delivery", rs.getString("delivery_mode"));
        }
        ObjectNode envelope = envelope(RISK_EVENT_VERSION, "risk_event", rs.getString("msg_id"),
                rs.getTimestamp("received_at").getTime());
        envelope.set("payload", e);
        return envelope;
    }

    private static void putOptional(ObjectNode node, String field, String value) {
        if (value != null) {
            node.put(field, value);
        }
    }

    private static void putTexts(ObjectNode node, String field, Array array, boolean required) throws SQLException {
        if (array == null) {
            if (required) {
                node.putNull(field);
            }
            return;
        }
        ArrayNode out = node.putArray(field);
        for (Object value : (Object[]) array.getArray()) {
            out.add(value == null ? null : value.toString());
        }
    }

    private static void putReals(ObjectNode node, String field, Array array, boolean required) throws SQLException {
        if (array == null) {
            if (required) {
                node.putNull(field);
            }
            return;
        }
        ArrayNode out = node.putArray(field);
        for (Object value : (Object[]) array.getArray()) {
            if (value == null) {
                out.addNull();
            } else {
                out.add(real(value));
            }
        }
    }

    private static void putReal(ObjectNode node, String field, Object value, boolean required) {
        if (value == null) {
            if (required) {
                node.putNull(field);
            }
            return;
        }
        node.put(field, real(value));
    }

    /**
     * real(float4) 칸은 받은 소수(0.08)를 float 로 줄여 둔다. double 로 넓히면 0.07999999821 이 되므로 float 의 가장 짧은 표기로 되돌린다
     */
    private static double real(Object value) {
        if (value instanceof Float f) {
            return new BigDecimal(Float.toString(f)).doubleValue();
        }
        return ((Number) value).doubleValue();
    }

    private static OffsetDateTime utc(Instant instant) {
        return instant.atOffset(ZoneOffset.UTC);
    }

    private record TrackRow(long observationAt, String msgId, long receivedAt, String calibrationVersion,
            String sceneVersion, String configVersion, String inputProvenance, ObjectNode track) {
    }
}
