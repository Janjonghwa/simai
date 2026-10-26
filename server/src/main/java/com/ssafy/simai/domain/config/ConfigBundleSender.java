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
 * 구성 묶음 알림(I1 config_bundle, EC2 → Tier 0)을 보낸다. 발행하면 열린 연결에 바로 보내고, 그 뒤로는 10초마다
 * 가장 최근 묶음에 아직 config_ack 를 보내지 않은 Tier 0 에 다시 보낸다(RESEND_AFTER_MS 지난 것). 끊겨 있던 Tier 0 는 다시 붙으면
 * 받는다. 최근 묶음만 보낸다. 지난 묶음은 새 묶음이 대신한다(새 묶음의 previous_config_version 이 그것을 가리킨다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ConfigBundleSender {

    static final String CONFIG_BUNDLE_SCHEMA_VERSION = "1.2.0";
    static final long RESEND_AFTER_MS = 30_000;
    private static final String SENT_KEY = "config_bundle.sent";
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
                if (connection.gatewayId() == null || !connection.isOpen() || acknowledged(version, connection.gatewayId())) {
                    continue;
                }
                Map<String, Long> sent = connection.state(SENT_KEY, ConcurrentHashMap::new);
                Long last = sent.get(version);
                if (last == null || clock.millis() - last >= RESEND_AFTER_MS) {
                    send(connection, version, notice);
                }
            }
        }));
    }

    private boolean acknowledged(String version, String gatewayId) {
        Integer count = jdbc.queryForObject("SELECT count(*) FROM config_ack WHERE config_version = :v AND gateway_id = :g",
                new MapSqlParameterSource("v", version).addValue("g", gatewayId), Integer.class);
        return count != null && count > 0;
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
