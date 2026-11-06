package com.ssafy.simai.domain.config;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import java.io.IOException;
import java.time.Clock;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구성 묶음 알림(I1 config_bundle, EC2 → Tier 0)을 보낸다. 발행하면 열린 연결에 바로 보낸다. 그 뒤로는 10초마다 가장 최근 묶음을
 * <ul>
 *   <li>그 연결로 아직 보낸 적이 없으면 보낸다. Tier 0 는 (재)연결 뒤 이 알림을 받기 전까지 UNVERIFIED 다
 *       (ai/src/simai_ai/config/sync.py '전제(서버)'). 이미 적용한 묶음을 다시 받아도 Tier 0 는 전환을 되풀이하지 않는다.
 *   <li>보낸 뒤 RESEND_AFTER_MS 가 지났는데 응답이 없거나, 마지막 응답이 내려받기 실패(DOWNLOAD_FAILED)면 다시 보낸다.
 *       Tier 0 는 거부한 묶음을 남기지 않고 다시 오면 다시 검증한다. 그 밖의 거부(해시·형식·참조)는 다시 보내도 같아 보내지 않는다.
 * </ul>
 * 최근 묶음만 보낸다. 지난 묶음은 새 묶음이 대신한다(새 묶음의 previous_config_version 이 그것을 가리킨다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ConfigBundleSender {

    static final String CONFIG_BUNDLE_SCHEMA_VERSION = "1.3.0";
    static final long RESEND_AFTER_MS = 30_000;
    private static final String SENT_KEY = "config_bundle.sent";
    private static final String RETRYABLE = "DOWNLOAD_FAILED";
    private static final Logger log = LoggerFactory.getLogger(ConfigBundleSender.class);

    private final I1ConnectionRegistry connections;
    private final ConfigBundleViews views;
    private final NamedParameterJdbcTemplate jdbc;
    private final Clock clock;

    public ConfigBundleSender(I1ConnectionRegistry connections, ConfigBundleViews views, NamedParameterJdbcTemplate jdbc,
            Clock clock) {
        this.connections = connections;
        this.views = views;
        this.jdbc = jdbc;
        this.clock = clock;
    }

    /** 방금 발행한 묶음을 열린 연결 모두에 보낸다. 못 보낸 곳은 주기 작업이 다시 보낸다 */
    public void sendToAll(String configVersion) {
        views.notice(configVersion).ifPresent(notice -> {
            for (I1Connection connection : connections.all()) {
                if (connection.gatewayId() != null && connection.isOpen()) {
                    send(connection, configVersion, notice);
                }
            }
        });
    }

    @Scheduled(fixedDelay = 10_000)
    void resendUnacknowledged() {
        views.latestVersion().ifPresent(version -> views.notice(version).ifPresent(notice -> {
            for (I1Connection connection : connections.all()) {
                if (connection.gatewayId() == null || !connection.isOpen()) {
                    continue;
                }
                Map<String, Long> sent = connection.state(SENT_KEY, ConcurrentHashMap::new);
                Long last = sent.get(version);
                if (last == null || (clock.millis() - last >= RESEND_AFTER_MS && needsResend(version, connection.gatewayId()))) {
                    send(connection, version, notice);
                }
            }
        }));
    }

    /** 응답이 없거나 마지막 응답이 내려받기 실패면 다시 보낸다 */
    private boolean needsResend(String version, String gatewayId) {
        return jdbc.query("""
                SELECT result, reject_reason FROM config_ack WHERE config_version = :v AND gateway_id = :g
                ORDER BY received_at DESC LIMIT 1
                """, new MapSqlParameterSource("v", version).addValue("g", gatewayId),
                (rs, i) -> "REJECTED".equals(rs.getString("result")) && RETRYABLE.equals(rs.getString("reject_reason")))
                .stream().findFirst().orElse(true);
    }

    private void send(I1Connection connection, String version, ObjectNode notice) {
        Map<String, Long> sent = connection.state(SENT_KEY, ConcurrentHashMap::new);
        sent.put(version, clock.millis());
        try {
            connection.send("config_bundle", CONFIG_BUNDLE_SCHEMA_VERSION, notice.deepCopy());
            log.info("config_bundle 보냄: gateway={} config_version={}", connection.gatewayId(), version);
        } catch (IOException | RuntimeException e) {
            log.warn("config_bundle 보내기 실패(다시 보냄): gateway={} config_version={} error={}", connection.gatewayId(), version,
                    e.getClass().getSimpleName());
        }
    }
}
