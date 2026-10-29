package com.ssafy.simai.domain.zone;

import com.ssafy.simai.domain.config.SiteLookup;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구역 조회·생성·새 버전·보관(명세 12.6, 8.2). 쓰기는 관리자, 조회는 열람 이상.
 * 바꾼 구역은 구성 묶음(POST /api/config-bundles)으로 발행해야 Tier 0 판정에 쓰인다(3.2).
 */
@RestController
@RequestMapping("/api/zones")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ZoneController {

    private final ZoneRepository zones;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final Clock clock;

    ZoneController(ZoneRepository zones, SiteLookup sites, TransactionTemplate transaction, Clock clock) {
        this.zones = zones;
        this.sites = sites;
        this.transaction = transaction;
        this.clock = clock;
    }

    /** 지금 유효한 구역(구역마다 최신 버전) */
    @GetMapping
    List<ObjectNode> list() {
        return zones.findCurrent(sites.siteId());
    }

    /** 버전 이력(오래된 것부터). 리플레이는 그 시각 유효 버전을 쓴다 */
    @GetMapping("/{zoneId}/versions")
    List<ObjectNode> versions(@PathVariable String zoneId) {
        return zones.findVersions(zoneId);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode create(@RequestBody ZoneRequest request, @AuthenticationPrincipal Jwt jwt) {
        String siteId = sites.siteId();
        return transaction.execute(s -> zones.create(siteId, request, Long.parseLong(jwt.getSubject()), now()));
    }

    @PutMapping("/{zoneId}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode newVersion(@PathVariable String zoneId, @RequestBody ZoneRequest request, @AuthenticationPrincipal Jwt jwt) {
        return transaction.execute(s -> zones.newVersion(zoneId, request, Long.parseLong(jwt.getSubject()), now()));
    }

    /** 보관: 지금 버전을 닫아 목록·다음 묶음에서 뺀다. 이미 적용된 묶음은 다음 발행까지 그대로 쓰인다 */
    @DeleteMapping("/{zoneId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    @PreAuthorize("hasRole('ADMIN')")
    void archive(@PathVariable String zoneId) {
        transaction.executeWithoutResult(s -> zones.archive(zoneId, now()));
    }

    private OffsetDateTime now() {
        return OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
    }
}
