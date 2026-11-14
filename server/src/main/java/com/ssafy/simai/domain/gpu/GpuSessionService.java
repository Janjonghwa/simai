package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.machine.MachineTokenService.IssuedToken;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.security.Tier0SessionTokens;
import com.ssafy.simai.global.security.TokenHashes;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.function.Predicate;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 세션 등록·진행 보고·종료(S15P31A101-699, 설계 docs/design/S15P31A101-699-gpu-session-register.md, 명세 3.12·14).
 * <ul>
 * <li>같은 시점 GPU 세션은 하나다. 새 등록은 열린 세션을 모두 끝낸다(SUPERSEDED). 등록·종료는 advisory 잠금 하나로 줄 세운다.</li>
 * <li>등록마다 부팅 비밀값에 묶인 워커의 토큰을 새로 발급하고(폐기돼 있어도 되살림) Tier 0 토큰을 새로 만든다. 원문은 결과로 한 번만.</li>
 * <li>세션이 끝나면(종료 보고·밀림·무응답·부팅 비밀값 폐기·관리자 정지) Tier 0 토큰은 더 받지 않고 워커 토큰은 폐기한다
 * (그 워커를 쓰는 다른 열린 세션이 없을 때). 끝은 한 번만 기록한다(부분 UNIQUE 인덱스).</li>
 * <li>기록은 추가만 한다: gpu_session 은 등록 때 한 번, 진행·종료는 gpu_session_event 에 덧붙인다(AGENTS 6).</li>
 * </ul>
 * 관리 명령(웹 서버 없는 프로세스)에서도 쓰므로 웹 전용 빈(I1 연결 목록)에 기대지 않는다. I1 연결 끊기는 GpuSessionSweeper 가 한다.
 */
@Service
public class GpuSessionService implements Tier0SessionTokens {

    /** 끝난 까닭(gpu_session_event.end_reason) */
    public enum EndReason {
        /** 인스턴스가 보낸 종료 보고(stage ENDED) */
        END_REPORTED,
        /** 새 등록에 밀림 */
        SUPERSEDED,
        /** 마지막 보고 뒤 stale-after 동안 보고도 이 세션 I1 연결도 없음 */
        STALE,
        /** 그 세션을 연 부팅 비밀값을 폐기함 */
        BOOT_SECRET_REVOKED,
        /** 관리자가 끔(S15P31A101-702 화면 끄기·자동 정지가 부른다) */
        ADMIN_STOPPED
    }

    /** 등록 결과. 토큰 두 개는 원문이라 응답 본문 말고는 어디에도 남기지 않는다 */
    public record Registered(UUID sessionId, Instant registeredAt, List<UUID> superseded, String workerId, String workerToken,
            String gatewayId, String tier0Token) {

        @Override
        public String toString() {
            // 실수로 로그에 찍어도 토큰이 나가지 않게
            return "Registered[sessionId=" + sessionId + ", workerId=" + workerId + ", gatewayId=" + gatewayId + "]";
        }
    }

    /** 부팅 비밀값 폐기 결과 */
    public record BootRevoked(boolean revoked, List<UUID> endedSessions) {
    }

    private static final String LOCK = "SELECT pg_advisory_xact_lock(hashtext('simai.gpu_session'))";
    /** s 가 아직 끝나지 않은 세션 */
    private static final String OPEN =
            "NOT EXISTS (SELECT 1 FROM gpu_session_event x WHERE x.session_id = s.session_id AND x.kind = 'ENDED')";
    private static final String LAST_RECEIVED =
            "(SELECT max(l.received_at) FROM gpu_session_event l WHERE l.session_id = s.session_id)";

    private final NamedParameterJdbcTemplate jdbc;
    private final MachineTokenService tokens;
    private final TransactionTemplate transaction;
    private final ApplicationEventPublisher events;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final String gatewayId;
    private final Duration reportInterval;
    private final Duration staleAfter;

    public GpuSessionService(NamedParameterJdbcTemplate jdbc, MachineTokenService tokens, TransactionTemplate transaction,
            ApplicationEventPublisher events, ObjectMapper mapper, Clock clock,
            @Value("${simai.gpu-session.gateway-id:tier0-lobby-s9}") String gatewayId,
            @Value("${simai.gpu-session.report-interval:60s}") Duration reportInterval,
            @Value("${simai.gpu-session.stale-after:10m}") Duration staleAfter) {
        this.jdbc = jdbc;
        this.tokens = tokens;
        this.transaction = transaction;
        this.events = events;
        this.mapper = mapper;
        this.clock = clock;
        this.gatewayId = gatewayId;
        this.reportInterval = reportInterval;
        this.staleAfter = staleAfter;
    }

