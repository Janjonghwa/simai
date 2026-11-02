package com.ssafy.simai.domain.cctv;

import java.io.IOException;
import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.Map;
import java.util.function.LongConsumer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.socket.BinaryMessage;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.PingMessage;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketMessage;
import org.springframework.web.socket.WebSocketSession;

/**
 * 원격 시청자 하나의 송신 대기열과 보내는 스레드(S15P31A101-400). 중계(gateway/relay preview.PreviewConsumer)와 같은 규칙이다:
 * <ul>
 * <li>카메라별로 다음 키프레임부터 넣는다(접속 직후, codec·hello 를 받은 뒤, 넘쳐 건너뛴 뒤).</li>
 * <li>프레임 수·바이트 상한을 넘으면 그 시청자의 그 카메라만 다음 키프레임까지 건너뛴다. 느린 시청자는 다른 시청자·수신에 영향이 없다.</li>
 * <li>텍스트(status·hello·codec)와 ping 은 상한에 세지 않는다(작고, 빠지면 디코더를 설정하지 못한다).</li>
 * </ul>
 * 넣기는 수신 스레드(게이트웨이 메시지)가, 보내기는 이 시청자의 가상 스레드 하나가 한다. 영상은 메모리 대기열에만 잠깐 있고 저장하지 않는다.
 */
final class CctvViewer {

    private static final Logger log = LoggerFactory.getLogger(CctvViewer.class);

    /** 대기열 맨 끝에서 연결을 닫으라는 표시. */
    private static final WebSocketMessage<?> CLOSE = new TextMessage("");

    enum Offer {
        QUEUED, WAITING_KEYFRAME, DROPPED_OVERFLOW, CLOSED
    }

    private record Item(WebSocketMessage<?> message, long countedBytes) {
    }

    final WebSocketSession session;
    final long userId;
    private final int maxFrames;
    private final long maxBytes;
    private final Object lock = new Object();
    private final ArrayDeque<Item> items = new ArrayDeque<>();
    private final Map<String, Boolean> waiting = new HashMap<>();
    private int frames;
    private long bytes;
    private boolean ending;
    private CloseStatus endStatus = CloseStatus.NORMAL;
    private boolean closed;
    private volatile long lastPongNanos = System.nanoTime();
    private long droppedOverflow;
    private long waitingKeyframe;

    CctvViewer(WebSocketSession session, long userId, int maxFrames, long maxBytes) {
        this.session = session;
        this.userId = userId;
        this.maxFrames = maxFrames;
        this.maxBytes = maxBytes;
    }

    /** status·hello·codec. cameraId 가 있으면(codec) 그 카메라를, resetAll 이면(hello) 모든 카메라를 다음 키프레임부터. */
    void offerText(String text, String cameraId, boolean resetAll) {
        synchronized (lock) {
            if (closed || ending) {
                return;
            }
            if (resetAll) {
                waiting.clear();
            } else if (cameraId != null) {
                waiting.put(cameraId, Boolean.TRUE);
            }
            items.add(new Item(new TextMessage(text), 0));
            lock.notifyAll();
        }
    }

    Offer offerFrame(String cameraId, boolean keyframe, byte[] payload) {
        synchronized (lock) {
            if (closed || ending) {
                return Offer.CLOSED;
            }
            if (waiting.getOrDefault(cameraId, Boolean.TRUE) && !keyframe) {
                waitingKeyframe++;
                return Offer.WAITING_KEYFRAME;
            }
            if (frames >= maxFrames || bytes + payload.length > maxBytes) {
                waiting.put(cameraId, Boolean.TRUE);
                droppedOverflow++;
                return Offer.DROPPED_OVERFLOW;
            }
            waiting.put(cameraId, Boolean.FALSE);
            items.add(new Item(new BinaryMessage(payload), payload.length));
            frames++;
            bytes += payload.length;
            lock.notifyAll();
            return Offer.QUEUED;
        }
    }

    void offerPing() {
        synchronized (lock) {
            if (closed || ending) {
                return;
            }
            items.addFirst(new Item(new PingMessage(), 0));
            lock.notifyAll();
        }
    }

    /** 대기 중인 영상을 버리고 마지막 status 하나를 보낸 뒤 닫는다(시청 상한·월 상한). */
    void endWith(String statusText, CloseStatus status) {
        synchronized (lock) {
            if (closed || ending) {
                return;
            }
            items.clear();
            frames = 0;
            bytes = 0;
            items.add(new Item(new TextMessage(statusText), 0));
            items.add(new Item(CLOSE, 0));
            ending = true;
            endStatus = status;
            lock.notifyAll();
        }
    }

    /** 보내기를 멈춘다(연결이 닫혔을 때). */
    void close() {
        synchronized (lock) {
            closed = true;
            items.clear();
            frames = 0;
            bytes = 0;
            lock.notifyAll();
        }
    }

    void pong() {
        lastPongNanos = System.nanoTime();
    }

    long lastPongNanos() {
        return lastPongNanos;
    }

    long droppedOverflow() {
        synchronized (lock) {
            return droppedOverflow;
        }
    }

    long waitingKeyframe() {
        synchronized (lock) {
            return waitingKeyframe;
        }
    }

    /** 이 시청자의 보내는 스레드 본문. 보낸 본문 바이트(ping 제외)를 sent 로 알린다. */
    void sendLoop(LongConsumer sent) {
        try {
            while (true) {
                Item item;
                synchronized (lock) {
                    while (items.isEmpty() && !closed) {
                        lock.wait();
                    }
                    if (closed) {
                        return;
                    }
                    item = items.poll();
                    if (item.countedBytes() > 0) {
                        frames--;
                        bytes -= item.countedBytes();
                    }
                }
                if (item.message() == CLOSE) {
                    session.close(endStatus);
                    return;
                }
                session.sendMessage(item.message());
                if (!(item.message() instanceof PingMessage)) {
                    sent.accept(item.message().getPayloadLength());
                }
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } catch (IOException | IllegalStateException e) {
            log.debug("원격 CCTV 시청자에게 보내지 못해 닫는다: session={} {}", session.getId(), e.getClass().getSimpleName());
            closeQuietly(CloseStatus.SESSION_NOT_RELIABLE);
        }
    }

    void closeQuietly(CloseStatus status) {
        try {
            if (session.isOpen()) {
                session.close(status);
            }
        } catch (IOException | IllegalStateException e) {
            log.debug("원격 CCTV 시청 연결을 닫지 못했다: session={}", session.getId());
        }
    }
}
