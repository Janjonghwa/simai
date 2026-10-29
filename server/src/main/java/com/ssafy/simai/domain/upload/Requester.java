package com.ssafy.simai.domain.upload;

import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.global.security.Tier0TokenAuthenticationFilter;
import java.util.Optional;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.GrantedAuthority;

/** 업로드 주소를 요청한 주체(upload_grant.requester). 기기는 deviceId, 사람은 userId, 워커는 workerId 가 있다 */
record Requester(Kind kind, String deviceId, Long userId, String workerId) {

    enum Kind {
        TIER0, DEVICE, USER, WORKER
    }

    /** 조치 사진을 받는 사람 역할. 업로드 체인(SecurityConfig)도 같은 Role 값으로 막는다 */
    static final String FOREMAN = "ROLE_" + Role.FOREMAN.name();

    /** 업로드 체인(SecurityConfig)이 붙인 권한으로 고른다. 사람은 JWT sub(user_id) 가 이름이다 */
    static Optional<Requester> of(Authentication auth) {
        if (has(auth, Tier0TokenAuthenticationFilter.ROLE)) {
            return Optional.of(new Requester(Kind.TIER0, null, null, null));
        }
        if (has(auth, MachineKind.DEVICE.authority())) {
            return Optional.of(new Requester(Kind.DEVICE, auth.getName(), null, null));
        }
        if (has(auth, MachineKind.WORKER.authority())) {
            return Optional.of(new Requester(Kind.WORKER, null, null, auth.getName()));
        }
        if (has(auth, FOREMAN)) {
            // sub 는 user_id 다(AccessTokenIssuer). 숫자가 아니면 우리가 발급한 사용자 토큰이 아니라 거부한다
            try {
                return Optional.of(new Requester(Kind.USER, null, Long.parseLong(auth.getName()), null));
            } catch (NumberFormatException e) {
                return Optional.empty();
            }
        }
        return Optional.empty();
    }

    private static boolean has(Authentication auth, String authority) {
        return auth.getAuthorities().stream().map(GrantedAuthority::getAuthority).anyMatch(authority::equals);
    }
}
