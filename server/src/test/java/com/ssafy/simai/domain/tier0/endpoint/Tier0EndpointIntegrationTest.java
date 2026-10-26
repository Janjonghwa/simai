package com.ssafy.simai.domain.tier0.endpoint;

import static org.assertj.core.api.Assertions.assertThat;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.security.TokenHashes;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
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
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * Tier 0 엔드포인트 등록(I1 tier0_endpoint)과 노트북 조회(GET /api/endpoints/tier0, 기기 토큰)(S15P31A101-290, 명세 3.12·12.6, FR-138).
 * 실제 포트로 Tier 0(I1)·노트북처럼 보내고, 응답이 Tier 0 가 보낸 봉투 그대로인지·계약에 맞는지 본다.
 * 조회는 표 전체에서 최신 하나를 고르므로 시험마다 이 시험이 쓴 행을 앞뒤로 지운다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class Tier0EndpointIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final JsonSchemaFactory SCHEMAS = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012,
            builder -> builder.schemaMappers(m -> m.mapPrefix("https://simai.dev/contracts/",
                    CONTRACTS.toAbsolutePath().toUri().toString())));
    private static final String METRIC = "simai.i1.messages";
    /** 계약 예시의 gateway_id. 예시를 그대로 보내는 시험이 쓴다 */
    private static final String EXAMPLE_GATEWAY = "tier0-01";

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    MeterRegistry meters;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String laptopId = "laptop-" + tag;
    private final String laptopToken = "test-only-device-token-" + tag;
    private final String gatewayId = "tier0-endpoint-" + tag;

    @BeforeEach
    void setUp() {
        cleanEndpoints();
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'LAPTOP', ?, now())",
                laptopId, TokenHashes.sha256Hex(laptopToken));
    }

    @AfterEach
    void cleanUp() {
        cleanEndpoints();
        jdbc.update("DELETE FROM device WHERE device_id = ?", laptopId);
    }

    @Test
    void 계약_예시를_그대로_등록하면_조회가_같은_봉투를_돌려준다() throws Exception {
        ObjectNode example = read("tier0_endpoint.json");

        try (Tier0 tier0 = tier0()) {
            tier0.send(example);
        }

        awaitCount(() -> count(EXAMPLE_GATEWAY), 1);
        HttpResponse<String> response = get(laptopToken);
        assertThat(response.statusCode()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(response.body());
        assertThat(body).isEqualTo(example);
        assertValid(body);
        assertThat(jdbc.queryForObject("SELECT host(public_ip) FROM tier0_endpoint WHERE gateway_id = ?", String.class,
                EXAMPLE_GATEWAY)).isEqualTo("203.0.113.24");
    }

    @Test
    void 조회는_registered_at_이_가장_늦은_등록을_주고_같으면_나중에_받은_것을_준다() throws Exception {
        long base = Instant.now().toEpochMilli();
        ObjectNode first = endpoint(base, 44464);
        ObjectNode older = endpoint(base - 60_000, 45000);
        ObjectNode sameTimeLater = endpoint(base, 46000);
        ((ObjectNode) sameTimeLater.get("payload")).remove("pod_id");

        try (Tier0 tier0 = tier0()) {
            tier0.send(first);
            awaitCount(() -> count(gatewayId), 1);
            // 늦게 왔지만 Tier 0 시계로 더 이른 등록(재전송 지연 등)은 현재 엔드포인트가 아니다
            tier0.send(older);
            awaitCount(() -> count(gatewayId), 2);
            assertThat(MAPPER.readTree(get(laptopToken).body()).get("msg_id")).isEqualTo(first.get("msg_id"));
            tier0.send(sameTimeLater);
        }

        awaitCount(() -> count(gatewayId), 3);
        JsonNode body = MAPPER.readTree(get(laptopToken).body());
        assertThat(body).isEqualTo(sameTimeLater);
        // 없던 선택 필드는 null 로 채우지 않는다
        assertThat(body.get("payload").has("pod_id")).isFalse();
        assertValid(body);
        // 추가만 한다(덮어쓰지 않음)
        assertThat(count(gatewayId)).isEqualTo(3);
    }

    @Test
    void 같은_msg_id_를_다시_받으면_행이_늘지_않고_duplicate_로_센다() throws Exception {
        ObjectNode message = endpoint(Instant.now().toEpochMilli(), 44464);
        double duplicateBefore = counter("duplicate");

        try (Tier0 tier0 = tier0()) {
            tier0.send(message);
            tier0.send(message);
        }

        awaitCount(() -> (int) (counter("duplicate") - duplicateBefore), 1);
        assertThat(count(gatewayId)).isEqualTo(1);
    }

    @Test
    void 계약에_맞지_않는_등록은_저장하지_않고_연결은_유지한다() throws Exception {
        long base = Instant.now().toEpochMilli();
        List<String> invalidExamples = List.of("tier0_endpoint.secret_token.json", "tier0_endpoint.asymmetric_port.json",
                "tier0_endpoint.video_without_camera.json", "tier0_endpoint.missing_fingerprint.json");
        ObjectNode otherGateway = endpoint(base, 44464);
        ((ObjectNode) otherGateway.get("payload")).put("gateway_id", "tier0-someone-else");
        ObjectNode futureMajor = endpoint(base, 44464);
        futureMajor.put("schema_version", "2.0.0");
        ObjectNode valid = endpoint(base, 47000);
        double invalidBefore = counter("invalid");
        double unsupportedBefore = counter("unsupported_version");

        try (Tier0 tier0 = tier0()) {
            for (String file : invalidExamples) {
                tier0.send(ours(read("invalid/" + file)));
            }
            tier0.send(otherGateway);
            tier0.send(futureMajor);
            tier0.send(valid);
        }

        awaitCount(() -> count(gatewayId), 1);
        assertThat(counter("invalid") - invalidBefore).isEqualTo(invalidExamples.size() + 1.0);
        assertThat(counter("unsupported_version") - unsupportedBefore).isEqualTo(1.0);
        // 비밀 값이 실린 등록 등은 행을 남기지 않았다. 조회는 유일하게 맞는 등록이다
        assertThat(MAPPER.readTree(get(laptopToken).body())).isEqualTo(valid);
    }

    @Test
    void 등록이_없으면_404_와_오류_본문을_준다() throws Exception {
        HttpResponse<String> response = get(laptopToken);

        assertThat(response.statusCode()).isEqualTo(404);
        JsonNode problem = MAPPER.readTree(response.body());
        assertThat(problem.get("code").asString()).isEqualTo("TIER0_ENDPOINT_NOT_REGISTERED");
        assertThat(problem.get("status").asInt()).isEqualTo(404);
    }

    @Test
    void 기기_토큰이_아니면_조회를_거부한다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(endpoint(Instant.now().toEpochMilli(), 44464));
        }
        awaitCount(() -> count(gatewayId), 1);

        assertThat(get(null).statusCode()).isEqualTo(401);
        assertThat(get("wrong-token").statusCode()).isEqualTo(401);
        // 사용자 JWT(관리자 포함)·Tier 0 토큰은 기기 경로에서 쓸 수 없다
        assertThat(get(userToken("ADMIN")).statusCode()).isEqualTo(401);
        assertThat(get(TIER0_TOKEN).statusCode()).isEqualTo(401);
        assertThat(get(laptopToken).statusCode()).isEqualTo(200);
    }

    // ---- 도우미

    private void cleanEndpoints() {
        jdbc.update("DELETE FROM tier0_endpoint WHERE gateway_id IN (?, ?)", gatewayId, EXAMPLE_GATEWAY);
    }

    private static ObjectNode read(String name) throws Exception {
        return (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(name)));
    }

    /** 계약 예시를 이 시험의 gateway_id·새 msg_id·시각·제어 포트로 바꾼다 */
    private ObjectNode endpoint(long registeredAtMs, int controlPort) throws Exception {
        ObjectNode root = ours(read("tier0_endpoint.json"));
        ObjectNode payload = (ObjectNode) root.get("payload");
        payload.put("registered_at", registeredAtMs);
        ((ObjectNode) payload.get("ports").get(0)).put("public_port", controlPort);
        return root;
    }

    private ObjectNode ours(ObjectNode root) {
        root.remove("_expect_error");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", gatewayId);
        ((ObjectNode) root.get("payload")).put("gateway_id", gatewayId);
        return root;
    }

    private int count(String gateway) {
        return jdbc.queryForObject("SELECT count(*) FROM tier0_endpoint WHERE gateway_id = ?", Integer.class, gateway);
    }

    /** 지표는 앱 전체에서 누적된다. 시험은 보내기 전후 차이만 본다 */
    private double counter(String result) {
        Counter counter = meters.find(METRIC).tags("msg_type", "tier0_endpoint", "result", result).counter();
        return counter == null ? 0 : counter.count();
    }

    private static void awaitCount(Supplier<Integer> actual, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (actual.get() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(actual.get()).isEqualTo(expected);
    }

    private HttpResponse<String> get(String token) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/api/endpoints/tier0"))
                .header(HttpHeaders.ACCEPT, "application/json").GET();
        if (token != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return http.send(request.build(), HttpResponse.BodyHandlers.ofString());
    }

    private static void assertValid(JsonNode message) {
        List<String> problems = SCHEMAS.getSchema(SchemaLocation.of("https://simai.dev/contracts/messages/tier0_endpoint.schema.json"))
                .validate(MAPPER.writeValueAsString(message), InputFormat.JSON).stream().map(Object::toString).toList();
        assertThat(problems).isEmpty();
    }

    private String userToken(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private Tier0 tier0() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TIER0_TOKEN);
        WebSocketSession session = new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
        return new Tier0(session);
    }

    private record Tier0(WebSocketSession session) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
