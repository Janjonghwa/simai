package com.ssafy.simai.domain.clip;

import com.ssafy.simai.domain.clip.ClipDeferral.Deferrable;
import com.ssafy.simai.domain.clip.ClipDeferral.Parked;
import com.ssafy.simai.domain.clip.ClipDeferral.WaitingForEarlier;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 보관해 둔 클립 메시지(ClipDeferral)를 다시 처리한다. 앞선 메시지가 왔으면 저장하고 지운다. 아직이면 점점 길게 기다렸다 다시 본다.
 * 계약 위반이 드러나면 지운다. give-up 이 지나도록 앞선 메시지가 오지 않으면 경고를 남기고 지운다(그 이벤트가 오지 않는다는 뜻).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ClipDeferredRetrier {

    private static final Logger log = LoggerFactory.getLogger(ClipDeferredRetrier.class);
    private static final int BATCH = 100;

    private final ClipDeferral deferral;
    private final Map<String, Deferrable> ingestors;
    private final Clock clock;
    private final Duration giveUp;

    ClipDeferredRetrier(ClipDeferral deferral, List<Deferrable> ingestors, Clock clock,
            @Value("${simai.clip.deferred-give-up:24h}") Duration giveUp) {
        this.deferral = deferral;
        this.ingestors = ingestors.stream().collect(Collectors.toUnmodifiableMap(Deferrable::msgType, Function.identity()));
        this.clock = clock;
        this.giveUp = giveUp;
    }

    @Scheduled(fixedDelayString = "${simai.clip.deferred-retry-ms:1000}")
    void retry() {
        Instant now = clock.instant();
        for (Parked parked : deferral.due(now, BATCH)) {
            Deferrable ingestor = ingestors.get(parked.msgType());
            if (ingestor == null) {
                // 받는 쪽이 없어진 종류(배포로 빠진 수집기 등). 그대로 두면 NPE 로 매초 영원히 다시 시도한다
                log.warn("보관한 I1 메시지의 종류를 처리할 수집기가 없어 버림: msg_type={} msg_id={}", parked.msgType(), parked.msgId());
                deferral.remove(parked.msgId());
                continue;
            }
            try {
                ingestor.apply(parked.envelope(), parked.receivedAt());
                deferral.remove(parked.msgId());
            } catch (WaitingForEarlier waiting) {
                if (parked.receivedAt().plus(giveUp).isBefore(now)) {
                    log.warn("앞선 메시지가 오지 않아 보관한 I1 메시지를 버림: msg_type={} msg_id={} reason={}",
                            parked.msgType(), parked.msgId(), waiting.getMessage());
                    deferral.remove(parked.msgId());
                } else {
                    deferral.retryLater(parked.msgId(), waiting.getMessage(), now.plus(parked.backoff()));
                }
            } catch (InvalidI1Message e) {
                log.warn("보관한 I1 메시지가 계약에 맞지 않아 버림: msg_type={} msg_id={} reason={}",
                        parked.msgType(), parked.msgId(), e.getMessage());
                deferral.remove(parked.msgId());
            } catch (RuntimeException e) {
                // DB 오류 등. 다음에 다시 본다(오류 문구에는 값이 실릴 수 있어 종류만 남긴다)
                log.warn("보관한 I1 메시지를 처리하지 못함: msg_type={} msg_id={} error={}",
                        parked.msgType(), parked.msgId(), e.getClass().getSimpleName());
                deferral.retryLater(parked.msgId(), "처리 실패 " + e.getClass().getSimpleName(), now.plus(parked.backoff()));
            }
        }
    }
}
