package com.ssafy.simai.domain.event;

import com.fasterxml.jackson.annotation.JsonAlias;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.util.List;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 명세 12.6 /api/events(반장·관리자). 확인 본문 {event_sequence?, channel?(CONTROL_ROOM 기본·FOREMAN_PWA)}.
 * 항목은 계약 risk_event payload 와 같은 snake_case 이고 acks[] 를 싣는다(관제 앱 types.ts).
 * 같은 사람의 여러 규칙을 한 묶음으로 보이는 일은 화면이 한다(7장·13장 "화면에서", 관제 앱 groupIncidents). 서버는 이벤트를 하나씩 준다.
 * 실시간 갱신은 /topic/events(S15P31A101-119).
 */
@RestController
@RequestMapping("/api/events")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventController {

    static final int DEFAULT_SIZE = 20;
    static final int MAX_SIZE = 100;

    private final EventQueryRepository events;
    private final EventAckService acks;
    private final EventEscalationService escalations;
    private final IncidentSummaryService summaries;
    private final EventCloseService closes;
    private final EventFrameService frames;

    EventController(EventQueryRepository events, EventAckService acks, EventEscalationService escalations,
            IncidentSummaryService summaries, EventCloseService closes, EventFrameService frames) {
        this.events = events;
        this.acks = acks;
        this.escalations = escalations;
        this.summaries = summaries;
        this.closes = closes;
        this.frames = frames;
    }

    /** Spring Page 와 같은 모양. 관제 앱은 content 만 읽는다 */
    public record EventPage(List<ObjectNode> content, long totalElements, int number, int size) {
    }

    /** 확인 본문. event_sequence 는 화면이 본 sequence(없으면 EC2 가 마지막 sequence 로 채운다) */
    public record AckRequest(@JsonProperty("event_sequence") Integer eventSequence, @JsonProperty("channel") String channel) {
    }

    /** VLM 상향 의견 승인·거절 본문(명세 5.4·12.6, S15P31A101-276). assessmentId·decision 필수, note 선택 */
    public record EscalationRequest(
            @JsonProperty("assessmentId") @JsonAlias({"assessment_id", "assessmentJobId", "assessment_job_id", "jobId", "job_id"}) JsonNode assessmentId,
            @JsonProperty("decision") String decision,
            @JsonProperty("note") String note) {
    }

    /** 운영자 종료 본문(명세 12.6, S15P31A101-221). reason 1~500자, idempotency_key 1~128자 둘 다 필수 */
    public record CloseRequest(
            @JsonProperty("reason") String reason,
            @JsonProperty("idempotency_key") @JsonAlias("idempotencyKey") String idempotencyKey) {
    }

    @GetMapping
    @PreAuthorize("hasAnyRole('FOREMAN', 'ADMIN')")
    EventPage list(@RequestParam(defaultValue = "0") int page, @RequestParam(defaultValue = "" + DEFAULT_SIZE) int size) {
        int safePage = Math.max(page, 0);
        int safeSize = Math.clamp(size, 1, MAX_SIZE);
        return new EventPage(events.findPage(safePage, safeSize), events.count(), safePage, safeSize);
    }

    /** 이벤트 상세(명세 12.6): 목록과 같은 칸에 관련 구역과 전이 이력을 더한다. LIVE 만 */
    @GetMapping("/{eventId}")
    @PreAuthorize("hasAnyRole('FOREMAN', 'ADMIN')")
    ObjectNode detail(@PathVariable UUID eventId) {
        return events.detail(eventId).orElseThrow(() -> new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND));
    }

    /**
     * 이벤트 프레임(사진) 목록(S15P31A101-753). 요청마다 짧게 사는 GET 서명 주소를 낸다(만료되면 다시 부른다). 관리자 전용 —
     * 얼굴이 보일 수 있는 원본이라 클립(GET /api/events/{id}/clips)과 같은 권한이다
     */
    @GetMapping("/{eventId}/frames")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode frames(@PathVariable UUID eventId) {
        return frames.frames(eventId);
    }

    @PostMapping("/{eventId}/ack")
    @PreAuthorize("hasAnyRole('FOREMAN', 'ADMIN')")
    ObjectNode ack(@PathVariable UUID eventId, @RequestBody(required = false) AckRequest request, @AuthenticationPrincipal Jwt jwt) {
        Integer seen = request == null ? null : request.eventSequence();
        String channel = request == null ? null : request.channel();
        Role role = Role.valueOf(jwt.getClaimAsString(AccessTokenIssuer.ROLE_CLAIM));
        return acks.acknowledge(eventId, seen, channel, Long.parseLong(jwt.getSubject()), role);
    }

    /** VLM 상향 의견 승인·거절(명세 5.4·12.6, S15P31A101-276). 관리자 전용이며 승인만 I1 event_escalate 로 보낸다 */
    @PostMapping("/{eventId}/escalation")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode escalate(@PathVariable UUID eventId, @RequestBody(required = false) EscalationRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        String assessmentId = request == null || request.assessmentId() == null || request.assessmentId().isNull()
                ? null
                : request.assessmentId().asString();
        String decision = request == null ? null : request.decision();
        String note = request == null ? null : request.note();
        return escalations.decide(eventId, assessmentId, decision, note, Long.parseLong(jwt.getSubject()));
    }

    /**
     * 구역 대상 이벤트 운영자 종료(명세 6.4·12.6, S15P31A101-221). 관리자 전용. Tier 0 결과가 오면 200, 아직이면 202(result null 또는 일시 거부 JOURNAL_UNAVAILABLE).
     * 거부(사람 대상·HIGH·CRITICAL·조건 성립)도 Tier 0 결과라 200 에 result REJECTED·reject_reason 으로 싣는다
     */
    @PostMapping("/{eventId}/close")
    @PreAuthorize("hasRole('ADMIN')")
    ResponseEntity<ObjectNode> close(@PathVariable UUID eventId, @RequestBody(required = false) CloseRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        EventCloseService.CloseResponse res = closes.close(eventId,
                request == null ? null : request.reason(),
                request == null ? null : request.idempotencyKey(),
                Long.parseLong(jwt.getSubject()));
        return ResponseEntity.status(res.decided() ? HttpStatus.OK : HttpStatus.ACCEPTED).body(res.body());
    }

    /** 사고 구간 VLM 사후 요약 요청(명세 5.6·12.6, S15P31A101-369). 관리자 전용, 본문 없음 */
    @PostMapping("/{eventId}/summary")
    @PreAuthorize("hasRole('ADMIN')")
    ResponseEntity<ObjectNode> summary(@PathVariable UUID eventId, @AuthenticationPrincipal Jwt jwt) {
        Long userId = jwt == null || jwt.getSubject() == null ? null : Long.parseLong(jwt.getSubject());
        IncidentSummaryService.SummaryResponse res = summaries.requestSummary(eventId, userId);
        return ResponseEntity.status(res.created() ? HttpStatus.ACCEPTED : HttpStatus.OK).body(res.body());
    }
}
