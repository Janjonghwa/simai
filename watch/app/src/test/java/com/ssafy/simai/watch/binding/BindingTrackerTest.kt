package com.ssafy.simai.watch.binding

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BindingTrackerTest {
    private fun notice(state: BindingState) = BindingNotice(
        state = state,
        method = if (state == BindingState.BOUND) BindingMethod.WAVE else null,
        worldId = if (state == BindingState.BOUND) "w-17" else null,
        unbindReason = null,
        sentAtMs = 1L,
    )

    private val bound = notice(BindingState.BOUND)
    private val unbound = notice(BindingState.UNBOUND)

    @Test
    fun unboundShowsGuideOnlyWhileConnectedAndWithoutAlert() {
        val step = BindingTracker.apply(BindingView(), unbound, alertPending = false)
        assertEquals(BindingPhase.UNBOUND, step.view.phase)
        assertFalse(step.vibrate)
        assertTrue(step.view.showGuide(linkConnected = true, alertPending = false))
        assertFalse("연결이 끊기면 안내하지 않는다", step.view.showGuide(linkConnected = false, alertPending = false))
        assertFalse("경보가 먼저다", step.view.showGuide(linkConnected = true, alertPending = true))
    }

    @Test
    fun bindingAfterUnboundVibratesOnceAndConfirms() {
        val unboundView = BindingTracker.apply(BindingView(), unbound, false).view
        val step = BindingTracker.apply(unboundView, bound, alertPending = false)
        assertTrue(step.vibrate)
        assertEquals(BindingPhase.BOUND, step.view.phase)
        assertTrue(step.view.showConnected(alertPending = false))
        assertFalse(step.view.showGuide(linkConnected = true, alertPending = false))

        // 되풀이된 같은 BOUND 는 울리지 않고 상태도 그대로
        val again = BindingTracker.apply(step.view, bound, alertPending = false)
        assertFalse(again.vibrate)
        assertEquals(step.view, again.view)

        val done = BindingTracker.endConfirm(step.view)
        assertFalse(done.showConnected(alertPending = false))
        assertEquals(BindingPhase.BOUND, done.phase)
        assertEquals(done, BindingTracker.endConfirm(done))
    }

    @Test
    fun firstStateBoundShowsButDoesNotVibrate() {
        // 앱 재시작·재연결 동기화로 처음 받는 상태가 BOUND
        val step = BindingTracker.apply(BindingView(), bound, alertPending = false)
        assertFalse(step.vibrate)
        assertTrue(step.view.showConnected(alertPending = false))
    }

    @Test
    fun pendingAlertSuppressesVibrationAndConfirmation() {
        val unboundView = BindingTracker.apply(BindingView(), unbound, false).view
        val step = BindingTracker.apply(unboundView, bound, alertPending = true)
        assertFalse("경보 진동을 끊지 않는다", step.vibrate)
        assertFalse(step.view.confirming)
        assertEquals(BindingPhase.BOUND, step.view.phase)
    }

    @Test
    fun releasedBindingGoesBackToGuideAndRebindingVibratesAgain() {
        val boundView = BindingTracker.apply(BindingView(), bound, false).view
        val released = BindingTracker.apply(boundView, unbound, false)
        assertEquals(BindingPhase.UNBOUND, released.view.phase)
        assertFalse(released.vibrate)
        assertTrue(BindingTracker.apply(released.view, bound, false).vibrate)
    }
}
