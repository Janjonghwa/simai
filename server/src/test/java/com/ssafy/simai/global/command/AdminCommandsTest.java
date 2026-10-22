package com.ssafy.simai.global.command;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest
@ActiveProfiles("test")
@Transactional
class AdminCommandsTest {

    @Autowired
    AdminCommands commands;

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
        assertThat(text(err)).contains("create-user");
    }

    private int run(List<String> args, String... secrets) {
        Deque<String> inputs = new ArrayDeque<>(List.of(secrets));
        CommandIo io = new CommandIo(print(out), print(err), prompt -> inputs.poll());
        return commands.execute(args, io);
    }

    private static PrintStream print(ByteArrayOutputStream stream) {
        return new PrintStream(stream, true, StandardCharsets.UTF_8);
    }

    private static String text(ByteArrayOutputStream stream) {
        return stream.toString(StandardCharsets.UTF_8);
    }
}
