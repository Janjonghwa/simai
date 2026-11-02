package com.ssafy.simai.watch.ui

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private val CLOCK = DateTimeFormatter.ofPattern("HH:mm:ss")
private val BIG_CLOCK_24H = DateTimeFormatter.ofPattern("H:mm")
private val BIG_CLOCK_12H = DateTimeFormatter.ofPattern("h:mm")

private const val MS_PER_SECOND = 1000L
private const val MS_PER_MINUTE = 60_000L
private const val SECONDS_PER_MINUTE = 60L

/** 기한이 지난 뒤 흐른 시간을 "+0:12" 로 보인다(분:초). */
fun overdueText(overdueMs: Long): String {
    val seconds = overdueMs.coerceAtLeast(0L) / MS_PER_SECOND
    return "+${seconds / SECONDS_PER_MINUTE}:${(seconds % SECONDS_PER_MINUTE).toString().padStart(2, '0')}"
}

/** UTC ms 를 워치의 시간대 시계("14:32:11")로 보인다. */
fun clockText(epochMs: Long, zone: ZoneId = ZoneId.systemDefault()): String =
    CLOCK.format(Instant.ofEpochMilli(epochMs).atZone(zone))

/** 기본 화면의 큰 시계("9:05"). 시·분만 보이고 워치의 24시간 설정을 따른다. */
fun bigClockText(epochMs: Long, is24Hour: Boolean, zone: ZoneId = ZoneId.systemDefault()): String =
    (if (is24Hour) BIG_CLOCK_24H else BIG_CLOCK_12H).format(Instant.ofEpochMilli(epochMs).atZone(zone))

/** 다음 분이 바뀔 때까지 남은 ms. 큰 시계는 분이 바뀔 때만 다시 그린다. */
fun msUntilNextMinute(epochMs: Long): Long = MS_PER_MINUTE - epochMs % MS_PER_MINUTE
