package com.ssafy.simai.domain.machine;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** 기기·워커 토큰 발급·회전·폐기와 경로별 인증(S15P31A101-222). 토큰은 관리 명령으로 받는다(API 없음). */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
@Import(MachineTokenIntegrationTest.ProbeController.class)
class MachineTokenIntegrationTest {

    private static final Pattern TOKEN = Pattern.compile("토큰\\(이번 한 번만 표시\\): (\\S+)");

    @Autowired
    MockMvc mvc;

    @Autowired
    AdminCommands commands;

    @Autowired
    JdbcTemplate jdbc;

    private final String deviceId = "laptop-" + UUID.randomUUID().toString().substring(0, 8);
    private final String workerId = "worker-" + UUID.randomUUID().toString().substring(0, 8);

    @Test
    void 기기_토큰은_기기_경로만_통과하고_DB_에는_해시만_남는다() throws Exception {
        String token = issued("device", "register", "--id", deviceId, "--kind", "LAPTOP");

        mvc.perform(get("/api/devices/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isOk()).andExpect(content().string(deviceId));
        mvc.perform(get("/api/endpoints/tier0").header(HttpHeaders.AUTHORIZATION, "bearer " + token))
                .andExpect(status().isOk());
        // 다른 종류 경로·사용자 API 에서는 쓸 수 없다
        mvc.perform(get("/api/worker/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/api/events").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/api/devices/probe")).andExpect(status().isUnauthorized());

        String stored = jdbc.queryForObject("SELECT token_hash FROM device WHERE device_id = ?", String.class, deviceId);
        assertThat(stored).hasSize(64).doesNotContain(token);
    }

    @Test
    void 회전하면_옛_토큰은_바로_거부되고_폐기하면_새_토큰도_거부된다() throws Exception {
        String first = issued("device", "register", "--id", deviceId, "--kind", "LAPTOP");
        String second = issued("device", "rotate", "--id", deviceId);

        mvc.perform(get("/api/devices/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + first))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/api/devices/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + second))
                .andExpect(status().isOk());

        assertThat(run("device", "revoke", "--id", deviceId)).isZero();
        mvc.perform(get("/api/devices/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + second))
                .andExpect(status().isUnauthorized());

        // 폐기 뒤 회전하면 다시 쓸 수 있다
        String third = issued("device", "rotate", "--id", deviceId);
        mvc.perform(get("/api/devices/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + third))
                .andExpect(status().isOk());
    }

    @Test
    void 워커_토큰은_워커_경로만_통과하고_폐기하면_거부된다() throws Exception {
        String token = issued("worker", "register", "--id", workerId, "--job-types", "vlm_rationale,tbm_report");

        mvc.perform(get("/api/worker/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isOk()).andExpect(content().string(workerId));
        mvc.perform(get("/api/devices/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isUnauthorized());
        assertThat(jdbc.queryForObject("SELECT array_to_string(job_types, ',') FROM worker WHERE worker_id = ?",
                String.class, workerId)).isEqualTo("vlm_rationale,tbm_report");

        run("worker", "revoke", "--id", workerId);
        mvc.perform(get("/api/worker/probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void 잘못된_명령은_실패하고_토큰을_내지_않는다() {
        issued("device", "register", "--id", deviceId, "--kind", "LAPTOP");
        assertThat(run("device", "register", "--id", deviceId, "--kind", "LAPTOP")).isNotZero();
        assertThat(run("device", "register", "--id", "Bad_ID", "--kind", "LAPTOP")).isNotZero();
        assertThat(run("device", "register", "--id", "laptop-x", "--kind", "PHONE")).isNotZero();
        assertThat(run("worker", "register", "--id", workerId, "--job-types", "unknown_job")).isNotZero();
        assertThat(run("device", "rotate", "--id", "no-such-device")).isNotZero();
        assertThat(run("device")).isNotZero();
    }

    // ---- 도우미

    private String issued(String... args) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int code = commands.execute(List.of(args), io(out));
        assertThat(code).isZero();
        Matcher matcher = TOKEN.matcher(out.toString(StandardCharsets.UTF_8));
        assertThat(matcher.find()).isTrue();
        return matcher.group(1);
    }

    private int run(String... args) {
        return commands.execute(List.of(args), io(new ByteArrayOutputStream()));
    }

    private static CommandIo io(ByteArrayOutputStream out) {
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        return new CommandIo(print, print, prompt -> null);
    }

    @RestController
    static class ProbeController {

        @GetMapping({"/api/devices/probe", "/api/endpoints/tier0", "/api/worker/probe"})
        String whoAmI(@AuthenticationPrincipal String id) {
            return id;
        }
    }
}
