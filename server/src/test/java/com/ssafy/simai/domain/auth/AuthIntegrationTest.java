package com.ssafy.simai.domain.auth;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.RequestBuilder;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
@Import(AuthIntegrationTest.RoleProbeController.class)
class AuthIntegrationTest {

    private static final String PASSWORD = "correct-horse";

    @Autowired
    MockMvc mvc;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwordEncoder;

    @Autowired
    JwtEncoder jwtEncoder;

    @Test
    void 맞는_비밀번호면_JWT_와_계정_역할을_준다() throws Exception {
        AppUser user = saveUser("admin-1", Role.ADMIN);

        mvc.perform(login("admin-1", PASSWORD))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.accessToken").isNotEmpty())
                .andExpect(jsonPath("$.tokenType").value("Bearer"))
                .andExpect(jsonPath("$.expiresIn").value(30 * 60))
                .andExpect(jsonPath("$.refreshToken").doesNotExist())
                .andExpect(jsonPath("$.user.id").value(String.valueOf(user.getId())))
                .andExpect(jsonPath("$.user.role").value("ADMIN"));
    }

    @Test
    void 비밀번호가_틀리면_401_과_문구를_준다() throws Exception {
        saveUser("admin-1", Role.ADMIN);

        mvc.perform(login("admin-1", "wrong"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.detail").value("아이디 또는 비밀번호가 맞지 않습니다"))
                .andExpect(jsonPath("$.code").value("LOGIN_FAILED"));
    }

    @Test
    void 없는_아이디도_같은_401_을_준다() throws Exception {
        mvc.perform(login("nobody", PASSWORD))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.detail").value("아이디 또는 비밀번호가 맞지 않습니다"));
    }

    @Test
    void 막힌_계정은_비밀번호가_맞아도_401() throws Exception {
        AppUser user = saveUser("viewer-1", Role.VIEWER);
        user.disable(Instant.now());
        users.flush();

        mvc.perform(login("viewer-1", PASSWORD)).andExpect(status().isUnauthorized());
    }

    @Test
    void 읽을_수_없는_본문도_code_를_싣는다() throws Exception {
        // Spring MVC 표준 오류도 관제 앱이 code 로 가를 수 있게 HTTP 상태 이름을 싣는다(S15P31A101-632)
        mvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{not json"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("BAD_REQUEST"));
    }

    @Test
    void 아이디나_비밀번호가_비면_400() throws Exception {
        mvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\" \",\"password\":\"\"}"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void 비밀번호가_72바이트를_넘으면_500_이_아니라_401() throws Exception {
        saveUser("admin-1", Role.ADMIN);

        mvc.perform(login("admin-1", "가".repeat(30)))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("LOGIN_FAILED"));
    }

    @Test
    void 토큰이_없으면_보호된_API_는_401_과_본문을_준다() throws Exception {
        mvc.perform(get("/test/viewer"))
                .andExpect(status().isUnauthorized())
                .andExpect(header().exists(HttpHeaders.WWW_AUTHENTICATE))
                .andExpect(jsonPath("$.code").value("UNAUTHORIZED"))
                .andExpect(jsonPath("$.detail").value("로그인이 필요합니다"));
    }

    @Test
    void 역할이_모자라면_403_과_본문을_준다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);

        mvc.perform(get("/test/admin").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("viewer-1"))))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value("FORBIDDEN"))
                .andExpect(jsonPath("$.detail").value("권한이 없습니다"));
    }

    @Test
    void 로그인한_토큰으로_열람_API_를_부른다() throws Exception {
        saveUser("viewer-1", Role.VIEWER);

        mvc.perform(get("/test/viewer").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("viewer-1"))))
                .andExpect(status().isOk());
    }

    @Test
    void 관리자_API_는_관리자만_부른다() throws Exception {
        saveUser("admin-1", Role.ADMIN);
        saveUser("foreman-1", Role.FOREMAN);
        saveUser("viewer-1", Role.VIEWER);

        mvc.perform(get("/test/admin").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("admin-1"))))
                .andExpect(status().isOk());
        mvc.perform(get("/test/admin").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("foreman-1"))))
                .andExpect(status().isForbidden());
        mvc.perform(get("/test/admin").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("viewer-1"))))
                .andExpect(status().isForbidden());
    }

    @Test
    void 반장_API_는_반장과_관리자만_부른다() throws Exception {
        saveUser("admin-1", Role.ADMIN);
        saveUser("foreman-1", Role.FOREMAN);
        saveUser("viewer-1", Role.VIEWER);

        mvc.perform(get("/test/foreman").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("admin-1"))))
                .andExpect(status().isOk());
        mvc.perform(get("/test/foreman").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("foreman-1"))))
                .andExpect(status().isOk());
        mvc.perform(get("/test/foreman").header(HttpHeaders.AUTHORIZATION, bearer(loginToken("viewer-1"))))
                .andExpect(status().isForbidden());
    }

    @Test
    void 만료된_토큰은_401() throws Exception {
        Instant past = Instant.now().minusSeconds(3600);
        String token = encode(JwtClaimsSet.builder()
                .issuer("simai")
                .subject("1")
                .issuedAt(past.minusSeconds(60))
                .expiresAt(past)
                .claim("role", "ADMIN")
                .build());

        mvc.perform(get("/test/admin").header(HttpHeaders.AUTHORIZATION, bearer(token)))
                .andExpect(status().isUnauthorized())
                .andExpect(header().exists(HttpHeaders.WWW_AUTHENTICATE))
                .andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    }

    @Test
    void 다른_발급자의_토큰은_401() throws Exception {
        String token = encode(JwtClaimsSet.builder()
                .issuer("someone-else")
                .subject("1")
                .issuedAt(Instant.now())
                .expiresAt(Instant.now().plusSeconds(600))
                .claim("role", "ADMIN")
                .build());

        mvc.perform(get("/test/admin").header(HttpHeaders.AUTHORIZATION, bearer(token)))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void 서명을_바꾼_토큰은_401() throws Exception {
        saveUser("viewer-1", Role.VIEWER);
        String token = loginToken("viewer-1");
        String tampered = token.substring(0, token.length() - 2) + (token.endsWith("AA") ? "BB" : "AA");

        mvc.perform(get("/test/viewer").header(HttpHeaders.AUTHORIZATION, bearer(tampered)))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void 비밀번호는_해시로만_저장한다() {
        AppUser user = saveUser("admin-1", Role.ADMIN);

        assertThat(user.getPasswordHash()).startsWith("{bcrypt}").doesNotContain(PASSWORD);
    }

    private AppUser saveUser(String loginId, Role role) {
        return users.saveAndFlush(new AppUser(loginId, passwordEncoder.encode(PASSWORD), role, null, Instant.now()));
    }

    private String loginToken(String loginId) throws Exception {
        String body = mvc.perform(login(loginId, PASSWORD))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString();
        return JsonPath.read(body, "$.accessToken");
    }

    private String encode(JwtClaimsSet claims) {
        JwsHeader header = JwsHeader.with(MacAlgorithm.HS256).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(header, claims)).getTokenValue();
    }

    private static String bearer(String token) {
        return "Bearer " + token;
    }

    private static RequestBuilder login(String username, String password) {
        return post("/api/auth/login")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"username\":\"" + username + "\",\"password\":\"" + password + "\"}");
    }

    // 실제 API 가 아직 없어 명세 12.6 권한 세 가지(열람·반장·관리자)를 흉내 내는 시험용 경로
    @RestController
    static class RoleProbeController {

        @GetMapping("/test/viewer")
        String viewer() {
            return "ok";
        }

        @GetMapping("/test/foreman")
        @PreAuthorize("hasAnyRole('FOREMAN', 'ADMIN')")
        String foreman() {
            return "ok";
        }

        @GetMapping("/test/admin")
        @PreAuthorize("hasRole('ADMIN')")
        String admin() {
            return "ok";
        }
    }
}
