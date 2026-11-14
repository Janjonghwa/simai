package com.ssafy.simai.domain.gpu;

import java.util.UUID;

/** GPU 세션이 끝났다(커밋 뒤 받는다). 웹 서버는 그 세션 Tier 0 토큰으로 연 I1 연결을 바로 끊는다(GpuSessionSweeper) */
public record GpuSessionEnded(UUID sessionId, GpuSessionService.EndReason reason) {
}
