package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 세션 Tier 0 토큰으로 실제 I1(/ws/tier0)에 붙고, 새 등록이 오면 그 연결이 끊기고 옛 토큰으로는 다시 붙지 못한다(S15P31A101-699).
 * 실제 포트·커밋이 필요해 @Transactional 없이 돌고 끝나면 이 시험의 행을 지운다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class GpuSessionI1IntegrationTest {

    private static final Pattern TOKEN = Pattern.compile("토큰\\(이번 한 번만 표시\\): (\\S+)");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    AdminCommands commands;

    @Autowired
    JdbcTemplate jdbc;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String workerId = "worker-gpu-i1-" + suffix;
    private final String bootId = "gpu-boot-i1-" + suffix;
    private final HttpClient http = HttpClient.newHttpClient();

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM gpu_session_event WHERE session_id IN (SELECT session_id FROM gpu_session WHERE boot_id = ?)", bootId);
        jdbc.update("DELETE FROM gpu_session WHERE boot_id = ?", bootId);
        jdbc.update("DELETE FROM gpu_boot_secret WHERE boot_id = ?", bootId);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", workerId);
    }

    @Test
    void 세션_Tier0_토큰으로_I1_에_붙고_새_등록이_오면_끊긴다() throws Exception {
        issued("worker", "register", "--id", workerId, "--job-types", "vlm_rationale");
        String secret = issued("gpu-boot", "register", "--id", bootId, "--worker-id", workerId);

        JsonNode first = register(secret);
        WebSocketSession i1 = connect(first.at("/tier0/token").stringValue());
        assertThat(i1.isOpen()).isTrue();

        register(secret);
        // 커밋 뒤 이벤트로 바로 끊는다(주기 작업은 시험에서 1시간이라 이것만으로 끊겨야 한다)
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        while (i1.isOpen() && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(i1.isOpen()).isFalse();
        assertThatThrownBy(() -> connect(first.at("/tier0/token").stringValue())).isNotNull();
    }

    private JsonNode register(String secret) throws Exception {
        ObjectNode root = MAPPER.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gpu_session_register");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", bootId);
        ObjectNode p = root.putObject("payload");
        p.put("provider", "MANUAL");
        p.put("instance_id", "manual-" + suffix);
        p.put("gpu_type", "NVIDIA RTX 4090");
        p.putNull("region");
        p.put("public_host", "198.51.100.9");
        p.set("ports", MAPPER.readTree("[{\"role\":\"CONTROL\",\"public_port\":17000},{\"role\":\"VIDEO\",\"public_port\":17001}]"));
        p.putNull("image_digest");
        p.putNull("price_per_hour_usd");
        HttpResponse<String> response = http.send(HttpRequest.newBuilder(URI.create("http://localhost:" + port + GpuPaths.SESSIONS))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + secret).header(HttpHeaders.CONTENT_TYPE, "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(root))).build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertThat(response.statusCode()).isEqualTo(201);
        return MAPPER.readTree(response.body()).get("payload");
    }

    private WebSocketSession connect(String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        return new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
    }

    private String issued(String... args) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        assertThat(commands.execute(List.of(args), new CommandIo(print, print, prompt -> null))).isZero();
        Matcher matcher = TOKEN.matcher(out.toString(StandardCharsets.UTF_8));
        assertThat(matcher.find()).isTrue();
        return matcher.group(1);
    }
}
