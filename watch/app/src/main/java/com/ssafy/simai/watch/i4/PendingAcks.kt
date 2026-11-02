package com.ssafy.simai.watch.i4

import com.ssafy.simai.watch.alert.AckKind
import com.ssafy.simai.watch.alert.AckRecord

/** 연결이 끊긴 동안 쌓아 둘 확인 수의 상한. 넘으면 가장 오래된 것을 버린다 */
const val MAX_PENDING_ACKS = 64

/**
 * 아직 EC2 에 닿지 않은 확인. 다시 이어지면 원래 acked_at 그대로 보낸다(계약 watch_ack).
 * 같은 (alert_msg_id, kind) 는 하나만 둔다(EC2 가 그 키로 중복을 제거한다).
 */
class PendingAcks {
    private val records = LinkedHashMap<Pair<String, AckKind>, AckRecord>()

    val size: Int get() = records.size

    fun add(record: AckRecord) {
        records.remove(record.key)
        records[record.key] = record
        while (records.size > MAX_PENDING_ACKS) records.remove(records.keys.first())
    }

    /** 보낼 것을 모두 꺼낸다(오래된 것부터). 보내다 실패한 것은 [add] 로 다시 넣는다 */
    fun drain(): List<AckRecord> {
        val all = records.values.toList()
        records.clear()
        return all
    }
}
