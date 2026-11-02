package com.ssafy.simai.watch.i4

/** 하트비트 기본 주기(ms). EC2 가 interval_ms 를 주면 그것을 따른다(계약 watch_heartbeat) */
const val DEFAULT_HEARTBEAT_INTERVAL_MS = 10_000L

/** 내 하트비트 이만큼에 EC2 답(echo)이 없으면 연결을 닫고 다시 잇는다(docs/design/watch-i4-contract.md) */
const val UNANSWERED_HEARTBEATS_TO_DROP = 3

/** EC2 가 주는 주기의 허용 범위(계약 interval_ms 1~60초). 밖의 값은 범위로 끌어온다(0 이면 바쁜 루프가 된다) */
const val MIN_HEARTBEAT_INTERVAL_MS = 1_000L
const val MAX_HEARTBEAT_INTERVAL_MS = 60_000L

/** 시계 차이 계산용으로 기억하는 내 하트비트 수. EC2 답은 보통 바로 오므로 몇 개면 된다 */
private const val REMEMBERED_SENT = 8

/**
 * 한 연결 안의 하트비트 상태. 시계를 직접 읽지 않고 시각을 받는 순수한 값이라 JVM 에서 시험한다.
 *
 * 시계 차이(워치 − EC2)는 NTP 처럼 잰다: 내가 seq n 을 t1 에 보내고 EC2 가 echo{n, t2} 를 t3 에 답해 내가 t4 에 받으면
 * ((t1 − t2) + (t4 − t3)) / 2. 워치가 d 만큼 빠르면 +d 다(계약 watch_heartbeat 설명).
 */
class HeartbeatState {
    private var nextSeq = 0L
    private val sentAtMs = LinkedHashMap<Long, Long>()

    /** 하트비트 주기. EC2 가 알려 주면 바뀐다 */
    var intervalMs: Long = DEFAULT_HEARTBEAT_INTERVAL_MS
        private set

    /** EC2 에서 마지막으로 받은 하트비트(다음 내 하트비트의 echo 로 돌려준다). 받은 적 없으면 null */
    var lastEc2: Echo? = null
        private set

    /** EC2 가 마지막으로 답한 내 seq. 답을 받은 적 없으면 -1 */
    var lastAnsweredSeq: Long = -1
        private set

    /** 워치 시계 − EC2 시계(ms). 아직 못 쟀으면 null */
    var clockOffsetMs: Long? = null
        private set

    /** 이 연결에서 EC2 답을 한 번이라도 받았다 */
    val answered: Boolean get() = lastAnsweredSeq >= 0

    /** 보낸 뒤 아직 EC2 가 답하지 않은 내 하트비트 수 */
    val unanswered: Int get() = (nextSeq - (lastAnsweredSeq + 1)).toInt()

    /** 답 없는 하트비트가 상한에 닿았다: 연결을 닫고 다시 잇는다 */
    val isDead: Boolean get() = unanswered >= UNANSWERED_HEARTBEATS_TO_DROP

    /** 다음에 보낼 하트비트의 seq 와 echo. [nowMs] 는 워치 벽시계(봉투 sent_at 과 같은 값이어야 한다) */
    fun nextOutgoing(nowMs: Long): Outgoing {
        val seq = nextSeq++
        sentAtMs[seq] = nowMs
        while (sentAtMs.size > REMEMBERED_SENT) sentAtMs.remove(sentAtMs.keys.first())
        return Outgoing(seq, lastEc2)
    }

    /** EC2 하트비트를 받았다. [nowMs] 는 받은 워치 벽시계(t4) */
    fun onEc2Heartbeat(hb: Incoming.Heartbeat, nowMs: Long) {
        lastEc2 = Echo(hb.seq, nowMs)
        hb.intervalMs?.let { intervalMs = it.coerceIn(MIN_HEARTBEAT_INTERVAL_MS, MAX_HEARTBEAT_INTERVAL_MS) }
        // 내가 보낸 범위 밖의 echo(지난 연결의 seq 등)는 답으로 치지 않는다. 받아들이면 unanswered 가 음수가 돼 끊김을 못 본다
        if (hb.echoSeq >= nextSeq) return
        if (hb.echoSeq > lastAnsweredSeq) lastAnsweredSeq = hb.echoSeq
        val t1 = sentAtMs[hb.echoSeq] ?: return
        clockOffsetMs = ((t1 - hb.echoReceivedAtMs) + (nowMs - hb.sentAtMs)) / 2
    }

    /** 새 연결: seq 는 0 부터, 답 기록은 비운다. 시계 차이와 주기는 지난 연결의 값을 그대로 둔다 */
    fun reset() {
        nextSeq = 0
        sentAtMs.clear()
        lastEc2 = null
        lastAnsweredSeq = -1
    }

    data class Outgoing(val seq: Long, val echo: Echo?)
}
