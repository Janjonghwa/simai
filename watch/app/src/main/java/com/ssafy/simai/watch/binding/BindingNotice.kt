package com.ssafy.simai.watch.binding

/** 배정 상태(계약 watch_binding_notice.payload.state, S15P31A101-510) */
enum class BindingState { BOUND, UNBOUND }

/** 배정 방식(계약 method). 모르는 값은 null 로 둔다(수신 측은 모르는 것을 무시한다, 명세 12.1) */
enum class BindingMethod {
    MOTION_MATCH,
    WAVE,
    MANUAL,
    ;

    companion object {
        fun parse(text: String): BindingMethod? = entries.firstOrNull { it.name == text }
    }
}

/**
 * EC2 가 보낸 배정 알림 한 건. 익명 World ID 만 있고 사람 이름은 없다.
 *
 * @property worldId BOUND 일 때만
 * @property unbindReason UNBOUND 일 때 직전 배정이 끝난 사유(없을 수 있다)
 * @property sentAtMs EC2 가 이 상태를 정한 시각(EC2 시계)
 */
data class BindingNotice(
    val state: BindingState,
    val method: BindingMethod?,
    val worldId: String?,
    val unbindReason: String?,
    val sentAtMs: Long,
)

/** 화면이 보는 배정 단계. UNKNOWN 은 아직 EC2 가 알려 주지 않았다 */
enum class BindingPhase { UNKNOWN, UNBOUND, BOUND }

/**
 * 배정 안내의 화면 상태.
 *
 * @property confirming 방금 BOUND 가 돼 "연결됨" 을 잠깐 보이는 중
 */
data class BindingView(val phase: BindingPhase = BindingPhase.UNKNOWN, val confirming: Boolean = false) {
    /** "워치 찬 손을 3초 흔들어 주세요" 안내를 보일 때: 연결돼 있고 배정이 없고 확인 대기 알림이 없다(경보 우선) */
    fun showGuide(linkConnected: Boolean, alertPending: Boolean): Boolean =
        phase == BindingPhase.UNBOUND && linkConnected && !alertPending

    /** "연결됨" 을 보일 때 */
    fun showConnected(alertPending: Boolean): Boolean = phase == BindingPhase.BOUND && confirming && !alertPending
}

/** [BindingTracker.apply] 의 결과: 새 화면 상태와 진동할지 */
data class BindingStep(val view: BindingView, val vibrate: Boolean)

/** 배정 알림을 받았을 때의 상태 전이. 시계도 Android 도 읽지 않는 순수한 함수라 JVM 에서 시험한다. */
object BindingTracker {
    /** "연결됨" 을 보이는 시간(ms) */
    const val CONFIRM_SHOW_MS = 3_000L

    /**
     * @param alertPending 확인 대기 중인 위험 알림이 있다. 있으면 진동도 "연결됨" 표시도 하지 않는다(경보 진동을 끊지 않는다)
     */
    fun apply(view: BindingView, notice: BindingNotice, alertPending: Boolean): BindingStep = when (notice.state) {
        BindingState.UNBOUND -> BindingStep(BindingView(BindingPhase.UNBOUND), vibrate = false)
        // 같은 BOUND 가 되풀이돼도 다시 울리지 않는다
        BindingState.BOUND ->
            if (view.phase == BindingPhase.BOUND) {
                BindingStep(view, vibrate = false)
            } else {
                // 배정이 없던 것을 본 뒤 생긴 배정만 진동한다. 처음 받는 상태가 BOUND(재시작·재연결 동기화)면 표시만 한다
                BindingStep(
                    BindingView(BindingPhase.BOUND, confirming = !alertPending),
                    vibrate = view.phase == BindingPhase.UNBOUND && !alertPending,
                )
            }
    }

    /** "연결됨" 표시 시간이 끝났다 */
    fun endConfirm(view: BindingView): BindingView = if (view.confirming) view.copy(confirming = false) else view
}
