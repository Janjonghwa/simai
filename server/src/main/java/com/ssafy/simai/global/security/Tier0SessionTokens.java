package com.ssafy.simai.global.security;

import java.util.Optional;
import java.util.UUID;

/**
 * GPU 세션마다 발급한 Tier 0 토큰 확인(S15P31A101-699). 환경 변수 해시(Tier0Properties, 손으로 켠 세션)와 함께 받는다.
 * 구현은 GpuSessionService 다. 끝난 세션의 토큰은 비어 있다.
 */
public interface Tier0SessionTokens {

    /** @param tokenSha256Hex Tier 0 토큰 원문의 SHA-256 16진수 64자. @return 그 토큰을 발급한 열린 GPU 세션 */
    Optional<UUID> activeSessionForTier0Token(String tokenSha256Hex);
}
