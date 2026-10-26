package com.ssafy.simai.domain.tier0.endpoint;

import static com.ssafy.simai.domain.tier0.i1.I1Json.*;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * Tier 0 엔드포인트 등록 이력(V1 tier0_endpoint + V8 봉투 칸). 추가만 한다(덮어쓰지 않음, AGENTS 6). 같은 msg_id 는 한 행이다.
 * 현재 엔드포인트는 registered_at 이 가장 늦은 행이고, 같으면 늦게 받은 행(received_at, endpoint_id)이다(계약 설명).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class Tier0EndpointRepository {

    static final String MSG_TYPE = "tier0_endpoint";

    private static final String INSERT = """
            INSERT INTO tier0_endpoint (
                gateway_id, public_ip, ports, tls_public_key_sha256, registered_at, pod_id, datacenter, received_at,
                msg_id, schema_version, sent_at)
            VALUES (
                :gatewayId, CAST(:publicIp AS inet), CAST(:ports AS jsonb), :fingerprint, :registeredAt, :podId, :datacenter,
                :receivedAt, :msgId, :schemaVersion, :sentAt)
            ON CONFLICT (msg_id) DO NOTHING
            """;

    private static final String CURRENT = """
            SELECT schema_version, msg_id::text AS msg_id, sent_at, gateway_id, host(public_ip) AS public_ip, ports::text AS ports,
                tls_public_key_sha256, registered_at, pod_id, datacenter
            FROM tier0_endpoint
            WHERE msg_id IS NOT NULL
            ORDER BY registered_at DESC, received_at DESC, endpoint_id DESC
            LIMIT 1
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    Tier0EndpointRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 계약 검사를 마친 메시지. @return 새로 썼으면 true, 같은 msg_id 가 이미 있으면 false */
    boolean insert(I1Envelope envelope, Instant receivedAt) {
        JsonNode p = envelope.payload();
        int rows = jdbc.update(INSERT, new MapSqlParameterSource()
                .addValue("gatewayId", requiredText(p, "gateway_id"))
                .addValue("publicIp", requiredText(p, "public_ip"))
                .addValue("ports", json(p, "ports"))
                .addValue("fingerprint", requiredText(p, "tls_public_key_sha256"))
                .addValue("registeredAt", requiredTime(p, "registered_at"))
                .addValue("podId", text(p, "pod_id"))
                .addValue("datacenter", text(p, "datacenter"))
                .addValue("receivedAt", utc(receivedAt))
                .addValue("msgId", envelope.msgId())
                .addValue("schemaVersion", envelope.schemaVersion())
                .addValue("sentAt", utc(envelope.sentAt())));
        return rows > 0;
    }

    /** 현재 엔드포인트를 Tier 0 가 보낸 봉투 모양으로 되살린다. 없으면 비어 있다 */
    Optional<ObjectNode> findCurrent() {
        return jdbc.query(CURRENT, (rs, i) -> envelope(rs)).stream().findFirst();
    }

    private ObjectNode envelope(ResultSet rs) throws SQLException {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("gateway_id", rs.getString("gateway_id"));
        payload.put("public_ip", rs.getString("public_ip"));
        payload.set("ports", mapper.readTree(rs.getString("ports")));
        payload.put("tls_public_key_sha256", rs.getString("tls_public_key_sha256"));
        payload.put("registered_at", millis(rs, "registered_at"));
        // 선택 필드는 받은 적이 없으면 싣지 않는다(계약에 null 이 없다)
        if (rs.getString("pod_id") != null) {
            payload.put("pod_id", rs.getString("pod_id"));
        }
        if (rs.getString("datacenter") != null) {
            payload.put("datacenter", rs.getString("datacenter"));
        }
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("schema_version", rs.getString("schema_version"));
        envelope.put("msg_type", MSG_TYPE);
        envelope.put("msg_id", rs.getString("msg_id"));
        envelope.put("sent_at", millis(rs, "sent_at"));
        // 저장할 때 gateway_id 가 source_id 와 같은지 봤다(Tier0EndpointIngestor)
        envelope.put("source_id", rs.getString("gateway_id"));
        envelope.set("payload", payload);
        return envelope;
    }

    private static long millis(ResultSet rs, String column) throws SQLException {
        return rs.getObject(column, OffsetDateTime.class).toInstant().toEpochMilli();
    }
}
