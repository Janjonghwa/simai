package com.ssafy.simai.domain.tier0;

/** Tier 0 가 붙는 경로. Tier 0 기본값(ai/services/tier0/i1_client.py DEFAULT_I1_PATH)과 같다. */
public final class Tier0Paths {

    /** I1 WebSocket(Tier 0 ↔ EC2, 명세 12.2). Nginx 의 location /ws 가 그대로 서버로 넘긴다. */
    public static final String I1 = "/ws/tier0";

    private Tier0Paths() {
    }
}
