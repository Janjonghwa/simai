package com.ssafy.simai.domain.job;

import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/**
 * 대여 long-poll 을 깨우는 신호(새 작업 등록·재할당). 같은 프로세스 안에서만 닿는다.
 * 관리 명령처럼 다른 프로세스가 넣은 작업은 기다리는 쪽이 1초마다 DB 를 다시 보므로 늦어도 1초 안에 나간다.
 * 트랜잭션 안에서 부르면 커밋한 뒤에 깨운다(커밋 전에 깨우면 기다리던 쪽이 아직 QUEUED 를 못 본다).
 */
@Component
public class JobSignal {

    private final Object lock = new Object();
    private long generation;

    public void wake() {
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    wakeNow();
                }
            });
        } else {
            wakeNow();
        }
    }

    private void wakeNow() {
        synchronized (lock) {
            generation++;
            lock.notifyAll();
        }
    }

    long generation() {
        synchronized (lock) {
            return generation;
        }
    }

    /** seen 이후 wake 가 있었거나 maxWaitMs 가 지날 때까지 기다린다 */
    void await(long seen, long maxWaitMs) throws InterruptedException {
        long deadline = System.nanoTime() + maxWaitMs * 1_000_000;
        synchronized (lock) {
            long remaining;
            while (generation == seen && (remaining = (deadline - System.nanoTime()) / 1_000_000) > 0) {
                lock.wait(remaining);
            }
        }
    }
}
