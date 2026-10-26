package com.ssafy.simai.domain.tier0.i1;

/** world_state 하나를 저장했다. 트랜잭션이 끝난 뒤 /topic/world 로 그대로 다시 싣는다(명세 12.4). */
public record WorldStateStored(I1Envelope envelope) {
}
