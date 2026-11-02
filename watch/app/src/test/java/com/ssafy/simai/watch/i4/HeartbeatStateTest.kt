package com.ssafy.simai.watch.i4

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class HeartbeatStateTest {
    private fun ec2(seq: Long, sentAt: Long, echoSeq: Long, echoReceivedAt: Long, intervalMs: Long? = null) =
        Incoming.Heartbeat(seq, sentAt, echoSeq, echoReceivedAt, intervalMs)

    @Test
    fun sequenceStartsAtZeroAndEchoesTheLastEc2Heartbeat() {
        val state = HeartbeatState()
        val first = state.nextOutgoing(1_000)
        assertEquals(0L, first.seq)
        assertNull(first.echo)
        state.onEc2Heartbeat(ec2(seq = 7, sentAt = 1_010, echoSeq = 0, echoReceivedAt = 1_005), nowMs = 1_020)
        val second = state.nextOutgoing(11_000)
        assertEquals(1L, second.seq)
        assertEquals(Echo(7, 1_020), second.echo)
    }

    @Test
    fun clockOffsetIsWatchMinusEc2() {
        // 워치가 EC2 보다 500ms 빠르고 지연은 없다: t1=T+500(워치), t2=T(EC2), t3=T(EC2), t4=T+500(워치)
        val state = HeartbeatState()
        state.nextOutgoing(10_500)
        state.onEc2Heartbeat(ec2(seq = 0, sentAt = 10_000, echoSeq = 0, echoReceivedAt = 10_000), nowMs = 10_500)
        assertEquals(500L, state.clockOffsetMs)
        // 워치가 300ms 느리면 음수. 편도 지연 100ms 는 상쇄된다
        state.nextOutgoing(20_000)
        state.onEc2Heartbeat(ec2(seq = 1, sentAt = 20_400, echoSeq = 1, echoReceivedAt = 20_400), nowMs = 20_200)
        assertEquals(-300L, state.clockOffsetMs)
    }

    @Test
    fun offsetStaysUnknownUntilAnEchoOfOurOwnHeartbeat() {
        // 보낸 적 없는 seq(지난 연결의 것 등)의 echo 는 답으로 치지 않는다
        val state = HeartbeatState()
        state.nextOutgoing(0)
        state.onEc2Heartbeat(ec2(seq = 0, sentAt = 1, echoSeq = 99, echoReceivedAt = 1), nowMs = 2)
        assertNull(state.clockOffsetMs)
        assertFalse(state.answered)
        assertEquals(1, state.unanswered)
        assertEquals(Echo(0, 2), state.nextOutgoing(3).echo) // EC2 하트비트 자체는 echo 로 돌려준다
    }

    @Test
    fun intervalIsClampedToTheContractRange() {
        val state = HeartbeatState()
        state.nextOutgoing(0)
        state.onEc2Heartbeat(ec2(seq = 0, sentAt = 1, echoSeq = 0, echoReceivedAt = 1, intervalMs = 0), nowMs = 2)
        assertEquals(MIN_HEARTBEAT_INTERVAL_MS, state.intervalMs)
        state.onEc2Heartbeat(ec2(seq = 1, sentAt = 3, echoSeq = 0, echoReceivedAt = 3, intervalMs = 999_999), nowMs = 4)
        assertEquals(MAX_HEARTBEAT_INTERVAL_MS, state.intervalMs)
    }

    @Test
    fun threeUnansweredHeartbeatsMeanDead() {
        val state = HeartbeatState()
        state.nextOutgoing(0)
        state.nextOutgoing(10_000)
        assertEquals(2, state.unanswered)
        assertFalse(state.isDead)
        state.nextOutgoing(20_000)
        assertTrue(state.isDead)
        // 답이 오면 산다
        state.onEc2Heartbeat(ec2(seq = 1, sentAt = 20_001, echoSeq = 2, echoReceivedAt = 20_000), nowMs = 20_002)
        assertEquals(0, state.unanswered)
        assertFalse(state.isDead)
    }

    @Test
    fun ec2SetsTheIntervalAndResetKeepsIt() {
        val state = HeartbeatState()
        assertEquals(DEFAULT_HEARTBEAT_INTERVAL_MS, state.intervalMs)
        state.nextOutgoing(0)
        state.onEc2Heartbeat(ec2(seq = 0, sentAt = 1, echoSeq = 0, echoReceivedAt = 1, intervalMs = 5_000), nowMs = 2)
        assertEquals(5_000L, state.intervalMs)
        state.reset()
        assertEquals(5_000L, state.intervalMs)
        assertEquals(0L, state.nextOutgoing(100).seq)
        assertNull(state.nextOutgoing(200).echo)
        assertEquals(2, state.unanswered)
    }
}
