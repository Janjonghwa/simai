package com.ssafy.simai.global.command;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import jakarta.servlet.http.Cookie;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class AdminCommandsTest {

    @Autowired
    AdminCommands commands;

    @Autowired
    MockMvc mvc;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwordEncoder;

    private final ByteArrayOutputStream out = new ByteArrayOutputStream();
    private final ByteArrayOutputStream err = new ByteArrayOutputStream();

    @Test
    void create_user_는_해시한_비밀번호로_계정을_만든다() {
        int code = run(List.of("create-user", "--login", "admin-1", "--role", "ADMIN", "--label", "관리자"),
                "long-password", "long-password");

        assertThat(code).isEqualTo(AdminCommand.OK);
        AppUser user = users.findByLoginId("admin-1").orElseThrow();
        assertThat(user.getRole()).isEqualTo(Role.ADMIN);
        assertThat(user.getDisplayLabel()).isEqualTo("관리자");
        assertThat(user.getPasswordHash()).startsWith("{bcrypt}");
        assertThat(passwordEncoder.matches("long-password", user.getPasswordHash())).isTrue();
        assertThat(text(out)).contains("login=admin-1").doesNotContain("long-password");
    }

    @Test
    void 명령으로_만든_계정으로_로그인된다() throws Exception {
        run(List.of("create-user", "--login", "foreman-1", "--role", "FOREMAN"), "long-password", "long-password");

        login("foreman-1", "long-password")
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.accessToken").isNotEmpty())
                .andExpect(jsonPath("$.user.role").value("FOREMAN"));
    }

    @Test
    void disable_user_는_로그인과_갱신을_막는다() throws Exception {
        run(List.of("create-user", "--login", "viewer-1", "--role", "VIEWER"), "long-password", "long-password");
        Cookie refresh = login("viewer-1", "long-password").andReturn().getResponse().getCookie("simai_refresh");

        int code = run(List.of("disable-user", "--login", "viewer-1"));

        assertThat(code).isEqualTo(AdminCommand.OK);
        assertThat(text(out)).contains("login=viewer-1", "폐기한 갱신 토큰 1개");
        assertThat(users.findByLoginId("viewer-1").orElseThrow().isDisabled()).isTrue();
        login("viewer-1", "long-password").andExpect(status().isUnauthorized());
        mvc.perform(post("/api/auth/refresh").cookie(refresh)).andExpect(status().isUnauthorized());
    }

    @Test
    void set_password는_비밀번호를_바꾸고_모든_갱신_토큰을_폐기한다() throws Exception {
        run(List.of("create-user", "--login", "viewer-1", "--role", "VIEWER"), "long-password", "long-password");
        Cookie first = login("viewer-1", "long-password").andReturn().getResponse().getCookie("simai_refresh");
        Cookie second = login("viewer-1", "long-password").andReturn().getResponse().getCookie("simai_refresh");
        assertThat(run(List.of("set-password", "--login", "viewer-1"), "new-password", "new-password"))
                .isEqualTo(AdminCommand.OK);
        login("viewer-1", "long-password").andExpect(status().isUnauthorized());
        login("viewer-1", "new-password").andExpect(status().isOk());
        mvc.perform(post("/api/auth/refresh").cookie(first)).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/auth/refresh").cookie(second)).andExpect(status().isUnauthorized());
        assertThat(text(out)).doesNotContain("new-password");
    }

    @Test
    void set_password의_확인과_비밀번호_규칙을_검사한다() {
        run(List.of("create-user", "--login", "viewer-1", "--role", "VIEWER"), "long-password", "long-password");
        String before = users.findByLoginId("viewer-1").orElseThrow().getPasswordHash();
        assertThat(run(List.of("set-password", "--login", "viewer-1"), "new-password", "different"))
                .isEqualTo(AdminCommand.FAILED);
        assertThat(run(List.of("set-password", "--login", "viewer-1"), "short", "short"))
                .isEqualTo(AdminCommand.FAILED);
        String longPassword = "가".repeat(25);
        assertThat(run(List.of("set-password", "--login", "viewer-1"), longPassword, longPassword))
                .isEqualTo(AdminCommand.FAILED);
        assertThat(run(List.of("set-password", "--login", "missing"), "new-password", "new-password"))
                .isEqualTo(AdminCommand.FAILED);
        assertThat(run(List.of("set-password", "--login", "viewer-1", "--password", "secret")))
                .isEqualTo(AdminCommand.USAGE);
        assertThat(users.findByLoginId("viewer-1").orElseThrow().getPasswordHash()).isEqualTo(before);
    }

    @Test
    void 이미_막힌_계정을_다시_막아도_처음_시각을_둔다() {
        run(List.of("create-user", "--login", "viewer-1", "--role", "VIEWER"), "long-password", "long-password");
        run(List.of("disable-user", "--login", "viewer-1"));
        var first = users.findByLoginId("viewer-1").orElseThrow().getDisabledAt();

        int code = run(List.of("disable-user", "--login", "viewer-1"));

        assertThat(code).isEqualTo(AdminCommand.OK);
        assertThat(users.findByLoginId("viewer-1").orElseThrow().getDisabledAt()).isEqualTo(first);
    }

    @Test
    void 없는_아이디는_막을_수_없다() {
        int code = run(List.of("disable-user", "--login", "nobody"));

        assertThat(code).isEqualTo(AdminCommand.FAILED);
        assertThat(text(err)).contains("없는 아이디");
    }

    @Test
    void 옵션은_등호로도_받는다() {
        int code = run(List.of("create-user", "--login=foreman-1", "--role=FOREMAN"), "long-password", "long-password");

        assertThat(code).isEqualTo(AdminCommand.OK);
        assertThat(users.findByLoginId("foreman-1")).isPresent();
    }

    @Test
    void 비밀번호_확인이_다르면_만들지_않는다() {
        int code = run(List.of("create-user", "--login", "admin-1", "--role", "ADMIN"), "long-password", "other-password");

        assertThat(code).isEqualTo(AdminCommand.FAILED);
        assertThat(users.findByLoginId("admin-1")).isEmpty();
    }

    @Test
    void 짧은_비밀번호는_거부한다() {
        int code = run(List.of("create-user", "--login", "admin-1", "--role", "ADMIN"), "short", "short");

        assertThat(code).isEqualTo(AdminCommand.FAILED);
        assertThat(text(err)).contains("8자 이상");
        assertThat(users.findByLoginId("admin-1")).isEmpty();
    }

    @Test
    void 비밀번호가_72바이트를_넘으면_사용법이_아니라_처리_실패로_끝낸다() {
        String longPassword = "가".repeat(25);

        int code = run(List.of("create-user", "--login", "admin-1", "--role", "ADMIN"), longPassword, longPassword);

        assertThat(code).isEqualTo(AdminCommand.FAILED);
        assertThat(text(err)).contains("72바이트").doesNotContain("사용법");
        assertThat(users.findByLoginId("admin-1")).isEmpty();
    }

    @Test
    void 같은_아이디가_있으면_실패하고_덮어쓰지_않는다() {
        run(List.of("create-user", "--login", "admin-1", "--role", "ADMIN"), "long-password", "long-password");
        String before = users.findByLoginId("admin-1").orElseThrow().getPasswordHash();

        int code = run(List.of("create-user", "--login", "admin-1", "--role", "VIEWER"), "another-pass", "another-pass");

        assertThat(code).isEqualTo(AdminCommand.FAILED);
        assertThat(text(err)).contains("이미 있는 아이디");
        AppUser user = users.findByLoginId("admin-1").orElseThrow();
        assertThat(user.getRole()).isEqualTo(Role.ADMIN);
        assertThat(user.getPasswordHash()).isEqualTo(before);
    }

    @Test
    void 역할이_틀리면_사용법을_보여준다() {
        int code = run(List.of("create-user", "--login", "x", "--role", "DEVICE"));

        assertThat(code).isEqualTo(AdminCommand.USAGE);
        assertThat(text(err)).contains("ADMIN·FOREMAN·VIEWER");
    }

    @Test
    void 필수_옵션이_없으면_사용법을_보여준다() {
        int code = run(List.of("create-user", "--role", "ADMIN"));

        assertThat(code).isEqualTo(AdminCommand.USAGE);
        assertThat(text(err)).contains("--login");
    }

    @Test
    void 비밀번호를_인자로_주면_거부한다() {
        int code = run(List.of("create-user", "--login", "x", "--role", "ADMIN", "--password", "secret123"));

        assertThat(code).isEqualTo(AdminCommand.USAGE);
        assertThat(users.findByLoginId("x")).isEmpty();
    }

    @Test
    void 모르는_명령이면_사용법을_보여준다() {
        assertThat(run(List.of("drop-everything"))).isEqualTo(AdminCommand.USAGE);
        assertThat(run(List.of())).isEqualTo(AdminCommand.USAGE);
        assertThat(text(err)).contains("create-user", "set-password");
    }

    private int run(List<String> args, String... secrets) {
        Deque<String> inputs = new ArrayDeque<>(List.of(secrets));
        CommandIo io = new CommandIo(print(out), print(err), prompt -> inputs.poll());
        return commands.execute(args, io);
    }

    private ResultActions login(String loginId, String password) throws Exception {
        return mvc.perform(post("/api/auth/login")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"username\":\"" + loginId + "\",\"password\":\"" + password + "\"}"));
    }

    private static PrintStream print(ByteArrayOutputStream stream) {
        return new PrintStream(stream, true, StandardCharsets.UTF_8);
    }

    private static String text(ByteArrayOutputStream stream) {
        return stream.toString(StandardCharsets.UTF_8);
    }
}