    public Duration reportInterval() {
        return reportInterval;
    }

    public Duration staleAfter() {
        return staleAfter;
    }

    /**
     * 등록. 계약 검사를 마친 gpu_session_register payload 를 받는다. 열린 세션을 끝내고 새 세션의 토큰을 발급한다.
     * 부팅 비밀값이 그 사이 폐기됐으면 GPU_BOOT_REVOKED.
     */
    public Registered register(String bootId, UUID msgId, JsonNode p) {
        return transaction.execute(status -> {
            jdbc.queryForList(LOCK, Map.of());
            Instant now = now();
            List<String> workers = jdbc.queryForList(
                    "SELECT worker_id FROM gpu_boot_secret WHERE boot_id = :boot AND revoked_at IS NULL",
                    new MapSqlParameterSource("boot", bootId), String.class);
            if (workers.isEmpty()) {
                throw new BusinessException(ErrorCode.Gpu.GPU_BOOT_REVOKED);
            }
            String workerId = workers.getFirst();
            // 먼저 끝내고(그 워커 폐기) 나서 새로 발급한다. 순서가 바뀌면 같은 워커의 새 토큰을 폐기한다
            List<UUID> superseded = openSessions(null);
            for (UUID old : superseded) {
                endLocked(old, EndReason.SUPERSEDED, now, null, null, null);
            }
            IssuedToken worker = tokens.reissue(MachineKind.WORKER, workerId);
            String tier0Token = TokenHashes.newToken();
            UUID sessionId = UUID.randomUUID();
            jdbc.update("""
                    INSERT INTO gpu_session (session_id, boot_id, provider, instance_id, gpu_type, gpu_count, region, public_host, ports,
                        image_digest, price_per_hour_usd, tls_public_key_sha256, worker_id, gateway_id, tier0_token_hash,
                        registered_at, register_msg_id)
                    VALUES (:id, :boot, :provider, :instance, :gpuType, :gpuCount, :region, :host, CAST(:ports AS jsonb),
                        :digest, :price, :tls, :worker, :gateway, :tier0Hash, :now, :msgId)
                    """, new MapSqlParameterSource()
                    .addValue("id", sessionId).addValue("boot", bootId)
                    .addValue("provider", p.get("provider").stringValue())
                    .addValue("instance", p.get("instance_id").stringValue())
                    .addValue("gpuType", p.get("gpu_type").stringValue())
                    .addValue("gpuCount", present(p, "gpu_count") ? p.get("gpu_count").intValue() : null)
                    .addValue("region", text(p, "region"))
                    .addValue("host", p.get("public_host").stringValue())
                    .addValue("ports", mapper.writeValueAsString(p.get("ports")))
                    .addValue("digest", text(p, "image_digest"))
                    .addValue("price", present(p, "price_per_hour_usd") ? p.get("price_per_hour_usd").decimalValue() : null)
                    .addValue("tls", text(p, "tls_public_key_sha256"))
                    .addValue("worker", workerId).addValue("gateway", gatewayId)
                    .addValue("tier0Hash", TokenHashes.sha256Hex(tier0Token))
                    .addValue("now", utc(now)).addValue("msgId", msgId));
            jdbc.update("""
                    INSERT INTO gpu_session_event (session_id, kind, received_at) VALUES (:id, 'REGISTERED', :now)
                    """, new MapSqlParameterSource("id", sessionId).addValue("now", utc(now)));
            return new Registered(sessionId, now, superseded, workerId, worker.token(), gatewayId, tier0Token);
        });
    }

