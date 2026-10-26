package com.ssafy.simai.domain.config;

import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구성 묶음 발행·적용 상태(명세 12.6: 발행은 관리자, 조회는 열람)와 Tier 0 가 내려받는 묶음 파일.
 * 파일 경로는 JWT 없이 열린다(Tier 0 는 헤더를 싣지 않는다). 대신 주소의 서명(sig)이 맞아야 하고, 틀리면 없는 것과 같이 404 다.
 */
@RestController
@RequestMapping("/api/config-bundles")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ConfigBundleController {

    static final int RECENT = 20;

    private final ConfigBundleService bundles;
    private final ConfigBundleViews views;
    private final BundleUrlSigner signer;
    private final NamedParameterJdbcTemplate jdbc;

    ConfigBundleController(ConfigBundleService bundles, ConfigBundleViews views, BundleUrlSigner signer,
            NamedParameterJdbcTemplate jdbc) {
        this.bundles = bundles;
        this.views = views;
        this.signer = signer;
        this.jdbc = jdbc;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode publish(@RequestBody(required = false) PublishRequest request, @AuthenticationPrincipal Jwt jwt) {
        return bundles.publish(request == null ? null : request.ruleProfile(), Long.parseLong(jwt.getSubject()));
    }

    /** 최근 묶음(새것부터) */
    @GetMapping
    List<ObjectNode> recent() {
        return views.recent(RECENT);
    }

    @GetMapping("/{version}")
    ObjectNode find(@PathVariable String version) {
        return views.find(version);
    }

    @GetMapping("/{version}/files/{name}")
    ResponseEntity<byte[]> file(@PathVariable String version, @PathVariable String name,
            @RequestParam(name = "sig", required = false) String sig) {
        if (!ConfigFiles.known(name) || !signer.verify(version, name, sig)) {
            return ResponseEntity.notFound().build();
        }
        List<byte[]> content = jdbc.query("SELECT content FROM config_bundle_file WHERE config_version = :v AND name = :name",
                new MapSqlParameterSource("v", version).addValue("name", name), (rs, i) -> rs.getBytes("content"));
        if (content.isEmpty()) {
            return ResponseEntity.notFound().build();
        }
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON).body(content.getFirst());
    }
}
