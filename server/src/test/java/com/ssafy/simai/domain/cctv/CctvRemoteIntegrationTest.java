package com.ssafy.simai.domain.cctv;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.BinaryMessage;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * S15P31A101-400 CCTV 원격 시청 통합 시험(가짜 게이트웨이 → 서버 → 시청 WebSocket):
 * <ul>
 * <li>수신 /ws/devices/cctv 기기 토큰(LAPTOP 만)·시청 /ws/cctv/view 입장권(관리자만, 한 번만)</li>
 * <li>수요: 접속 직후 false, 시청자 0 → 1 이면 true, 마지막 시청자가 나가고 유예 뒤 false</li>
 * <li>시청자: status → hello → codec → 카메라별 다음 키프레임부터 이진 프레임(받은 그대로), 계약 cctv_preview·cctv_remote_control</li>
 * <li>동시 시청 상한(시험 2명) viewer_limit, 월 송출 상한 monthly_quota·수요 끄기, 사용 기록 추가만</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class CctvRemoteIntegrationTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MachineTokenService machineTokens;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    CctvRemoteHub hub;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final List<Client> clients = new ArrayList<>();
    private String laptopId;
    private String laptopToken;
    private String watchId;
    private String watchToken;

    @BeforeEach
    void setUp() {
        hub.resetForTest();
        jdbc.update("DELETE FROM cctv_remote_usage");
        laptopId = "laptop-cctv-" + tag;
        laptopToken = machineTokens.registerDevice(laptopId, "LAPTOP").token();
        watchId = "watch-cctv-" + tag;
        watchToken = machineTokens.registerDevice(watchId, "WATCH").token();
    }

    @AfterEach
    void tearDown() {
        for (Client c : clients) {
            c.close();
        }
        hub.resetForTest();
        jdbc.update("DELETE FROM cctv_remote_usage");
        jdbc.update("DELETE FROM device WHERE device_id IN (?, ?)", laptopId, watchId);
    }

    // ------------------------------------------------------------------ 인증

    @Test
    void 수신은_현장_노트북_기기_토큰만_받는다() throws Exception {
        assertThatThrownBy(() -> source(null)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> source("wrong-token")).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> source(watchToken)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> source(jwt("ADMIN"))).isInstanceOf(ExecutionException.class);
        Client gw = source(laptopToken);
        assertThat(gw.session.isOpen()).isTrue();
    }

    @Test
    void 입장권은_관리자만_받고_한_번만_쓴다() throws Exception {
        assertThat(postTicket(jwt("VIEWER")).statusCode()).isEqualTo(403);
        assertThat(postTicket(jwt("FOREMAN")).statusCode()).isEqualTo(403);
        assertThat(postTicket(null).statusCode()).isEqualTo(401);

        HttpResponse<String> issued = postTicket(jwt("ADMIN"));
        assertThat(issued.statusCode()).isEqualTo(201);
        JsonNode body = mapper.readTree(issued.body());
        assertThat(body.path("path").asString()).isEqualTo(CctvPaths.VIEW_WS);
        assertThat(body.path("expires_at_ms").longValue()).isGreaterThan(Instant.now().toEpochMilli());
        String ticket = body.path("ticket").asString();

        assertThatThrownBy(() -> viewer(null)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> viewer("not-a-ticket")).isInstanceOf(ExecutionException.class);
        Client v = viewer(ticket);
        assertThat(v.session.isOpen()).isTrue();
        assertThatThrownBy(() -> viewer(ticket)).isInstanceOf(ExecutionException.class);
    }

    // ------------------------------------------------------------------ 수요·형식

    @Test
    void 시청자가_있을_때만_수요를_켜고_키프레임부터_그대로_나눠_준다() throws Exception {
        Client gw = source(laptopToken);
        assertDemand(gw.next(), false);

        Client v1 = viewer(newTicket());
        assertStatus(v1.next(), "waiting_source");
        assertDemand(gw.next(), true);

        gw.send(new TextMessage(example("cctv_preview")));
        String codec = example("cctv_preview.codec");
        gw.send(new TextMessage(codec));
        byte[] delta = frame("cam1", 1, false, new byte[] {0, 0, 0, 1, 0x41, 1, 2});
        byte[] key = frame("cam1", 2, true, new byte[] {0, 0, 0, 1, 0x65, 3, 4, 5});
        byte[] delta2 = frame("cam1", 3, false, new byte[] {0, 0, 0, 1, 0x41, 6});
        byte[] unknownCamera = frame("camX", 4, true, new byte[] {0, 0, 0, 1, 0x65});
        gw.send(new BinaryMessage(delta));
        gw.send(new BinaryMessage(key));
        gw.send(new BinaryMessage(unknownCamera));
        gw.send(new BinaryMessage(delta2));

        assertStatus(v1.next(), "live");
        JsonNode hello = text(v1.next());
        assertThat(hello.path("kind").asString()).isEqualTo("hello");
        assertThat(mapper.readTree(text(v1.next()).toString())).isEqualTo(mapper.readTree(codec));
        assertThat(binary(v1.next())).isEqualTo(key);  // 키프레임 앞 델타는 넘기지 않는다, 받은 그대로
        assertThat(binary(v1.next())).isEqualTo(delta2);  // 모르는 카메라는 버린다

        // 늦게 온 시청자는 마지막 hello·codec 을 먼저 받고 다음 키프레임부터
        Client v2 = viewer(newTicket());
        assertStatus(v2.next(), "live");
        assertThat(text(v2.next()).path("kind").asString()).isEqualTo("hello");
        assertThat(text(v2.next()).path("kind").asString()).isEqualTo("codec");
        byte[] delta3 = frame("cam1", 5, false, new byte[] {0, 0, 0, 1, 0x41, 7});
        byte[] key2 = frame("cam1", 6, true, new byte[] {0, 0, 0, 1, 0x65, 8});
        gw.send(new BinaryMessage(delta3));
        gw.send(new BinaryMessage(key2));
        assertThat(binary(v2.next())).isEqualTo(key2);
        assertThat(binary(v1.next())).isEqualTo(delta3);
        assertThat(binary(v1.next())).isEqualTo(key2);
        assertThat(hub.status().viewers()).isEqualTo(2);

        // 시청자가 모두 나가도 유예(시험 400 ms) 동안은 수요를 둔다
        v1.close();
        v2.close();
        assertThat(gw.poll(200)).isNull();
        assertDemand(gw.next(), false);
        assertThat(hub.status().demand()).isFalse();
    }

    @Test
    void 게이트웨이가_끊기면_waiting_source_를_보낸다() throws Exception {
        Client gw = source(laptopToken);
        assertDemand(gw.next(), false);
        Client v = viewer(newTicket());
        assertStatus(v.next(), "waiting_source");
        gw.send(new TextMessage(example("cctv_preview")));
        assertStatus(v.next(), "live");
        assertThat(text(v.next()).path("kind").asString()).isEqualTo("hello");
        gw.close();
        assertStatus(v.next(), "waiting_source");
        assertThat(v.session.isOpen()).isTrue();

        Client gw2 = source(laptopToken);
        assertDemand(gw2.next(), true);  // 접속 직후 지금 수요
    }

    @Test
    void 동시_시청_상한을_넘으면_viewer_limit_후_닫는다() throws Exception {
        Client v1 = viewer(newTicket());
        Client v2 = viewer(newTicket());
        assertStatus(v1.next(), "waiting_source");
        assertStatus(v2.next(), "waiting_source");
        Client v3 = viewer(newTicket());
        assertStatus(v3.next(), "viewer_limit");
        assertThat(v3.closed.poll(5, TimeUnit.SECONDS)).isNotNull();
        assertThat(hub.status().viewers()).isEqualTo(2);
    }

    @Test
    void 월_송출_상한에_닿으면_모두_닫고_수요를_끄고_사용_기록을_추가만_한다() throws Exception {
        Client gw = source(laptopToken);
        assertDemand(gw.next(), false);
        Client v = viewer(newTicket());
        assertStatus(v.next(), "waiting_source");
        assertDemand(gw.next(), true);
        gw.send(new TextMessage(example("cctv_preview")));
        assertStatus(v.next(), "live");
        v.next();

        hub.quotaBytesForTest(2_000);
        byte[] key = frame("cam1", 1, true, new byte[3_000]);
        gw.send(new BinaryMessage(key));
        assertThat(binary(v.next())).isEqualTo(key);
        JsonNode quota = text(v.next());
        assertThat(quota.path("state").asString()).isEqualTo("monthly_quota");
        assertThat(quota.path("monthly_sent_bytes").longValue()).isGreaterThanOrEqualTo(2_000);
        assertThat(v.closed.poll(5, TimeUnit.SECONDS)).isNotNull();
        assertDemand(gw.next(), false);
        assertThat(hub.status().blocked()).isTrue();

        Client late = viewer(newTicket());
        assertStatus(late.next(), "monthly_quota");
        assertThat(late.closed.poll(5, TimeUnit.SECONDS)).isNotNull();

        hub.flushUsage();
        hub.flushUsage();  // 보낸 것이 없으면 줄을 더하지 않는다
        List<Long> rows = jdbc.queryForList("SELECT sent_bytes FROM cctv_remote_usage", Long.class);
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst()).isGreaterThanOrEqualTo(3_000);

        HttpResponse<String> status = HttpClient.newHttpClient().send(HttpRequest.newBuilder(
                        URI.create("http://localhost:" + port + CctvPaths.STATUS_API))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("ADMIN")).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(status.statusCode()).isEqualTo(200);
        JsonNode s = mapper.readTree(status.body());
        assertThat(s.path("blocked").booleanValue()).isTrue();
        assertThat(s.path("monthly_sent_bytes").longValue()).isEqualTo(rows.getFirst());
        assertThat(s.path("monthly_quota_bytes").longValue()).isEqualTo(2_000);
    }

    // ------------------------------------------------------------------ 도움

    private void assertDemand(WebSocketMessage<?> message, boolean active) {
        JsonNode doc = text(message);
        assertThat(schemas.violations("messages/cctv_remote_control", doc.toString())).isEmpty();
        assertThat(doc.path("active").booleanValue()).isEqualTo(active);
    }

    private void assertStatus(WebSocketMessage<?> message, String state) {
        JsonNode doc = text(message);
        assertThat(schemas.violations("messages/cctv_preview", doc.toString())).isEmpty();
        assertThat(doc.path("kind").asString()).isEqualTo("status");
        assertThat(doc.path("state").asString()).isEqualTo(state);
    }

    private JsonNode text(WebSocketMessage<?> message) {
        assertThat(message).isInstanceOf(TextMessage.class);
        return mapper.readTree(((TextMessage) message).getPayload());
    }

    private static byte[] binary(WebSocketMessage<?> message) {
        assertThat(message).isInstanceOf(BinaryMessage.class);
        ByteBuffer buf = ((BinaryMessage) message).getPayload();
        byte[] out = new byte[buf.remaining()];
        buf.get(out);
        return out;
    }

    private String example(String name) throws Exception {
        return Files.readString(CONTRACTS.resolve("examples").resolve(name + ".json"));
    }

    private byte[] frame(String cameraId, long sequence, boolean keyframe, byte[] accessUnit) throws Exception {
        String header = mapper.writeValueAsString(java.util.Map.of(
                "camera_id", cameraId, "stream_epoch", 1761609600000L, "frame_sequence", sequence,
                "estimated_capture_time_ms", 1761609600000L + sequence * 40, "timestamp_source", "RTCP_SR",
                "timestamp_uncertainty_ms", 5, "keyframe", keyframe));
        byte[] h = header.getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(ByteBuffer.allocate(4).putInt(h.length).array());
        out.write(h);
        out.write(accessUnit);
        return out.toByteArray();
    }

    private String newTicket() throws Exception {
        HttpResponse<String> response = postTicket(jwt("ADMIN"));
        assertThat(response.statusCode()).isEqualTo(201);
        return mapper.readTree(response.body()).path("ticket").asString();
    }

    private HttpResponse<String> postTicket(String token) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + CctvPaths.TICKETS_API))
                .POST(HttpRequest.BodyPublishers.noBody());
        if (token != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return HttpClient.newHttpClient().send(request.build(), HttpResponse.BodyHandlers.ofString());
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(JwtConfig.ISSUER)
                .subject("1")
                .issuedAt(now)
                .expiresAt(now.plusSeconds(300))
                .claim(AccessTokenIssuer.ROLE_CLAIM, role)
                .build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims))
                .getTokenValue();
    }

    private Client source(String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        if (token != null) {
            headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return connect(URI.create("ws://localhost:" + port + CctvPaths.SOURCE_WS), headers);
    }

    private Client viewer(String ticket) throws Exception {
        String query = ticket == null ? "" : "?ticket=" + ticket;
        return connect(URI.create("ws://localhost:" + port + CctvPaths.VIEW_WS + query), new WebSocketHttpHeaders());
    }

    private Client connect(URI uri, WebSocketHttpHeaders headers) throws Exception {
        Client client = new Client();
        StandardWebSocketClient ws = new StandardWebSocketClient();
        client.session = ws.execute(client.handler, headers, uri).get(5, TimeUnit.SECONDS);
        try {
            client.session.setBinaryMessageSizeLimit(1024 * 1024);
            client.session.setTextMessageSizeLimit(1024 * 1024);
        } catch (IllegalStateException alreadyClosed) {
            // 서버가 상태 한 줄만 보내고 바로 닫는 경우(월 송출 상한 등) 연결 직후 이미 닫혀 있을 수 있다.
            // 받은 메시지와 닫힘은 handler 큐에 그대로 남으므로 시험은 그것을 본다(S15P31A101-466)
        }
        clients.add(client);
        return client;
    }

    private static final class Client {
        final BlockingQueue<WebSocketMessage<?>> received = new LinkedBlockingQueue<>();
        final BlockingQueue<CloseStatus> closed = new LinkedBlockingQueue<>();
        WebSocketSession session;
        final AbstractWebSocketHandler handler = new AbstractWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                received.add(message);
            }

            @Override
            protected void handleBinaryMessage(WebSocketSession s, BinaryMessage message) {
                received.add(message);
            }

            @Override
            public void afterConnectionClosed(WebSocketSession s, CloseStatus status) {
                closed.add(status);
            }
        };

        WebSocketMessage<?> next() throws InterruptedException {
            WebSocketMessage<?> m = received.poll(5, TimeUnit.SECONDS);
            assertThat(m).as("5초 안에 메시지가 와야 한다").isNotNull();
            return m;
        }

        WebSocketMessage<?> poll(long millis) throws InterruptedException {
            return received.poll(millis, TimeUnit.MILLISECONDS);
        }

        void send(WebSocketMessage<?> message) throws Exception {
            synchronized (this) {
                session.sendMessage(message);
            }
        }

        void close() {
            try {
                if (session != null && session.isOpen()) {
                    session.close();
                }
            } catch (Exception e) {
                // 이미 닫힘
            }
        }
    }
}
