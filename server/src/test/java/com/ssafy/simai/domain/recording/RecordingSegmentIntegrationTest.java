package com.ssafy.simai.domain.recording;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.security.TokenHashes;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;
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
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 게이트웨이 녹화 작업 구간 업로드(S15P31A101-668). 노트북 기기 토큰으로 작업 구간을 묻고(GET /api/devices/recordings/windows),
 * 업로드 주소를 받아(POST /api/uploads/sign, RECORDING_SEGMENT) 조각을 등록하면(POST /api/devices/recordings/segments)
 * 기존 상시 영상 조각 표에 남아 관리자 다시 보기 목록·재생 주소(GET /api/videos)로 나오는지 본다. 저장소는 서명만 하는 시험 주소다.
 * 시험마다 다른 기기·카메라·작업을 쓰고 끝나면 지운다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class RecordingSegmentIntegrationTest {

    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String SHA = "c".repeat(64);

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String laptop = "laptop-rec-" + tag;
    private final String laptopToken = "test-only-rec-laptop-" + tag;
    private final String watch = "watch-rec-" + tag;
    private final String watchToken = "test-only-rec-watch-" + tag;
    private final String camera = "cam_" + tag;
    /** 작업 구간: 세팅 완료(2일 전 09:00 꼴) → 작업 종료(+8시간). 작업 시작은 세팅 완료 30분 뒤 */
    private final Instant setupDone = Instant.now().minus(Duration.ofDays(2)).truncatedTo(ChronoUnit.SECONDS)
            .plusMillis((long) (Math.random() * 3_600_000L));
    private final Instant ended = setupDone.plus(Duration.ofHours(8));
    private String site;
    private long adminId;
    private long workId;

    @BeforeEach
    void setUp() {
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'LAPTOP', ?, now())",
                laptop, TokenHashes.sha256Hex(laptopToken));
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'WATCH', ?, now())",
                watch, TokenHashes.sha256Hex(watchToken));
        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id", Long.class, "rec-admin-" + tag);
        site = jdbc.queryForObject("SELECT site_id FROM site ORDER BY site_id LIMIT 1", String.class);
        workId = doneWork(site, setupDone, setupDone.plus(Duration.ofMinutes(30)), ended);
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM video_access_log WHERE user_id = ?", adminId);
        jdbc.update("DELETE FROM upload_grant WHERE requested_by_device IN (?, ?)", laptop, watch);
        jdbc.update("DELETE FROM video_segment WHERE gateway_id = ?", laptop);
        jdbc.update("DELETE FROM work_unit WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM site WHERE site_id = ?", "rec-" + tag);
        jdbc.update("DELETE FROM device WHERE device_id IN (?, ?)", laptop, watch);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 끝난_작업의_구간을_알려_주고_올린_조각을_기록해_관리자_다시_보기에_mp4_로_보인다() throws Exception {
        long from = setupDone.minus(Duration.ofHours(1)).toEpochMilli();
        long to = ended.plus(Duration.ofHours(1)).toEpochMilli();
        JsonNode windows = get("/api/devices/recordings/windows?from=" + from + "&to=" + to, laptopToken, 200);
        JsonNode mine = null;
        for (JsonNode w : windows.get("windows")) {
            if (w.get("work_id").asLong() == workId) {
                mine = w;
            }
        }
        assertThat(mine).as(windows.toString()).isNotNull();
        // 세팅 완료가 작업 시작보다 이르면 세팅 완료부터
        assertThat(mine.get("start_ms").asLong()).isEqualTo(setupDone.toEpochMilli());
        assertThat(mine.get("end_ms").asLong()).isEqualTo(ended.toEpochMilli());
        assertThat(windows.get("retention_days").asInt()).isEqualTo(14);
        assertThat(windows.get("schema_version").asString()).isEqualTo("1.0.0");

        // 구간 시작 10 s 전 키프레임부터 자른 5분 조각
        long start = setupDone.toEpochMilli() - 10_000;
        UUID id = uuid7(start);
        String key = sign(id, 4096);
        assertThat(key).isEqualTo("recording-segments/" + id + ".mp4");
        JsonNode created = postJson("/api/devices/recordings/segments", laptopToken, segment(id, start, start + 300_040, 4096), 200);
        assertThat(created.get("status").asString()).isEqualTo("CREATED");
        // 다시 보내면(이어 올리기) 같은 내용이라 DUPLICATE
        assertThat(postJson("/api/devices/recordings/segments", laptopToken, segment(id, start, start + 300_040, 4096), 200)
                .get("status").asString()).isEqualTo("DUPLICATE");

        Timestamp deleteAfter = jdbc.queryForObject("SELECT delete_after FROM video_segment WHERE segment_id = ?", Timestamp.class, id);
        assertThat(deleteAfter.toInstant()).isEqualTo(Instant.ofEpochMilli(start).plus(Duration.ofDays(14)));
        assertThat(jdbc.queryForObject("SELECT origin FROM video_segment WHERE segment_id = ?", String.class, id))
                .isEqualTo("GATEWAY_RECORDING");
        assertThat(jdbc.queryForObject("SELECT work_id FROM video_segment WHERE segment_id = ?", Long.class, id)).isEqualTo(workId);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE object_key = ? AND used_at IS NOT NULL",
                Integer.class, key)).isOne();

        JsonNode list = get("/api/videos?from=" + (start + 1000) + "&to=" + (start + 2000) + "&camera_id=" + camera, admin(), 200);
        assertThat(list.get("segments")).hasSize(1);
        JsonNode item = list.get("segments").get(0);
        assertThat(item.get("container").asString()).isEqualTo("mp4");
        assertThat(item.get("origin").asString()).isEqualTo("GATEWAY_RECORDING");
        assertThat(item.get("segment_start_capture_ms").asLong()).isEqualTo(start);
        JsonNode playback = get("/api/videos/" + id, admin(), 200);
        assertThat(playback.get("url").asString()).contains(key);
        assertThat(playback.get("content_type").asString()).isEqualTo("video/mp4");
    }

    @Test
    void 진행_중인_작업이_겹치면_open_work_이고_구간이_없는_범위는_비어_있다() throws Exception {
        // 진행 중 작업은 현장마다 하나라 이 시험만의 현장을 쓴다
        jdbc.update("INSERT INTO site (site_id, name, created_at) VALUES (?, '녹화 시험', now())", "rec-" + tag);
        Instant openStart = Instant.parse("2001-01-01T09:00:00Z");
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, actual_started_at,
                    created_by, created_at, updated_at)
                VALUES (?, DATE '2001-01-01', 'open', ?, ?, 1, 'IN_PROGRESS', ?, ?, now(), now())
                """, "rec-" + tag, Timestamp.from(openStart), Timestamp.from(openStart.plus(Duration.ofHours(8))),
                Timestamp.from(openStart), adminId);
        long dayStart = openStart.minus(Duration.ofHours(1)).toEpochMilli();
        JsonNode open = get("/api/devices/recordings/windows?from=" + dayStart + "&to=" + (dayStart + 36_000_000L), laptopToken, 200);
        assertThat(open.get("open_work").asBoolean()).isTrue();
        assertThat(open.get("windows")).isEmpty();

        JsonNode none = get("/api/devices/recordings/windows?from=0&to=1000", laptopToken, 200);
        assertThat(none.get("open_work").asBoolean()).isFalse();
        assertThat(none.get("windows")).isEmpty();
        // 24시간 초과·거꾸로·숫자 아님은 400
        get("/api/devices/recordings/windows?from=0&to=" + Duration.ofHours(25).toMillis(), laptopToken, 400);
        get("/api/devices/recordings/windows?from=2000&to=1000", laptopToken, 400);
        get("/api/devices/recordings/windows?from=x&to=1000", laptopToken, 400);
    }

    @Test
    void 구간_밖_보관_지남_발급과_다름_계약_위반_같은_ID_다른_내용은_거부한다() throws Exception {
        long inside = setupDone.toEpochMilli() + 60_000;
        // 구간 시작보다 30 s 넘게 이른 조각
        UUID early = uuid7(inside);
        sign(early, 100);
        long earlyStart = setupDone.toEpochMilli() - 40_000;
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, segment(early, earlyStart, earlyStart + 1000, 100)), 409))
                .isEqualTo("OUTSIDE_WORK_WINDOW");
        // 작업 종료 뒤로 넘는 조각
        UUID late = uuid7(inside);
        sign(late, 100);
        long lateStart = ended.toEpochMilli() - 1000;
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, segment(late, lateStart, lateStart + 5000, 100)), 409))
                .isEqualTo("OUTSIDE_WORK_WINDOW");
        // 없는 작업
        UUID noWork = uuid7(inside);
        sign(noWork, 100);
        ObjectNode wrongWork = segment(noWork, inside, inside + 1000, 100);
        wrongWork.put("work_id", Long.MAX_VALUE);
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, wrongWork), 409)).isEqualTo("OUTSIDE_WORK_WINDOW");
        // 크기가 발급과 다름 · 발급 없음
        UUID size = uuid7(inside);
        sign(size, 100);
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, segment(size, inside, inside + 1000, 101)), 400))
                .isEqualTo("RECORDING_UPLOAD_MISMATCH");
        UUID unsigned = uuid7(inside);
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, segment(unsigned, inside, inside + 1000, 100)), 400))
                .isEqualTo("RECORDING_UPLOAD_MISMATCH");
        // 계약 위반(h264 그대로)
        UUID raw = uuid7(inside);
        ObjectNode annexb = segment(raw, inside, inside + 1000, 100);
        annexb.put("container", "h264-annexb");
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, annexb), 400)).isEqualTo("INVALID_RECORDING_SEGMENT");
        // 보관 기간(14일)이 지난 녹화
        Instant oldSetup = Instant.now().minus(Duration.ofDays(15));
        long oldWork = doneWork(site, oldSetup, oldSetup, oldSetup.plus(Duration.ofHours(8)));
        UUID expired = uuid7(oldSetup.toEpochMilli());
        sign(expired, 100);
        ObjectNode old = segment(expired, oldSetup.toEpochMilli() + 1000, oldSetup.toEpochMilli() + 2000, 100);
        old.put("work_id", oldWork);
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, old), 410)).isEqualTo("RECORDING_EXPIRED");
        // 같은 ID 에 다른 해시
        UUID dup = uuid7(inside);
        sign(dup, 100);
        postJson("/api/devices/recordings/segments", laptopToken, segment(dup, inside, inside + 1000, 100), 200);
        ObjectNode other = segment(dup, inside, inside + 1000, 100);
        other.put("sha256", "sha256:" + "d".repeat(64));
        assertThat(code(post("/api/devices/recordings/segments", laptopToken, other), 409)).isEqualTo("RECORDING_SEGMENT_CONFLICT");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM video_segment WHERE gateway_id = ?", Integer.class, laptop)).isOne();
    }

    @Test
    void 노트북만_올리고_업로드_목적은_기기만_mp4_만_받는다() throws Exception {
        long inside = setupDone.toEpochMilli() + 60_000;
        assertThat(code(http.send(getRequest("/api/devices/recordings/windows?from=0&to=1000", watchToken),
                HttpResponse.BodyHandlers.ofString()), 403)).isEqualTo("RECORDING_NOT_A_LAPTOP");
        get("/api/devices/recordings/windows?from=0&to=1000", admin(), 401);
        // 관리자 토큰으로는 RECORDING_SEGMENT 주소를 받지 못한다
        UUID id = uuid7(inside);
        assertThat(post("/api/uploads/sign", admin(), signBody(id, 100, "video/mp4")).statusCode()).isEqualTo(403);
        // h264 는 받지 않는다(재인코딩 없이 mp4 로 포장해 올린다)
        assertThat(post("/api/uploads/sign", laptopToken, signBody(id, 100, "video/h264")).statusCode()).isEqualTo(400);
    }

    private long doneWork(String siteId, Instant setup, Instant started, Instant end) {
        return jdbc.queryForObject("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, actual_started_at,
                    actual_ended_at, setup_completed_at, created_by, created_at, updated_at)
                VALUES (?, DATE '2001-01-02', 'rec', ?, ?, 1, 'DONE', ?, ?, ?, ?, now(), now()) RETURNING work_id
                """, Long.class, siteId, Timestamp.from(started), Timestamp.from(end), Timestamp.from(started), Timestamp.from(end),
                Timestamp.from(setup), adminId);
    }

    private String sign(UUID segmentId, long size) throws Exception {
        HttpResponse<String> response = post("/api/uploads/sign", laptopToken, signBody(segmentId, size, "video/mp4"));
        assertThat(response.statusCode()).as(response.body()).isEqualTo(200);
        return MAPPER.readTree(response.body()).get("object_key").asString();
    }

    private static ObjectNode signBody(UUID segmentId, long size, String contentType) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("purpose", "RECORDING_SEGMENT");
        body.put("content_type", contentType);
        body.put("size_bytes", size);
        body.put("sha256", SHA);
        body.put("segment_id", segmentId.toString());
        return body;
    }

    /** 계약 예시 recording_segment 를 이 작업·카메라·구간·크기로 바꾼다 */
    private ObjectNode segment(UUID id, long startMs, long endMs, long size) throws Exception {
        ObjectNode body = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("recording_segment.json")));
        body.put("segment_id", id.toString());
        body.put("work_id", workId);
        body.put("camera_id", camera);
        body.put("segment_start_capture_ms", startMs);
        body.put("segment_end_capture_ms", endMs);
        body.put("size_bytes", size);
        body.put("sha256", "sha256:" + SHA);
        body.put("object_key", "recording-segments/" + id + ".mp4");
        body.put("created_at_ms", Instant.now().toEpochMilli());
        body.put("uploaded_at_ms", Instant.now().toEpochMilli());
        return body;
    }

    private HttpRequest getRequest(String path, String token) {
        return HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token).GET().build();
    }

    private JsonNode get(String path, String token, int expectedStatus) throws Exception {
        HttpResponse<String> response = http.send(getRequest(path, token), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(path + " → " + response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.nullNode() : MAPPER.readTree(response.body());
    }

    private HttpResponse<String> post(String path, String token, JsonNode body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token).header(HttpHeaders.CONTENT_TYPE, "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build();
        return http.send(request, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode postJson(String path, String token, JsonNode body, int expectedStatus) throws Exception {
        HttpResponse<String> response = post(path, token, body);
        assertThat(response.statusCode()).as(response.body()).isEqualTo(expectedStatus);
        return MAPPER.readTree(response.body());
    }

    private static String code(HttpResponse<String> response, int expectedStatus) throws Exception {
        assertThat(response.statusCode()).as(response.body()).isEqualTo(expectedStatus);
        return MAPPER.readTree(response.body()).get("code").asString();
    }

    private String admin() {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(adminId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, "ADMIN").build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    /** 시각 칸이 주어진 ms 인 UUIDv7 */
    private static UUID uuid7(long ms) {
        UUID random = UUID.randomUUID();
        long high = (ms << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long low = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(high, low);
    }
}
