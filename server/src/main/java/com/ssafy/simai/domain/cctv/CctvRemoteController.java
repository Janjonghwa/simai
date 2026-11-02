package com.ssafy.simai.domain.cctv;

import com.fasterxml.jackson.annotation.JsonProperty;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

/**
 * CCTV 원격 시청 REST(관리자만, S15P31A101-400).
 * <ul>
 * <li>POST /api/cctv/remote/tickets: 시청 WebSocket(/ws/cctv/view?ticket=…) 일회용 입장권(초기 60 s, 한 번만).</li>
 * <li>GET /api/cctv/remote/status: 이번 달(UTC) 원격 송출 바이트·상한·차단 여부·시청자 수. 운영 상태 표시이지 위험 알림이 아니다.</li>
 * </ul>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class CctvRemoteController {

    private final CctvTicketService tickets;
    private final CctvRemoteHub hub;

    CctvRemoteController(CctvTicketService tickets, CctvRemoteHub hub) {
        this.tickets = tickets;
        this.hub = hub;
    }

    record TicketResponse(@JsonProperty("ticket") String ticket, @JsonProperty("expires_at_ms") long expiresAtMs,
            @JsonProperty("path") String path) {
    }

    record StatusResponse(@JsonProperty("month") String month,
            @JsonProperty("monthly_sent_bytes") long monthlySentBytes,
            @JsonProperty("monthly_quota_bytes") long monthlyQuotaBytes,
            @JsonProperty("blocked") boolean blocked,
            @JsonProperty("viewers") int viewers,
            @JsonProperty("max_viewers") int maxViewers,
            @JsonProperty("source_connected") boolean sourceConnected,
            @JsonProperty("demand") boolean demand) {
    }

    @PostMapping(CctvPaths.TICKETS_API)
    @PreAuthorize("hasRole('ADMIN')")
    @ResponseStatus(HttpStatus.CREATED)
    TicketResponse ticket(@AuthenticationPrincipal Jwt jwt) {
        CctvTicketService.Issued issued = tickets.issue(Long.parseLong(jwt.getSubject()));
        return new TicketResponse(issued.value(), issued.expiresAt().toEpochMilli(), CctvPaths.VIEW_WS);
    }

    @GetMapping(CctvPaths.STATUS_API)
    @PreAuthorize("hasRole('ADMIN')")
    StatusResponse status() {
        CctvRemoteHub.Status s = hub.status();
        return new StatusResponse(s.month(), s.monthlySentBytes(), s.monthlyQuotaBytes(), s.blocked(), s.viewers(),
                s.maxViewers(), s.sourceConnected(), s.demand());
    }
}
