package com.ssafy.simai.watch.ui

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private val CLOCK = DateTimeFormatter.ofPattern("HH:mm:ss")

private const val MS_PER_SECOND = 1000L
private const val SECONDS_PER_MINUTE = 60L

/** 기한이 지난 뒤 흐른 시간을 "+0:12" 로 보인다(분:초). */
fun overdueText(overdueMs: Long): String {
    val seconds = overdueMs.coerceAtLeast(0L) / MS_PER_SECOND
    return "+${seconds / SECONDS_PER_MINUTE}:${(seconds % SECONDS_PER_MINUTE).toString().padStart(2, '0')}"
}

/** UTC ms 를 워치의 시간대 시계("14:32:11")로 보인다. */
fun clockText(epochMs: Long, zone: ZoneId = ZoneId.systemDefault()): String =
    CLOCK.format(Instant.ofEpochMilli(epochMs).atZone(zone))
