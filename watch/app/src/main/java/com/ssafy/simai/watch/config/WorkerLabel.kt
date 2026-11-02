package com.ssafy.simai.watch.config

/**
 * 기본 화면에 보이는 착용자 이름과 구역. 값을 줄 서버 계약(S15P31A101-334)이 아직 없어서
 * 지금은 debug 빌드의 adb 입구로만 넣는다. 둥근 화면 한 줄에 들어가게 길이를 막는다.
 */
data class WorkerLabel(val name: String, val zone: String) {
    init {
        require(name.isNotBlank() && name.length <= MAX_LENGTH) { "이름은 1~$MAX_LENGTH 자여야 한다" }
        require(zone.isNotBlank() && zone.length <= MAX_LENGTH) { "구역은 1~$MAX_LENGTH 자여야 한다" }
    }

    companion object {
        const val MAX_LENGTH = 12
    }
}
