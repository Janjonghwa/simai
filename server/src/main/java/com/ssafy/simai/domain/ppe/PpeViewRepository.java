package com.ssafy.simai.domain.ppe;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
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
 * 화면용 PPE 스냅샷(I2 ppe_view 1.0.0 payload, S15P31A101-531, 명세 12.4·13.3)을 ppe_current_state 에서 만든다.
 * <ul>
 * <li>Tier 0(source_id)마다 한 장. 촬영 시각이 스냅샷 시각 − {@link #WINDOW} 이후인 사람만, 최신 촬영 순으로 최대 {@link #MAX_PEOPLE}명.</li>
 * <li>장비마다 state 는 스냅샷 시각(EC2 시계)에 만료를 반영한 값이다. 착용·미착용인데 expires_at 이 지났으면 UNKNOWN·EVIDENCE_EXPIRED.
 *     reported_state 는 PPE 프로세스가 보낸 값 그대로다. EC2 는 상태를 지어내지 않는다(UNKNOWN 을 착용·미착용으로 바꾸지 않는다).</li>
 * <li>근거 프레임 키·local ID·모델 판은 싣지 않는다(계약이 닫힌 객체).</li>
 * </ul>
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class PpeViewRepository {

    /** 스냅샷에 넣는 촬영 시각 범위. 그보다 오래된 사람은 이미 world_state 에서 빠졌을 가능성이 커 싣지 않는다 */
    static final Duration WINDOW = Duration.ofSeconds(60);
    /** 계약 people maxItems */
    static final int MAX_PEOPLE = 64;
    static final String EXPIRED_REASON = "EVIDENCE_EXPIRED";

    private static final String SELECT = """
            SELECT world_id, identity_revision, observation_at, updated_at,
                   helmet_state, helmet_reason, helmet_last_evidence_at, helmet_expires_at,
                   vest_state, vest_reason, vest_last_evidence_at, vest_expires_at
            FROM ppe_current_state
            WHERE source_id = :sourceId AND observation_at >= :since
            ORDER BY observation_at DESC, world_id
            LIMIT :limit
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final PpeReceipts receipts;

    PpeViewRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, Clock clock, PpeReceipts receipts) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.clock = clock;
        this.receipts = receipts;
    }

    /** 한 Tier 0 의 지금 스냅샷(ppe_view payload) */
    public ObjectNode snapshot(String sourceId) {
        Instant now = clock.instant();
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("sourceId", sourceId)
                .addValue("since", OffsetDateTime.ofInstant(now.minus(WINDOW), ZoneOffset.UTC))
                .addValue("limit", MAX_PEOPLE);
        ObjectNode payload = mapper.createObjectNode();
        payload.put("tier0_source_id", sourceId);
        payload.put("snapshot_at_ms", now.toEpochMilli());
        // ppe_view 1.1.0(S15P31A101-634): 이 Tier 0 의 ppe_state 를 마지막으로 받은 시각. 받은 적 없으면 null
        receipts.lastReceived(sourceId).ifPresentOrElse(
                at -> payload.put("last_received_at_ms", at.toEpochMilli()), () -> payload.putNull("last_received_at_ms"));
        ArrayNode people = payload.putArray("people");
        jdbc.query(SELECT, params, rs -> {
            people.add(person(rs, now));
        });
        return payload;
    }

    /**
     * 촬영 시각이 범위 안인 상태를 가진 Tier 0, 그리고 이 EC2 가 시작된 뒤 ppe_state 를 받은 적이 있는 Tier 0 마다 하나씩(처음 상태 REST).
     * 사람이 없어도 받은 적이 있으면 넣어 화면이 마지막 수신 시각을 볼 수 있게 한다(S15P31A101-634). 없으면 빈 목록
     */
    public List<ObjectNode> snapshots() {
        Instant now = clock.instant();
        java.util.TreeSet<String> sources = new java.util.TreeSet<>(jdbc.queryForList("""
                SELECT DISTINCT source_id FROM ppe_current_state WHERE observation_at >= :since
                """, new MapSqlParameterSource("since", OffsetDateTime.ofInstant(now.minus(WINDOW), ZoneOffset.UTC)),
                String.class));
        sources.addAll(receipts.sources());
        List<ObjectNode> result = new ArrayList<>(sources.size());
        for (String source : sources) {
            result.add(snapshot(source));
        }
        return result;
    }

    private ObjectNode person(ResultSet rs, Instant now) throws SQLException {
        ObjectNode person = mapper.createObjectNode();
        person.put("world_id", rs.getString("world_id"));
        person.put("identity_revision", rs.getInt("identity_revision"));
        person.put("capture_time_ms", millis(rs, "observation_at"));
        person.put("received_at_ms", millis(rs, "updated_at"));
        person.set("helmet", item(rs, "helmet", now));
        person.set("vest", item(rs, "vest", now));
        return person;
    }

    private ObjectNode item(ResultSet rs, String name, Instant now) throws SQLException {
        String reported = rs.getString(name + "_state");
        String reason = rs.getString(name + "_reason");
        Long lastEvidence = nullableMillis(rs, name + "_last_evidence_at");
        Long expires = nullableMillis(rs, name + "_expires_at");
        boolean decided = "WORN".equals(reported) || "NOT_WORN".equals(reported);
        boolean expired = decided && (expires == null || expires < now.toEpochMilli());
        ObjectNode item = mapper.createObjectNode();
        item.put("state", expired ? "UNKNOWN" : reported);
        item.put("reported_state", reported);
        item.put("reason", expired ? EXPIRED_REASON : reason);
        if (lastEvidence == null) {
            item.putNull("last_evidence_at_ms");
        } else {
            item.put("last_evidence_at_ms", lastEvidence);
        }
        if (expires == null) {
            item.putNull("expires_at_ms");
        } else {
            item.put("expires_at_ms", expires);
        }
        return item;
    }

    private static long millis(ResultSet rs, String column) throws SQLException {
        return rs.getObject(column, OffsetDateTime.class).toInstant().toEpochMilli();
    }

    private static Long nullableMillis(ResultSet rs, String column) throws SQLException {
        OffsetDateTime value = rs.getObject(column, OffsetDateTime.class);
        return value == null ? null : value.toInstant().toEpochMilli();
    }
}
