package com.ssafy.simai.global.security;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.global.properties.Tier0Properties;
import java.util.HexFormat;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.core.context.SecurityContextHolder;

class Tier0TokenAuthenticationFilterTest {

    private static final String TOKEN = "some-tier0-token";
    private static final String HASH = HexFormat.of().formatHex(TokenHashes.sha256(TOKEN));

    @AfterEach
    void clear() {
        SecurityContextHolder.clearContext();
    }

    @Test
    void 맞는_해시면_TIER0_로_인증한다() throws Exception {
        assertThat(authenticates(HASH, "Bearer " + TOKEN)).isTrue();
        assertThat(authenticates(HASH.toUpperCase(), "BEARER " + TOKEN)).isTrue();
    }

    @Test
    void 틀린_토큰은_인증하지_않는다() throws Exception {
        assertThat(authenticates(HASH, "Bearer other")).isFalse();
        assertThat(authenticates(HASH, "Basic " + TOKEN)).isFalse();
        assertThat(authenticates(HASH, null)).isFalse();
    }

    @Test
    void 설정이_비었거나_형식이_틀려도_서버를_막지_않고_모두_거부한다() throws Exception {
        // sha256sum 출력 줄을 통째로 넣은 경우, 잘린 경우, 비운 경우
        assertThat(authenticates(HASH + "  -", "Bearer " + TOKEN)).isFalse();
        assertThat(authenticates(HASH.substring(0, 62), "Bearer " + TOKEN)).isFalse();
        assertThat(authenticates("", "Bearer " + TOKEN)).isFalse();
    }

    private static boolean authenticates(String configuredHash, String authorization) throws Exception {
        SecurityContextHolder.clearContext();
        Tier0TokenAuthenticationFilter filter = new Tier0TokenAuthenticationFilter(new Tier0Properties(configuredHash));
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/ws/tier0");
        if (authorization != null) {
            request.addHeader("Authorization", authorization);
        }
        filter.doFilter(request, new MockHttpServletResponse(), new MockFilterChain());
        var authentication = SecurityContextHolder.getContext().getAuthentication();
        return authentication != null && authentication.getAuthorities().stream()
                .anyMatch(a -> a.getAuthority().equals(Tier0TokenAuthenticationFilter.ROLE));
    }
}
