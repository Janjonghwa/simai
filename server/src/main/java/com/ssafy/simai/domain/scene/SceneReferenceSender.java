package com.ssafy.simai.domain.scene;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import java.io.IOException;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import tools.jackson.databind.ObjectMapper;

/** 자산·규칙과 독립적인 현재 Scene 참조. 재연결/활성화/되돌림은 다음 1초 주기에 반영한다. */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SceneReferenceSender {
    private static final Logger log = LoggerFactory.getLogger(SceneReferenceSender.class);
    private final I1ConnectionRegistry connections;
    private final NamedParameterJdbcTemplate jdbc;
    private final SiteLookup sites;
    private final ObjectMapper mapper;
    private final String fallback;

    SceneReferenceSender(I1ConnectionRegistry connections, NamedParameterJdbcTemplate jdbc, SiteLookup sites,
            ObjectMapper mapper, @Value("${simai.scene.default-version:scene-demo}") String version,
            @Value("${simai.scene.default-gltf-key:}") String key) {
        this.connections = connections;
        this.jdbc = jdbc;
        this.sites = sites;
        this.mapper = mapper;
        this.fallback = key.isBlank() ? null : version;
    }

    @Scheduled(fixedDelay = 1000)
    void send() {
        if (connections.all().isEmpty()) return;
        String site = sites.siteId();
        var versions = jdbc.queryForList("SELECT scene_version FROM (" + SceneService.CURRENT_COMMITTED + ") s WHERE site_id = :site LIMIT 1",
                Map.of("site", site), String.class);
        var payload = mapper.createObjectNode();
        payload.put("site_id", site);
        payload.put("scene_version", versions.isEmpty() ? fallback : versions.getFirst());
        for (var connection : connections.all()) {
            if (!connection.isOpen() || connection.gatewayId() == null) continue;
            try {
                connection.send("scene_reference", "1.0.0", payload.deepCopy());
            } catch (IOException | RuntimeException e) {
                log.warn("Scene 참조 전송 실패(다음 주기에 재전송): {}", e.getClass().getSimpleName());
            }
        }
    }
}
