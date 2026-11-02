package com.ssafy.simai.watch.i4

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit

/** OkHttp 의 WebSocket ping 간격. 하트비트와 별개로 TCP 가 살아 있는지 본다 */
private const val PING_INTERVAL_S = 30L
private const val CONNECT_TIMEOUT_S = 10L

/** 워치가 닫을 때 쓰는 코드(4000~4999 는 응용 전용). EC2 답 없음은 close 가 아니라 cancel 로 끊는다(상대가 죽어 있으면 close 는 60초를 기다린다) */
const val CLOSE_RECONFIGURED = 4001
const val CLOSE_STOPPING = 4002

/** 상대가 닫기 시작했을 때 맞닫는 코드. 받은 코드를 되돌리면 1005(코드 없음) 같은 예약 코드로 close 가 예외를 낸다 */
private const val CLOSE_NORMAL = 1000

/**
 * EC2 I4 WebSocket 연결 하나를 연다(wss, Authorization: Bearer 기기 토큰 — I1 Tier 0·I6 워커와 같은 방식).
 * 콜백은 OkHttp 스레드에서 오므로 받는 쪽이 자기 스레드로 옮긴다.
 */
class I4Connection(private val client: OkHttpClient = defaultClient()) {

    interface Listener {
        fun onOpen()
        fun onText(text: String)

        /** 상대가 닫았거나 내가 닫은 뒤 */
        fun onClosed(code: Int, reason: String)

        /** 연결 실패·끊김. [t] 의 문장에 토큰은 없다(헤더는 예외 문장에 들어가지 않는다) */
        fun onFailure(t: Throwable)
    }

    fun open(wsUrl: String, deviceToken: String, listener: Listener): WebSocket {
        val request = Request.Builder().url(wsUrl).header("Authorization", "Bearer $deviceToken").build()
        return client.newWebSocket(
            request,
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) = listener.onOpen()
                override fun onMessage(webSocket: WebSocket, text: String) = listener.onText(text)
                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    // 상대가 닫기 시작하면 바로 맞닫는다. 그래야 onClosed 가 온다
                    webSocket.close(CLOSE_NORMAL, null)
                }
                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = listener.onClosed(code, reason)
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = listener.onFailure(t)
            },
        )
    }

    companion object {
        fun defaultClient(): OkHttpClient =
            OkHttpClient.Builder()
                .connectTimeout(CONNECT_TIMEOUT_S, TimeUnit.SECONDS)
                .pingInterval(PING_INTERVAL_S, TimeUnit.SECONDS)
                .build()
    }
}
