package com.ssafy.simai.domain.layout;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.transaction.support.TransactionTemplate;
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
 * 가상 레이아웃(명세 8.3, 12.6, docs/design/server-layouts.md). 목록·만들기·고치기는 관리자, 하나 조회는 열람 이상(12.6 표).
 * 계획 모드 단독 평가(/evaluate)는 확장(부록 C.4)이라 두지 않는다. M3 계산은 TBM 보고서(POST /api/reports/tbm) 안에서 한다.
 */
@RestController
@RequestMapping("/api/layouts")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class LayoutController {

    private final LayoutRepository layouts;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final Clock clock;

    LayoutController(LayoutRepository layouts, SiteLookup sites, TransactionTemplate transaction, Clock clock) {
        this.layouts = layouts;
        this.sites = sites;
        this.transaction = transaction;
        this.clock = clock;
    }

    @GetMapping
    @PreAuthorize("hasRole('ADMIN')")
    List<ObjectNode> list() {
        return layouts.findAll(sites.siteId());
    }

    @GetMapping("/{layoutId}")
    ObjectNode one(@PathVariable long layoutId) {
        return layouts.findOne(sites.siteId(), layoutId)
                .orElseThrow(() -> new BusinessException(ErrorCode.Layout.LAYOUT_NOT_FOUND));
    }

    /** 보고서가 계산한 revision 의 배치(이력). 열람 이상 */
    @GetMapping("/{layoutId}/revisions/{revision}")
    ObjectNode revision(@PathVariable long layoutId, @PathVariable int revision) {
        return layouts.findRevision(sites.siteId(), layoutId, revision)
                .orElseThrow(() -> new BusinessException(ErrorCode.Layout.LAYOUT_NOT_FOUND));
    }

    // 응답은 같은 트랜잭션 안에서 다시 읽는다(그 사이 다른 요청이 고친 값이 내 저장 결과로 보이지 않게)

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode create(@RequestBody(required = false) LayoutRequest request, @AuthenticationPrincipal Jwt jwt) {
        String siteId = sites.siteId();
        return transaction.execute(s -> {
            long id = layouts.create(siteId, request, Long.parseLong(jwt.getSubject()), clock.instant());
            return layouts.findOne(siteId, id).orElseThrow(() -> new BusinessException(ErrorCode.Layout.LAYOUT_NOT_FOUND));
        });
    }

    /** 전체 바꾸기. revision 을 하나 올리고 이력에 남긴다. 본문 revision(고치기 시작한 값)이 지금과 다르면 409 */
    @PutMapping("/{layoutId}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode replace(@PathVariable long layoutId, @RequestBody(required = false) LayoutRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        String siteId = sites.siteId();
        return transaction.execute(s -> {
            layouts.replace(siteId, layoutId, request, Long.parseLong(jwt.getSubject()), clock.instant());
            return layouts.findOne(siteId, layoutId).orElseThrow(() -> new BusinessException(ErrorCode.Layout.LAYOUT_NOT_FOUND));
        });
    }
}
