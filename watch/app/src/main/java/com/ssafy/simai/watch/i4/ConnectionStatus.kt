package com.ssafy.simai.watch.i4

enum class LinkState {
    /** EC2 주소·기기 ID·토큰이 없다 */
    NOT_CONFIGURED,
    CONNECTING,
    CONNECTED,
    /** 끊겨서 다시 잇기를 기다린다 */
    DISCONNECTED,
}

/**
 * 상태 화면에 보이는 연결 상태. 서비스가 바꾸고 화면이 관찰한다.
 *
 * @property lastEc2AtMs EC2 하트비트를 마지막으로 받은 워치 시각
 * @property lastFailure 마지막 실패 이유(예외 클래스 이름·문장). 토큰은 들어가지 않는다
 */
data class ConnectionStatus(
    val state: LinkState = LinkState.NOT_CONFIGURED,
    val lastEc2AtMs: Long? = null,
    val clockOffsetMs: Long? = null,
    val transport: Transport = Transport.UNKNOWN,
    val batteryPct: Int? = null,
    val lastFailure: String? = null,
)
