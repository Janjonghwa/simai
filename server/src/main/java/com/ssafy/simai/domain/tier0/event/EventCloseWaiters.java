package com.ssafy.simai.domain.tier0.event;

import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.springframework.stereotype.Component;

/**
 * 운영자 종료 REST 요청이 Tier 0 의 event_close_result 를 잠깐 기다리게 한다(명세 12.6 "결과 event_close_result 를 돌려줌", S15P31A101-221).
 * 결과 행이 커밋된 뒤에 깨우므로, 깨어난 요청은 DB 에서 결과를 읽는다. 기다리는 요청이 없으면 알림은 버린다.
 */
@Component
public class EventCloseWaiters {

    private final ConcurrentMap<UUID, CompletableFuture<Void>> waiting = new ConcurrentHashMap<>();

    /** I1 으로 보내기 전에 등록한다(결과가 등록보다 먼저 오면 놓친다) */
    public void register(UUID closeId) {
        waiting.putIfAbsent(closeId, new CompletableFuture<>());
    }

    /** @return 시간 안에 결과가 왔으면 true. 어느 쪽이든 등록을 지운다 */
    public boolean await(UUID closeId, Duration timeout) {
        CompletableFuture<Void> future = waiting.get(closeId);
        if (future == null) {
            return false;
        }
        try {
            future.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
            return true;
        } catch (TimeoutException | ExecutionException e) {
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } finally {
            waiting.remove(closeId, future);
        }
    }

    public void cancel(UUID closeId) {
        waiting.remove(closeId);
    }

    void complete(UUID closeId) {
        CompletableFuture<Void> future = waiting.get(closeId);
        if (future != null) {
            future.complete(null);
        }
    }
}
