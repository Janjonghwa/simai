package com.ssafy.simai.domain.tier0.event;

import java.util.HashSet;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;

/**
 * I1 연결 하나의 저널 수신 위치(명세 3.2 저널 계약). DB 를 모르는 순수 상태라 단위 시험으로 본다.
 *
 * <p>acked 는 "빈틈없이 저장한 마지막 journal_offset" 이다. 저장된 커서가 없으면 이 연결에서 처음 받은 offset 바로 앞을 기준으로 잡는다
 * (Tier 0 는 연결할 때 자기 acked_offset 다음부터 다시 보낸다). acked 다음이 아닌 offset 은 받아 두고(above), 처음 보는 빈 구간이면
 * 그 시작을 다시 요청한다(journal_ack.resend_from_offset). 빈 구간이 다 차면 요청을 지운다.
 *
 * <p>연결은 동기화 모드로 시작하고 event_state_snapshot 으로 끝난다(3.2). 동기화 중에 상태가 바뀐 이벤트를 모아 스냅샷 때 분류한다.
 */
public final class JournalTracker {

    /** null = 기준을 아직 모른다 */
    private Long acked;
    private final TreeSet<Long> above = new TreeSet<>();
    private Long resendRequested;
    private boolean syncMode = true;
    private final Set<UUID> touchedDuringSync = new HashSet<>();
    /** 마지막으로 보낸 ack 뒤에 acked 가 나아갔다 */
    private boolean ackPending;

    public JournalTracker(Long storedAcked) {
        this.acked = storedAcked;
    }

    /**
     * offset 이 저장됐다(새로 넣었거나 이미 있었다).
     * @return 지금 다시 요청할 시작 offset, 없으면 null
     */
    public synchronized Long onPersisted(long offset) {
        if (acked == null) {
            acked = offset - 1;
        }
        if (offset <= acked) {
            return null;
        }
        if (offset == acked + 1) {
            acked = offset;
            while (above.remove(acked + 1)) {
                acked++;
            }
            ackPending = true;
            if (above.isEmpty()) {
                resendRequested = null;
            }
            return null;
        }
        above.add(offset);
        if (resendRequested == null || resendRequested <= acked) {
            resendRequested = acked + 1;
            return resendRequested;
        }
        return null;
    }

    /**
     * 스냅샷이 접어 넣은 마지막 offset 까지 빈틈없이 받았는가. null(저널 없음)이면 받을 것이 없다.
     * 이 연결·저장 커서 모두 기준이 없으면(EC2 DB 가 새로 만들어짐 등) 스냅샷 offset 을 기준으로 받아들인다.
     * @return 다시 요청할 시작 offset, 다 받았으면 null
     */
    public synchronized Long missingBefore(Long snapshotOffset) {
        if (snapshotOffset == null) {
            return null;
        }
        if (acked == null) {
            acked = snapshotOffset;
            ackPending = true;
            return null;
        }
        if (acked >= snapshotOffset) {
            return null;
        }
        resendRequested = acked + 1;
        return resendRequested;
    }

    public synchronized Long acked() {
        return acked;
    }

    public synchronized boolean syncMode() {
        return syncMode;
    }

    public synchronized void touched(UUID eventId) {
        if (syncMode) {
            touchedDuringSync.add(eventId);
        }
    }

    /** 동기화를 끝내고, 동기화 중에 바뀐 이벤트를 돌려준다 */
    public synchronized Set<UUID> finishSync() {
        syncMode = false;
        Set<UUID> touched = Set.copyOf(touchedDuringSync);
        touchedDuringSync.clear();
        return touched;
    }

    public synchronized boolean hasPendingAck() {
        return ackPending && acked != null && acked >= 0;
    }

    /** 보낼 ack 가 있으면 acked 를 돌려주고 비운다. Tier 0 는 음수 acked_offset 을 받지 않는다 */
    public synchronized Long takePendingAck() {
        if (!ackPending || acked == null || acked < 0) {
            return null;
        }
        ackPending = false;
        return acked;
    }
}
