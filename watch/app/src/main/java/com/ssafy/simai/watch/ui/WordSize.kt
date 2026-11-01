package com.ssafy.simai.watch.ui

import com.ssafy.simai.watch.alert.AlertDirection

/** 한 단어 글자 크기의 상한·하한(sp). 20sp 이상은 사용자 글꼴 설정으로 커지지 않는다(Wear 글꼴 규칙) */
private const val WORD_MAX_SP = 36f
private const val WORD_MIN_SP = 20f

/** 작은 화면에서는 상한도 화면 폭에 맞춰 줄인다(큰 아이콘·확인 단추와 같이 세로로 들어가게) */
private const val WORD_MAX_WIDTH_FRACTION = 0.16f

/** 한 단어가 차지할 수 있는 화면 너비 비율. 둥근 화면은 가운데 줄이 가장 넓다 */
private const val WORD_WIDTH_FRACTION = 0.7f

/**
 * 한 단어를 한 줄에 넣을 글자 크기(sp). 짧은 단어는 크게, 8자까지 긴 단어는 화면 폭에 맞춰 줄인다.
 * 한글 한 글자는 글자 크기와 비슷한 너비라서 폭 ÷ 글자 수로 정한다.
 */
fun wordFontSizeSp(screenWidthDp: Float, length: Int): Float =
    (screenWidthDp * WORD_WIDTH_FRACTION / length.coerceAtLeast(1))
        .coerceIn(WORD_MIN_SP, minOf(WORD_MAX_SP, screenWidthDp * WORD_MAX_WIDTH_FRACTION))

/**
 * 큰 한 단어 줄. 방향이 있으면 가리키는 쪽에 화살표를 붙인다(오른쪽은 단어 뒤, 나머지는 단어 앞). 없으면 단어만이다.
 * 화살표를 따로 줄로 두지 않아서 화면이 단순하고, 글자 크기는 이 줄 전체 길이로 정한다.
 */
fun wordLine(word: String, direction: AlertDirection?): String = when (direction) {
    null -> word
    AlertDirection.RIGHT -> "$word ${direction.arrow}"
    else -> "${direction.arrow} $word"
}
