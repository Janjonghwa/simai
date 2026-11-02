package com.ssafy.simai.watch.ui

private const val MS_PER_MINUTE = 60_000L

/** 번인 방지로 분마다 옮기는 위치(dp). 네 곳을 돌아서 한 자리에 오래 켜 두지 않는다. 큰 시계는 가장자리에서 멀어 ±3dp 면 충분하다. */
private val BURN_IN_SHIFTS_DP = listOf(-3 to -3, 3 to -3, 3 to 3, -3 to 3)

/**
 * 손목을 내려 어둡게 켜져 있는 화면(상시표시, 앰비언트 모드)에서 화면이 알아야 할 것.
 * [lowBit]: 흑백 두 색만 낼 수 있는 화면이라 회색을 쓸 수 없다.
 * [burnInProtection]: 같은 자리에 오래 켜 두면 번인이 생겨서 위치를 조금씩 옮겨야 하는 화면.
 */
data class AmbientState(val lowBit: Boolean, val burnInProtection: Boolean)

/** [epochMs] 가 속한 분에 옮길 위치(x, y, dp). 같은 분 안에서는 늘 같다. */
fun burnInShiftDp(epochMs: Long): Pair<Int, Int> =
    BURN_IN_SHIFTS_DP[((epochMs / MS_PER_MINUTE) % BURN_IN_SHIFTS_DP.size).toInt()]
