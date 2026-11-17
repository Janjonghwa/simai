package com.ssafy.simai.domain.setup;

import java.util.Arrays;
import java.util.Set;
import java.util.stream.Collectors;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 시연 계정(프론트 features/demo/demoAccount 와 같은 계정). 시연 계정은 「이전 맵 재사용」에서 카메라 시점 확인(MATCH) 없이 완료된 맵을 모두 고를 수
 * 있고 추천을 받지 않는다({@link SetupReuseCheck#require(String, java.util.UUID, boolean)}). 시연은 고정된 3D 맵을 쓰고 보정·맵을 미리
 * 맞춰 두므로 그 자리에서 카메라 확인으로 막지 않는다. 일반 계정의 안전 검사는 그대로다.
 * <p>값은 {@code simai.setup.demo-user-ids}(쉼표로 이은 user_id). 비우면 시연 계정이 없다.
 */
@Component
class SetupDemoAccounts {

    private final Set<Long> userIds;

    SetupDemoAccounts(@Value("${simai.setup.demo-user-ids:}") String userIds) {
        this.userIds = Arrays.stream(userIds.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .map(Long::valueOf)
                .collect(Collectors.toUnmodifiableSet());
    }

    boolean isDemo(long userId) {
        return userIds.contains(userId);
    }
}
