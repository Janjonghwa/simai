package com.ssafy.simai.watch.i4

import com.ssafy.simai.watch.alert.AckKind
import com.ssafy.simai.watch.alert.AckRecord
import org.junit.Assert.assertEquals
import org.junit.Test

class PendingAcksTest {
    private fun record(msgId: String, kind: AckKind = AckKind.VIBRATED, ackedAt: Long = 1L) =
        AckRecord(msgId, "e", null, kind, ackedAt, null)

    @Test
    fun keepsOnePerAlertAndKindInOrder() {
        val pending = PendingAcks()
        pending.add(record("a"))
        pending.add(record("b"))
        pending.add(record("a", AckKind.CONFIRMED))
        pending.add(record("a", ackedAt = 5L)) // 같은 키는 하나만, 뒤의 것이 남는다
        assertEquals(3, pending.size)
        val drained = pending.drain()
        assertEquals(listOf("b" to AckKind.VIBRATED, "a" to AckKind.CONFIRMED, "a" to AckKind.VIBRATED), drained.map { it.key })
        assertEquals(5L, drained.last().ackedAtMs)
        assertEquals(0, pending.size)
    }

    @Test
    fun dropsTheOldestBeyondTheCap() {
        val pending = PendingAcks()
        repeat(MAX_PENDING_ACKS + 3) { pending.add(record("m$it")) }
        assertEquals(MAX_PENDING_ACKS, pending.size)
        assertEquals("m3", pending.drain().first().alertMsgId)
    }
}
