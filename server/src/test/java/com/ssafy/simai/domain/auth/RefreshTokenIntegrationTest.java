package com.ssafy.simai.domain.auth;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.auth.entity.RefreshToken;
import com.ssafy.simai.domain.auth.repository.RefreshTokenRepository;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import jakarta.servlet.http.Cookie;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class RefreshTokenIntegrationTest {

    private static final String PASSWORD = "correct-horse";
    private static final String COOKIE = "simai_refresh";

    @Autowired
    MockMvc mvc;

    @Autowired
    AppUserRepository users;

    @Autowired
    RefreshTokenRepository refreshTokens;

    @Autowired
    PasswordEncoder passwordEncoder;

    @Test
    void 로그인하면_갱신_토큰을_HttpOnly_쿠키로만_준다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);

        MockHttpServletResponse response = login("viewer-1")
                .andExpect(jsonPath("$.accessToken").isNotEmpty())
                .andExpect(jsonPath("$.refreshToken").doesNotExist())
                .andReturn().getResponse();

        String setCookie = response.getHeader(HttpHeaders.SET_COOKIE);
        assertThat(setCookie)
                .startsWith(COOKIE + "=")
                .contains("HttpOnly", "Secure", "SameSite=Strict", "Path=/api/auth", "Max-Age=" + 14 * 24 * 60 * 60);
    }

    @Test
    void 갱신_토큰_쿠키로_새_access_와_새_쿠키를_받는다() throws Exception {
        AppUser user = saveUser("foreman-1", Role.FOREMAN);
        String first = refreshCookie(login("foreman-1"));

        ResultActions result = refresh(first)
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.accessToken").isNotEmpty())
                .andExpect(jsonPath("$.user.id").value(String.valueOf(user.getId())))
                .andExpect(jsonPath("$.user.role").value("FOREMAN"));

        assertThat(refreshCookie(result)).isNotBlank().isNotEqualTo(first);
    }

    @Test
    void 원문은_저장하지_않고_해시만_남긴다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);
        String token = refreshCookie(login("viewer-1"));

        assertThat(refreshTokens.findAll())
                .extracting(RefreshToken::getTokenHash)
                .contains(sha256(token))
                .doesNotContain(token);
    }

    @Test
    void 쿠키가_없으면_401() throws Exception {
        mvc.perform(post("/api/auth/refresh"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("INVALID_REFRESH_TOKEN"));
    }

    @Test
    void 갱신에_실패하면_쿠키를_지운다() throws Exception {
        String setCookie = refresh("no-such-token")
                .andExpect(status().isUnauthorized())
                .andReturn().getResponse().getHeader(HttpHeaders.SET_COOKIE);

        assertThat(setCookie).startsWith(COOKIE + "=;").contains("Max-Age=0", "Path=/api/auth");
    }

    @Test
    void 교체_직후_같은_토큰이_오면_동시_갱신으로_보고_409_만_준다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);
        String first = refreshCookie(login("viewer-1"));
        String second = refreshCookie(refresh(first).andExpect(status().isOk()));

        // 같은 쿠키를 든 다른 탭의 요청이 늦게 도착했다
        MockHttpServletResponse late = refresh(first)
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REFRESH_TOKEN_ROTATED"))
                .andReturn().getResponse();

        // 먼저 끝난 요청이 넣은 새 쿠키를 지우지 않고, 새 토큰도 폐기하지 않는다
        assertThat(late.getHeader(HttpHeaders.SET_COOKIE)).isNull();
        refresh(second).andExpect(status().isOk());
    }

    @Test
    void 오래된_토큰_재사용은_해당_계보만_폐기한다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);
        String first = refreshCookie(login("viewer-1"));
        String other = refreshCookie(login("viewer-1"));
        String next = refreshCookie(refresh(first).andExpect(status().isOk()));
        RefreshToken old = refreshTokens.findByTokenHash(sha256(first)).orElseThrow();
        RefreshToken successor = refreshTokens.findByTokenHash(sha256(next)).orElseThrow();
        RefreshToken separate = refreshTokens.findByTokenHash(sha256(other)).orElseThrow();
        assertThat(successor.getFamilyId()).isEqualTo(old.getFamilyId()).isNotEqualTo(separate.getFamilyId());
        // 유예 경과를 DB에서 재현한다.
        refreshTokens.revokeIfActive(successor.getId(), Instant.now().minusSeconds(60));
        String latest = refreshCookie(login("viewer-1"));
        // 교체 계보의 활성 토큰을 직접 준비한다.
        String familyToken = "family-token-for-test";
        Instant now = Instant.now();
        refreshTokens.saveAndFlush(new RefreshToken(UUID.randomUUID(), old.getUserId(), old.getFamilyId(),
                sha256(familyToken), now, now.plusSeconds(3600)));
        refresh(next).andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("INVALID_REFRESH_TOKEN"));
        refresh(familyToken).andExpect(status().isUnauthorized());
        refresh(other).andExpect(status().isOk());
        refresh(latest).andExpect(status().isOk());
    }

    @Test
    void 다른_로그인_계보는_로그아웃_토큰의_유예_근거가_되지_않는다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);
        String token = refreshCookie(login("viewer-1"));
        mvc.perform(post("/api/auth/logout").cookie(new Cookie(COOKIE, token))).andExpect(status().isNoContent());
        String other = refreshCookie(login("viewer-1"));
        refresh(token).andExpect(status().isUnauthorized());
        refresh(other).andExpect(status().isOk());
    }

    @Test
    void 로그아웃한_토큰은_바로_다시_와도_409_가_아니라_401() throws Exception {
        saveUser("viewer-1", Role.VIEWER);
        String token = refreshCookie(login("viewer-1"));
        mvc.perform(post("/api/auth/logout").cookie(new Cookie(COOKIE, token))).andExpect(status().isNoContent());

        refresh(token)
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("INVALID_REFRESH_TOKEN"));
    }

    @Test
    void 만료된_갱신_토큰은_401() throws Exception {
        AppUser user = saveUser("viewer-1", Role.VIEWER);
        String raw = "expired-refresh-token-for-test";
        Instant past = Instant.now().minusSeconds(60);
        refreshTokens.saveAndFlush(new RefreshToken(
                UUID.randomUUID(), user.getId(), UUID.randomUUID(), sha256(raw), past.minusSeconds(3600), past));

        refresh(raw).andExpect(status().isUnauthorized());
    }

    @Test
    void 없는_갱신_토큰은_401() throws Exception {
        refresh("no-such-token").andExpect(status().isUnauthorized());
    }

    @Test
    void 막힌_계정은_갱신할_수_없다() throws Exception {
        AppUser user = saveUser("viewer-1", Role.VIEWER);
        String token = refreshCookie(login("viewer-1"));
        AppUser reloaded = users.findById(user.getId()).orElseThrow();
        reloaded.disable(Instant.now());
        users.saveAndFlush(reloaded);

        refresh(token).andExpect(status().isUnauthorized());
    }

    @Test
    void 로그아웃하면_토큰을_폐기하고_쿠키를_지운다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);
        String token = refreshCookie(login("viewer-1"));

        String setCookie = mvc.perform(post("/api/auth/logout").cookie(new Cookie(COOKIE, token)))
                .andExpect(status().isNoContent())
                .andReturn().getResponse().getHeader(HttpHeaders.SET_COOKIE);

        assertThat(setCookie).startsWith(COOKIE + "=;").contains("Max-Age=0", "Path=/api/auth");
        refresh(token).andExpect(status().isUnauthorized());
    }

    @Test
    void 쿠키_없이_로그아웃해도_204() throws Exception {
        mvc.perform(post("/api/auth/logout")).andExpect(status().isNoContent());
    }

    private AppUser saveUser(String loginId, Role role) {
        return users.saveAndFlush(new AppUser(loginId, passwordEncoder.encode(PASSWORD), role, null, Instant.now()));
    }

    private ResultActions login(String loginId) throws Exception {
        return mvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + loginId + "\",\"password\":\"" + PASSWORD + "\"}"))
                .andExpect(status().isOk());
    }

    private ResultActions refresh(String token) throws Exception {
        return mvc.perform(post("/api/auth/refresh").cookie(new Cookie(COOKIE, token)));
    }

    private static String refreshCookie(ResultActions result) {
        Cookie cookie = result.andReturn().getResponse().getCookie(COOKIE);
        assertThat(cookie).as("Set-Cookie " + COOKIE).isNotNull();
        return cookie.getValue();
    }

    private static String sha256(String value) throws Exception {
        return HexFormat.of().formatHex(
                MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
    }
}