    /**
     * 진행·종료 보고. 그 부팅 비밀값이 연 세션이 아니면 GPU_SESSION_NOT_FOUND, 끝난 세션의 진행 보고는 GPU_SESSION_ENDED.
     * 종료 보고(ENDED)는 이미 끝난 세션에도 성공이다(같은 결과).
     */
    public void report(String bootId, UUID sessionId, JsonNode p) {
        transaction.executeWithoutResult(status -> {
            List<Boolean> rows = jdbc.query("SELECT " + OPEN + " AS open FROM gpu_session s WHERE s.session_id = :id AND s.boot_id = :boot",
                    new MapSqlParameterSource("id", sessionId).addValue("boot", bootId), (rs, i) -> rs.getBoolean("open"));
            if (rows.isEmpty()) {
                throw new BusinessException(ErrorCode.Gpu.GPU_SESSION_NOT_FOUND);
            }
            boolean open = rows.getFirst();
            String stage = p.get("stage").stringValue();
            Instant now = now();
            Instant reportedAt = Instant.ofEpochMilli(p.get("reported_at").longValue());
            if (stage.equals("ENDED")) {
                if (open) {
                    jdbc.queryForList(LOCK, Map.of());
                    endLocked(sessionId, EndReason.END_REPORTED, now, text(p, "reason_code"), text(p, "detail"), reportedAt);
                }
                return;
            }
            if (!open) {
                throw new BusinessException(ErrorCode.Gpu.GPU_SESSION_ENDED);
            }
            jdbc.update("""
                    INSERT INTO gpu_session_event (session_id, kind, stage, reason_code, detail, progress, reported_at, received_at)
                    VALUES (:id, 'PROGRESS', :stage, :reason, :detail, :progress, :reportedAt, :now)
                    """, new MapSqlParameterSource("id", sessionId).addValue("stage", stage)
                    .addValue("reason", text(p, "reason_code")).addValue("detail", text(p, "detail"))
                    .addValue("progress", present(p, "progress") ? p.get("progress").doubleValue() : null)
                    .addValue("reportedAt", utc(reportedAt)).addValue("now", utc(now)));
        });
    }

    /** 관리자 정지 등 서버 쪽에서 끝낸다. @return 이번에 끝냈으면 true(이미 끝났으면 false) */
    public boolean end(UUID sessionId, EndReason reason) {
        return Boolean.TRUE.equals(transaction.execute(status -> {
            jdbc.queryForList(LOCK, Map.of());
            return endLocked(sessionId, reason, now(), null, null, null);
        }));
    }

    /** 부팅 비밀값 폐기(관리 명령). 그 비밀값이 연 열린 세션도 끝낸다(BOOT_SECRET_REVOKED) */
    public BootRevoked revokeBoot(String bootId) {
        return transaction.execute(status -> {
            jdbc.queryForList(LOCK, Map.of());
            boolean revoked = tokens.revoke(MachineKind.GPU_BOOT, bootId);
            Instant now = now();
            List<UUID> ended = new ArrayList<>();
            for (UUID id : openSessions(bootId)) {
                if (endLocked(id, EndReason.BOOT_SECRET_REVOKED, now, null, null, null)) {
                    ended.add(id);
                }
            }
            return new BootRevoked(revoked, ended);
        });
    }

    /**
     * 무응답 세션을 끝낸다. 마지막 보고(EC2 수신 시각)가 stale-after 보다 오래됐고 이 세션 Tier 0 토큰의 I1 연결도 없는 세션.
     * I1 이 붙어 있으면 관제가 돌고 있으므로 보고기가 죽었어도 끝내지 않는다(관제를 끊지 않는다).
     * @param connected 지금 열린 I1 연결의 GPU 세션
     */
    public List<UUID> endStale(Set<UUID> connected) {
        Instant now = now();
        Instant cutoff = now.minus(staleAfter);
        List<UUID> candidates = jdbc.query("SELECT s.session_id::text AS id FROM gpu_session s WHERE " + OPEN
                        + " AND " + LAST_RECEIVED + " < :cutoff",
                new MapSqlParameterSource("cutoff", utc(cutoff)), (rs, i) -> UUID.fromString(rs.getString("id")));
        List<UUID> ended = new ArrayList<>();
        for (UUID id : candidates) {
            if (connected.contains(id)) {
                continue;
            }
            Boolean done = transaction.execute(status -> {
                jdbc.queryForList(LOCK, Map.of());
                return endLocked(id, EndReason.STALE, now, null, null, cutoff);
            });
            if (Boolean.TRUE.equals(done)) {
                ended.add(id);
            }
        }
        return ended;
    }

    /** 이 중 끝난 세션 */
    public Set<UUID> endedAmong(Collection<UUID> ids) {
        if (ids.isEmpty()) {
            return Set.of();
        }
        return new HashSet<>(jdbc.query(
                "SELECT session_id::text AS id FROM gpu_session_event WHERE kind = 'ENDED' AND session_id IN (:ids)",
                new MapSqlParameterSource("ids", List.copyOf(ids)), (rs, i) -> UUID.fromString(rs.getString("id"))));
    }

