package com.ssafy.simai.domain.cctv;

import com.ssafy.simai.global.security.TokenHashes;
import java.time.Clock;
import java.time.Instant;
import java.util.Comparator;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Service;

/**
 * 시청 입장권(S15P31A101-400). 브라우저는 WebSocket 에 Authorization 헤더를 싣지 못해, 관리자 JWT 로 받은 일회용 값을
 * 주소의 ticket 으로 싣는다. 유효 시간(초기 60 s) 안에 한 번만 쓴다. 메모리에만 둔다(서버를 다시 띄우면 다시 받는다).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CctvTicketService {

    /** 쓰지 않고 남은 입장권이 이보다 많으면 먼저 끝나는 것부터 정리한다(메모리 상한). */
    static final int MAX_OUTSTANDING = 256;

    private final Clock clock;
    private final CctvRemoteProperties properties;
    private final Map<String, Ticket> tickets = new ConcurrentHashMap<>();

    record Ticket(long userId, Instant expiresAt) {
    }

    /** 발급 결과. value 는 응답에만 싣고 로그에 남기지 않는다. */
    public record Issued(String value, Instant expiresAt) {
    }

    public CctvTicketService(Clock clock, CctvRemoteProperties properties) {
        this.clock = clock;
        this.properties = properties;
    }

    public Issued issue(long userId) {
        Instant now = clock.instant();
        purge(now);
        String value = TokenHashes.newToken();
        Instant expiresAt = now.plus(properties.ticketTtl());
        tickets.put(value, new Ticket(userId, expiresAt));
        return new Issued(value, expiresAt);
    }

    /** 맞고 지나지 않은 입장권이면 사용자 ID. 맞든 지났든 그 값은 다시 쓸 수 없다. */
    public Optional<Long> consume(String value) {
        if (value == null || value.isBlank()) {
            return Optional.empty();
        }
        Ticket ticket = tickets.remove(value);
        if (ticket == null || !clock.instant().isBefore(ticket.expiresAt())) {
            return Optional.empty();
        }
        return Optional.of(ticket.userId());
    }

    int outstanding() {
        return tickets.size();
    }

    private void purge(Instant now) {
        tickets.values().removeIf(t -> !now.isBefore(t.expiresAt()));
        int over = tickets.size() - MAX_OUTSTANDING + 1;
        if (over > 0) {
            tickets.entrySet().stream()
                    .sorted(Comparator.comparing((Map.Entry<String, Ticket> e) -> e.getValue().expiresAt()))
                    .limit(over)
                    .map(Map.Entry::getKey)
                    .toList()
                    .forEach(tickets::remove);
        }
    }
}
