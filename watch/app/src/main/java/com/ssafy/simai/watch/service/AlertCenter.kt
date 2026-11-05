package com.ssafy.simai.watch.service

import com.ssafy.simai.watch.alert.AlertBoard
import com.ssafy.simai.watch.binding.BindingView
import com.ssafy.simai.watch.i4.ConnectionStatus
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * 화면이 보는 서비스의 상태. 알림 보관·진동·ACK 는 [I4Service] 가 하고, 화면(AlertActivity·MainActivity)은 여기 흐름을 관찰하고
 * 확인만 넘긴다. 서비스가 없으면 빈 보드·설정 안 됨이다.
 */
object AlertCenter {
    private val _board = MutableStateFlow(AlertBoard())
    private val _status = MutableStateFlow(ConnectionStatus())
    private val _binding = MutableStateFlow(BindingView())
    private var service: I4Service? = null

    val board: StateFlow<AlertBoard> = _board.asStateFlow()
    val status: StateFlow<ConnectionStatus> = _status.asStateFlow()

    /** 배정 안내 상태(S15P31A101-510). EC2 의 watch_binding_notice 로 바뀐다 */
    val binding: StateFlow<BindingView> = _binding.asStateFlow()
    val isRunning: Boolean get() = service != null

    /** 작업자가 확인 단추를 눌렀다. 서비스가 없으면 아무 일도 없다 */
    fun acknowledge(eventId: String) {
        service?.acknowledge(eventId)
    }

    internal fun attach(s: I4Service) {
        service = s
    }

    internal fun detach(s: I4Service) {
        if (service !== s) return
        service = null
        _board.value = AlertBoard()
        _status.value = ConnectionStatus()
        _binding.value = BindingView()
    }

    internal fun publishBoard(board: AlertBoard) {
        _board.value = board
    }

    internal fun publishStatus(status: ConnectionStatus) {
        _status.value = status
    }

    internal fun publishBinding(binding: BindingView) {
        _binding.value = binding
    }
}
