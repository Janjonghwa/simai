package com.ssafy.simai.domain.calibration;

import com.ssafy.simai.domain.config.SiteLookup;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 보정 문서 올리기(관리자). 구성 묶음이 가장 최근에 올린 보정 문서를 싣는다(CalibrationService 설명).
 * 보정 상태(열람)와 설치 안내(관리자)는 Tier 0 가 알린 보정 기준으로 읽는다(CalibrationViews).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class CalibrationController {

    private final CalibrationService calibrations;
    private final CalibrationViews views;
    private final SiteLookup sites;

    CalibrationController(CalibrationService calibrations, CalibrationViews views, SiteLookup sites) {
        this.calibrations = calibrations;
        this.views = views;
        this.sites = sites;
    }

    @PostMapping("/api/calibrations")
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode upload(@RequestBody CalibrationUploadRequest request, @AuthenticationPrincipal Jwt jwt) {
        return calibrations.upload(sites.siteId(), request, Long.parseLong(jwt.getSubject()));
    }

    @GetMapping("/api/calibrations/current")
    ObjectNode current() {
        return views.current();
    }

    @GetMapping("/api/cameras/coverage-guides")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode coverageGuides() {
        return views.coverageGuides();
    }
}
