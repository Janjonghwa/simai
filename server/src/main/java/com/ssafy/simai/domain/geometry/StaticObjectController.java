package com.ssafy.simai.domain.geometry;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.layout.LayoutZoneService;
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
 * 작업면·정적 객체 조회·등록·새 버전·보관(명세 12.6, 7.5). 쓰기는 관리자, 조회는 열람 이상.
 * 등록한 작업면은 구성 묶음 operational_geometry 로 Tier 0 에 전달된다(3.2).
 */
@RestController
@RequestMapping("/api/static-objects")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class StaticObjectController {

    private final StaticObjectRepository objects;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final Clock clock;
    private final LayoutZoneService layoutZones;

    StaticObjectController(StaticObjectRepository objects, SiteLookup sites, TransactionTemplate transaction, Clock clock, LayoutZoneService layoutZones) {
        this.objects = objects;
        this.sites = sites;
        this.transaction = transaction;
        this.clock = clock;
        this.layoutZones = layoutZones;
    }

    @GetMapping
    List<ObjectNode> list() {
        return objects.findCurrent(sites.siteId());
    }

    @GetMapping("/{objectId}/versions")
    List<ObjectNode> versions(@PathVariable String objectId) {
        return objects.findVersions(objectId);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode create(@RequestBody StaticObjectRequest request, @AuthenticationPrincipal Jwt jwt) {
        String siteId = sites.siteId();
        return transaction.execute(s -> objects.create(siteId, request, Long.parseLong(jwt.getSubject()), now()));
    }

    @PutMapping("/{objectId}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode newVersion(@PathVariable String objectId, @RequestBody StaticObjectRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return transaction.execute(s -> { layoutZones.lock(); layoutZones.denyObject(objectId); return objects.newVersion(objectId, request, Long.parseLong(jwt.getSubject()), now()); });
    }

    /** 보관: 지금 버전을 닫아 목록·다음 묶음에서 뺀다. 이미 적용된 묶음은 다음 발행까지 그대로 쓰인다 */
    @DeleteMapping("/{objectId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    @PreAuthorize("hasRole('ADMIN')")
    void archive(@PathVariable String objectId) {
        transaction.executeWithoutResult(s -> { layoutZones.lock(); layoutZones.denyObject(objectId); objects.archive(objectId, now()); });
    }

    private OffsetDateTime now() {
        return OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
    }
}
