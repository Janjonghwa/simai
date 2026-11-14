package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.zone.SetupZonesUnconfirmedException;
import com.ssafy.simai.global.exception.GlobalExceptionHandler;
import java.util.Map;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import com.fasterxml.jackson.annotation.JsonProperty;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 세팅 REST. 조회는 인증된 열람자, 높이 조회·생성·재시도는 ADMIN(명세 12.6).
 * 맵 보강 촬영 업로드·다시 굽기(S15P31A101-607)도 ADMIN 이다(휴대폰 화면도 같은 관리자 로그인을 쓴다).
 */
@RestController
@RequestMapping("/api/setup-sessions")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupController {
    private final SetupService setups;
    private final SetupSupplementService supplements;
    private final SetupCompletionService completions;

    SetupController(SetupService setups, SetupSupplementService supplements, SetupCompletionService completions) {
        this.setups = setups;
        this.supplements = supplements;
        this.completions = completions;
    }

    /** mode: NEW(기본, 새 모델) | REUSE(이전 모델 재사용: 현재 관제 맵을 그대로 쓰고 보정만 다시 한다, S15P31A101-614) */
    record Create(@JsonProperty("work_unit_id") Long workUnitId,
                  @JsonProperty("camera_heights_m") Map<String, Double> cameraHeightsM,
                  @JsonProperty("mode") String mode) { }
    record Retry(@JsonProperty("camera_heights_m") Map<String, Double> cameraHeightsM) { }
    record Resume(@JsonProperty("camera_heights_m") Map<String, Double> cameraHeightsM) { }

    @GetMapping("/camera-height-defaults")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode defaults() { return setups.defaults(); }

    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode create(@RequestBody Create request, @AuthenticationPrincipal Jwt jwt) {
        return setups.create(request.workUnitId(), request.cameraHeightsM(), Long.parseLong(jwt.getSubject()), null,
                request.mode());
    }

    @GetMapping("/{id}")
    ObjectNode get(@PathVariable UUID id) { return setups.get(id); }

    @GetMapping("/{id}/zone-drafts")
    ObjectNode zoneDrafts(@PathVariable UUID id) { return setups.getZoneDrafts(id); }

    @PostMapping("/{id}/cancel")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode cancel(@PathVariable UUID id) { return setups.cancel(id); }

    @PostMapping("/{id}/retry")
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode retry(@PathVariable UUID id, @RequestBody(required = false) Retry request,
            @AuthenticationPrincipal Jwt jwt) {
        return setups.retry(id, request == null ? null : request.cameraHeightsM(), Long.parseLong(jwt.getSubject()));
    }

    /**
     * 카메라 높이 빠른 확인에 답한다(S15P31A101-690). 워커가 확인을 기다리는 세션(paused_reason HEIGHT_CHECK)만 받는다.
     * 보낸 높이가 지금과 같으면 「실측이 맞음」, 다르면 고친 높이로 다시 계산해 이어 간다
     */
    @PostMapping("/{id}/resume")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode resume(@PathVariable UUID id, @RequestBody Resume request, @AuthenticationPrincipal Jwt jwt) {
        return setups.resume(id, request.cameraHeightsM(), Long.parseLong(jwt.getSubject()));
    }

    /**
     * 세팅 완료(C17, S15P31A101-615): 세션 구역 목록 적용 → 완료 기록 → (새 모델) 이 세션 최종 맵 켜기 → 구성 묶음 한 번 발행.
     * 응답은 세션 조회와 같은 모양에 applied_zones{created,updated,kept,archived}(zone_id 목록)를 더한다
     */
    @PostMapping("/{id}/complete")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode complete(@PathVariable UUID id, @AuthenticationPrincipal Jwt jwt) {
        SetupCompletionService.Completed done = completions.complete(id, Long.parseLong(jwt.getSubject()));
        ObjectNode out = setups.get(id);
        ObjectNode applied = out.putObject("applied_zones");
        done.zones().created().forEach(applied.putArray("created")::add);
        done.zones().updated().forEach(applied.putArray("updated")::add);
        done.zones().kept().forEach(applied.putArray("kept")::add);
        done.zones().archived().forEach(applied.putArray("archived")::add);
        return out;
    }

    /** 세팅 완료의 확인 필요 구역 거부(409 SETUP_ZONES_UNCONFIRMED, S15P31A101-629): 남은 개수를 문구와 unconfirmed_count 로 싣는다 */
    @ExceptionHandler(SetupZonesUnconfirmedException.class)
    ProblemDetail zonesUnconfirmed(SetupZonesUnconfirmedException e) {
        ProblemDetail problem = GlobalExceptionHandler.problem(e.getErrorCode());
        problem.setDetail("확인이 필요한 구역 " + e.remaining() + "개가 남아 있습니다. 구역마다 확인·수정·삭제한 뒤 세팅을 완료하세요");
        problem.setProperty("unconfirmed_count", e.remaining());
        return problem;
    }

    /** 맵 보강 촬영 파일 하나의 서명 PUT 주소(S15P31A101-607). 응답은 파일 기록 + upload{method,url,headers,expires_at_ms} */
    @PostMapping("/{id}/supplement-media")
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode issueSupplement(@PathVariable UUID id, @RequestBody SetupSupplementService.IssueRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return supplements.issue(id, request, Long.parseLong(jwt.getSubject()));
    }

    /** 올리기를 마쳤다는 알림. 저장소 HEAD 로 확인한다 */
    @PostMapping("/{id}/supplement-media/{mediaId}/complete")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode completeSupplement(@PathVariable UUID id, @PathVariable String mediaId) {
        return supplements.complete(id, mediaId);
    }

    @DeleteMapping("/{id}/supplement-media/{mediaId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    @PreAuthorize("hasRole('ADMIN')")
    void removeSupplement(@PathVariable UUID id, @PathVariable String mediaId) {
        supplements.remove(id, mediaId);
    }

    /** 올린 보강 촬영 파일로 맵만 다시 굽는다(site_setup SUPPLEMENT_REBAKE). 응답은 세션 조회와 같은 모양 */
    @PostMapping("/{id}/supplement-rebake")
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode rebake(@PathVariable UUID id, @AuthenticationPrincipal Jwt jwt) {
        supplements.requestRebake(id, Long.parseLong(jwt.getSubject()));
        return setups.get(id);
    }

    @PostMapping("/{id}/supplement-rebake/cancel")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode cancelRebake(@PathVariable UUID id) {
        supplements.cancelRebake(id);
        return setups.get(id);
    }
}