    @Override
    public Optional<UUID> activeSessionForTier0Token(String tokenSha256Hex) {
        return jdbc.query("SELECT s.session_id::text AS id FROM gpu_session s WHERE s.tier0_token_hash = :hash AND " + OPEN,
                new MapSqlParameterSource("hash", tokenSha256Hex), (rs, i) -> UUID.fromString(rs.getString("id")))
                .stream().findFirst();
    }

    /**
     * 관리 화면용 가장 최근 세션(열려 있든 끝났든). 등록이 한 번도 없으면 비어 있다.
     * @param tier0Connected 그 세션 Tier 0 토큰으로 연 I1 연결이 지금 열려 있는지
     */
    public Optional<ObjectNode> latest(Predicate<UUID> tier0Connected) {
        Instant now = now();
        return jdbc.query("""
                SELECT s.session_id::text AS id, s.provider, s.instance_id, s.gpu_type, s.gpu_count, s.region, s.public_host,
                    s.ports::text AS ports, s.image_digest, s.price_per_hour_usd, s.tls_public_key_sha256, s.worker_id, s.gateway_id,
                    s.registered_at, ended.received_at AS ended_at, ended.end_reason, ended.reason_code AS end_reason_code,
                    prog.stage, prog.reason_code, prog.detail, prog.progress, prog.received_at AS stage_at,
                    """ + LAST_RECEIVED + """
                     AS last_report_at
                FROM gpu_session s
                LEFT JOIN gpu_session_event ended ON ended.session_id = s.session_id AND ended.kind = 'ENDED'
                LEFT JOIN LATERAL (
                    SELECT p.stage, p.reason_code, p.detail, p.progress, p.received_at FROM gpu_session_event p
                    WHERE p.session_id = s.session_id AND p.kind = 'PROGRESS' ORDER BY p.event_id DESC LIMIT 1) prog ON true
                ORDER BY s.registered_at DESC, s.session_id
                LIMIT 1
                """, Map.of(), (rs, i) -> view(rs, now, tier0Connected)).stream().findFirst();
    }

    private ObjectNode view(ResultSet rs, Instant now, Predicate<UUID> tier0Connected) throws SQLException {
        UUID id = UUID.fromString(rs.getString("id"));
        Instant registeredAt = instant(rs, "registered_at");
        Instant endedAt = instant(rs, "ended_at");
        ObjectNode node = mapper.createObjectNode();
        node.put("session_id", id.toString());
        node.put("state", endedAt == null ? "ACTIVE" : "ENDED");
        String stage = rs.getString("stage");
        node.put("stage", stage == null ? "REGISTERED" : stage);
        node.put("stage_reason_code", rs.getString("reason_code"));
        node.put("stage_detail", rs.getString("detail"));
        Object progress = rs.getObject("progress");
        if (progress == null) {
            node.putNull("stage_progress");
        } else {
            node.put("stage_progress", ((Number) progress).doubleValue());
        }
        putMillis(node, "stage_at_ms", instant(rs, "stage_at"));
        node.put("provider", rs.getString("provider"));
        node.put("instance_id", rs.getString("instance_id"));
        node.put("gpu_type", rs.getString("gpu_type"));
        Object gpuCount = rs.getObject("gpu_count");
        if (gpuCount == null) {
            node.putNull("gpu_count");
        } else {
            node.put("gpu_count", ((Number) gpuCount).intValue());
        }
        node.put("region", rs.getString("region"));
        node.put("public_host", rs.getString("public_host"));
        node.set("ports", mapper.readTree(rs.getString("ports")));
        node.put("image_digest", rs.getString("image_digest"));
        node.put("price_per_hour_usd", rs.getBigDecimal("price_per_hour_usd"));
        node.put("tls_public_key_sha256", rs.getString("tls_public_key_sha256"));
        node.put("worker_id", rs.getString("worker_id"));
        node.put("gateway_id", rs.getString("gateway_id"));
        putMillis(node, "registered_at_ms", registeredAt);
        putMillis(node, "last_report_at_ms", instant(rs, "last_report_at"));
        putMillis(node, "ended_at_ms", endedAt);
        node.put("end_reason", rs.getString("end_reason"));
        node.put("end_reason_code", rs.getString("end_reason_code"));
        node.put("elapsed_ms", Math.max(0, (endedAt == null ? now : endedAt).toEpochMilli() - registeredAt.toEpochMilli()));
        node.put("tier0_link", endedAt == null && tier0Connected.test(id) ? "CONNECTED" : "DISCONNECTED");
        node.put("report_interval_ms", reportInterval.toMillis());
        node.put("stale_after_ms", staleAfter.toMillis());
        return node;
    }

    /**
     * 끝을 한 번 기록하고, 이번에 기록했으면 워커 토큰을 폐기하고 끝남을 알린다. advisory 잠금을 잡은 트랜잭션 안에서 부른다.
     * @param staleCutoff 있으면 마지막 수신이 이보다 이를 때만 끝낸다(판정 뒤 들어온 보고를 무시하지 않게)
     */
    private boolean endLocked(UUID sessionId, EndReason reason, Instant now, String reasonCode, String detail, Instant reportedAt,
            Instant staleCutoff) {
        MapSqlParameterSource params = new MapSqlParameterSource("id", sessionId).addValue("reason", reason.name())
                .addValue("code", reasonCode).addValue("detail", detail)
                .addValue("reportedAt", reportedAt == null ? null : utc(reportedAt)).addValue("now", utc(now));
        String guard = "";
        if (staleCutoff != null) {
            guard = " WHERE (SELECT max(l.received_at) FROM gpu_session_event l WHERE l.session_id = :id) < :cutoff";
            params.addValue("cutoff", utc(staleCutoff));
        }
        int rows = jdbc.update("""
                INSERT INTO gpu_session_event (session_id, kind, end_reason, reason_code, detail, reported_at, received_at)
                SELECT :id, 'ENDED', :reason, :code, :detail, CAST(:reportedAt AS timestamptz), :now""" + guard + """

                ON CONFLICT (session_id) WHERE kind = 'ENDED' DO NOTHING
                """, params);
        if (rows == 0) {
            return false;
        }
        // 같은 워커를 쓰는 다른 열린 세션이 있으면 폐기하지 않는다(새 등록이 같은 워커에 막 발급한 토큰을 지키지 않게 순서로도 막는다)
        jdbc.update("""
                UPDATE worker w SET revoked_at = :now
                WHERE w.revoked_at IS NULL
                  AND w.worker_id = (SELECT g.worker_id FROM gpu_session g WHERE g.session_id = :id)
                  AND NOT EXISTS (SELECT 1 FROM gpu_session s WHERE s.worker_id = w.worker_id AND s.session_id <> :id""" // 텍스트 블록은 줄 끝 공백을 지운다
                + " AND " + OPEN + ")", new MapSqlParameterSource("id", sessionId).addValue("now", utc(now)));
        events.publishEvent(new GpuSessionEnded(sessionId, reason));
        return true;
    }

    private boolean endLocked(UUID sessionId, EndReason reason, Instant now, String reasonCode, String detail, Instant reportedAt) {
        return endLocked(sessionId, reason, now, reasonCode, detail, reportedAt, null);
    }

    /** 열린 세션(bootId 가 있으면 그 비밀값이 연 것만) */
    private List<UUID> openSessions(String bootId) {
        MapSqlParameterSource params = new MapSqlParameterSource();
        String filter = "";
        if (bootId != null) {
            filter = " AND s.boot_id = :boot";
            params.addValue("boot", bootId);
        }
        return jdbc.query("SELECT s.session_id::text AS id FROM gpu_session s WHERE " + OPEN + filter + " ORDER BY s.registered_at",
                params, (rs, i) -> UUID.fromString(rs.getString("id")));
    }

    private static boolean present(JsonNode p, String field) {
        JsonNode node = p.get(field);
        return node != null && !node.isNull();
    }

    private static String text(JsonNode p, String field) {
        return present(p, field) ? p.get(field).stringValue() : null;
    }

    private static Instant instant(ResultSet rs, String column) throws SQLException {
        OffsetDateTime value = rs.getObject(column, OffsetDateTime.class);
        return value == null ? null : value.toInstant();
    }

    private static void putMillis(ObjectNode node, String field, Instant value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value.toEpochMilli());
        }
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    /** 계약 시각은 UTC ms 라 DB 에도 ms 로 맞춘다 */
    private Instant now() {
        return clock.instant().truncatedTo(ChronoUnit.MILLIS);
    }
}
