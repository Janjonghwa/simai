package com.ssafy.simai.domain.upload;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.StorageProperties;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * POST /api/uploads/sign(S15P31A101-69). 서명은 계산만 하므로 저장소 없이 주소 모양·서명에 묶인 헤더·발급 기록을 본다.
 * 시험 설정은 path-style 로컬 주소(application-test.yaml). 실제 업로드·만료 거부는 S3 호환 저장소로 따로 확인한다(설계 문서).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class UploadSignIntegrationTest {

    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Pattern TOKEN = Pattern.compile("토큰\\(이번 한 번만 표시\\): (\\S+)");
    private static final String SHA = "a".repeat(64);

    @Autowired
    MockMvc mvc;

    @Autowired
    AdminCommands commands;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    ObjectStorage storage;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private long userId;

    @BeforeEach
    void user() {
        userId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'FOREMAN', now()) RETURNING user_id", Long.class, "upload-" + tag);
    }

    @Test
    void Tier0_토큰으로_클립_주소를_받으면_키는_서버가_정하고_형식_크기_해시가_서명에_묶인다() throws Exception {
        UUID eventId = UUID.randomUUID();
        UUID clipId = UUID.randomUUID();
        long before = System.currentTimeMillis();
        JsonNode body = ok(sign(TIER0_TOKEN, clip(eventId, clipId, SHA, 1234)));

        String key = "event-clips/" + eventId + "/" + clipId + ".h264";
        assertThat(body.get("object_key").stringValue()).isEqualTo(key);
        assertThat(body.get("object_uri").stringValue()).isEqualTo("s3://simai-test-bucket/" + key);
        assertThat(body.get("method").stringValue()).isEqualTo("PUT");
        String url = body.get("upload_url").stringValue();
        assertThat(url).startsWith("http://127.0.0.1:9000/simai-test-bucket/" + key + "?")
                .contains("X-Amz-Expires=300", "X-Amz-Signature=");
        assertThat(body.get("expires_at_ms").longValue()).isBetween(before + 299_000, System.currentTimeMillis() + 301_000);
        // 업로더가 그대로 실어야 하는 헤더. 다른 형식·크기·내용이면 저장소가 서명 불일치로 거부한다
        Map<String, String> headers = mapper.convertValue(body.get("headers"), Map.class);
        assertThat(headers).containsEntry("content-type", "video/h264").containsEntry("content-length", "1234")
                .containsEntry("x-amz-checksum-sha256", Base64.getEncoder().encodeToString(HexFormat.of().parseHex(SHA)))
                .doesNotContainKey("host");
        // 크기·해시가 쿼리 파라미터가 아니라 서명 헤더로 묶였는지 서명 헤더 목록에서 본다(SDK 판이 바뀌어도 잡게)
        Matcher signedHeaders = Pattern.compile("[?&]X-Amz-SignedHeaders=([^&]+)").matcher(url);
        assertThat(signedHeaders.find()).isTrue();
        assertThat(List.of(signedHeaders.group(1).split("%3B|;")))
                .contains("content-length", "content-type", "host", "x-amz-checksum-sha256");
        assertThat(url).doesNotContainIgnoringCase("x-amz-checksum-sha256=");

        Map<String, Object> grant = jdbc.queryForMap("SELECT * FROM upload_grant WHERE object_key = ?", key);
        assertThat(grant).containsEntry("purpose", "EVENT_CLIP").containsEntry("requester", "TIER0")
                .containsEntry("size_bytes", 1234L).containsEntry("sha256", SHA).containsEntry("event_id", eventId);
        assertThat(grant.get("requested_by_device")).isNull();
        assertThat(grant.get("used_at")).isNull();
    }

    @Test
    void 같은_클립은_같은_내용이면_다시_받고_내용이_다르면_앞선_발급이_만료된_뒤에만_바꾸며_기록된_뒤면_409() throws Exception {
        UUID eventId = UUID.randomUUID();
        UUID clipId = UUID.randomUUID();
        String first = ok(sign(TIER0_TOKEN, clip(eventId, clipId, SHA, 10))).get("object_key").stringValue();
        assertThat(ok(sign(TIER0_TOKEN, clip(eventId, clipId, SHA, 10))).get("object_key").stringValue()).isEqualTo(first);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE event_id = ?", Long.class, eventId)).isOne();

        sign(TIER0_TOKEN, clip(eventId, clipId, "b".repeat(64), 10)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("UPLOAD_KEY_CONFLICT"));
        sign(TIER0_TOKEN, clip(eventId, clipId, SHA, 11)).andExpect(status().isConflict());
        // 다른 주체(기기)는 Tier 0 가 받은 클립 키를 가져가지 못한다
        sign(device(), clip(eventId, clipId, SHA, 10)).andExpect(status().isConflict());

        // 앞선 발급이 만료됐고 아직 기록 전이면 다시 자른 클립(다른 크기·해시)으로 바꿔 준다
        jdbc.update("UPDATE upload_grant SET expires_at = now() - interval '1 second' WHERE event_id = ?", eventId);
        JsonNode recut = ok(sign(TIER0_TOKEN, clip(eventId, clipId, "c".repeat(64), 12)));
        assertThat(mapper.convertValue(recut.get("headers"), Map.class)).containsEntry("content-length", "12");
        assertThat(jdbc.queryForMap("SELECT size_bytes, sha256 FROM upload_grant WHERE event_id = ?", eventId))
                .containsEntry("size_bytes", 12L).containsEntry("sha256", "c".repeat(64));
        sign(TIER0_TOKEN, clip(eventId, clipId, SHA, 10)).andExpect(status().isConflict());

        jdbc.update("UPDATE upload_grant SET used_at = now() WHERE event_id = ?", eventId);
        sign(TIER0_TOKEN, clip(eventId, clipId, SHA, 10)).andExpect(status().isConflict());
    }

    @Test
    void 기기는_이벤트_프레임만_반장은_조치_사진만_받는다() throws Exception {
        UUID eventId = UUID.randomUUID();
        String deviceToken = device();
        JsonNode frame = ok(sign(deviceToken, Map.of("purpose", "EVENT_FRAME", "content_type", "IMAGE/JPEG",
                "size_bytes", 500, "sha256", SHA.toUpperCase(), "event_id", eventId.toString())));
        String frameKey = frame.get("object_key").stringValue();
        assertThat(frameKey).matches("event-frames/" + eventId + "/[0-9a-f-]{36}\\.jpg");
        assertThat(jdbc.queryForMap("SELECT requester, requested_by_device, sha256 FROM upload_grant WHERE object_key = ?",
                frameKey)).containsEntry("requester", "DEVICE").containsEntry("requested_by_device", "upload-" + tag)
                .containsEntry("sha256", SHA);
        // 프레임은 매번 새 키
        assertThat(ok(sign(deviceToken, Map.of("purpose", "EVENT_FRAME", "content_type", "image/png", "size_bytes", 5,
                "sha256", SHA, "event_id", eventId.toString()))).get("object_key").stringValue()).isNotEqualTo(frameKey);
        sign(deviceToken, photo()).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("UPLOAD_NOT_ALLOWED"));

        String foreman = jwt("FOREMAN");
        String photoKey = ok(sign(foreman, photo())).get("object_key").stringValue();
        assertThat(photoKey).matches("ticket-photos/[0-9a-f-]{36}\\.webp");
        assertThat(jdbc.queryForObject("SELECT requested_by_user FROM upload_grant WHERE object_key = ?", Long.class, photoKey))
                .isEqualTo(userId);
        // 조치 사진은 이벤트 칸을 비운다(보내도 싣지 않는다)
        Map<String, Object> withEvent = new HashMap<>(photo());
        withEvent.put("event_id", eventId.toString());
        String photoWithEvent = ok(sign(foreman, withEvent)).get("object_key").stringValue();
        assertThat(jdbc.queryForMap("SELECT event_id FROM upload_grant WHERE object_key = ?", photoWithEvent).get("event_id"))
                .isNull();
        sign(foreman, clip(eventId, UUID.randomUUID(), SHA, 10)).andExpect(status().isForbidden());
    }

    @Test
    void 반장이_아닌_사용자와_토큰_없음_틀린_토큰은_거부한다() throws Exception {
        sign(jwt("VIEWER"), photo()).andExpect(status().isForbidden());
        sign(jwt("ADMIN"), photo()).andExpect(status().isForbidden());
        // 서명은 맞지만 sub 가 user_id(숫자)가 아닌 반장 토큰은 500 이 아니라 403
        sign(jwt("FOREMAN", "not-a-number"), photo()).andExpect(status().isForbidden());
        sign(null, photo()).andExpect(status().isUnauthorized());
        sign("not-a-token", photo()).andExpect(status().isUnauthorized());
        // 워커 토큰은 이 경로의 주체가 아니다(워커 자산 주소는 따로 연다, 명세 14)
        String worker = issued("worker", "register", "--id", "upload-" + tag, "--job-types", "tbm_report");
        sign(worker, photo()).andExpect(status().isUnauthorized());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE requested_by_user = ?", Long.class, userId))
                .isZero();
    }

    @Test
    void 목적_형식_크기_해시_식별자가_맞지_않으면_400() throws Exception {
        UUID eventId = UUID.randomUUID();
        List<Map<String, Object>> bad = List.of(
                Map.of("purpose", "SELFIE", "content_type", "image/jpeg", "size_bytes", 5, "sha256", SHA),
                Map.of("purpose", "TICKET_PHOTO", "content_type", "image/gif", "size_bytes", 5, "sha256", SHA),
                Map.of("purpose", "TICKET_PHOTO", "content_type", "image/jpeg", "size_bytes", 0, "sha256", SHA),
                Map.of("purpose", "TICKET_PHOTO", "content_type", "image/jpeg", "size_bytes", (20L << 20) + 1, "sha256", SHA),
                Map.of("purpose", "TICKET_PHOTO", "content_type", "image/jpeg", "size_bytes", 5, "sha256", "abc"),
                Map.of("purpose", "TICKET_PHOTO", "content_type", "image/jpeg", "size_bytes", 5));
        for (Map<String, Object> request : bad) {
            sign(jwt("FOREMAN"), request).andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_UPLOAD"));
        }
        // 클립은 event_id·clip_id, 프레임은 event_id 가 있어야 한다
        sign(TIER0_TOKEN, Map.of("purpose", "EVENT_CLIP", "content_type", "video/h264", "size_bytes", 5, "sha256", SHA,
                "event_id", eventId.toString())).andExpect(status().isBadRequest());
        sign(TIER0_TOKEN, Map.of("purpose", "EVENT_FRAME", "content_type", "image/jpeg", "size_bytes", 5, "sha256", SHA))
                .andExpect(status().isBadRequest());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM upload_grant WHERE event_id = ?", Long.class, eventId)).isZero();
    }

    @Test
    void 조회_주소는_서명된_GET_이고_버킷이_없으면_서명하지_않는다() {
        ObjectStorage.SignedRequest get = storage.signGet("event-clips/a/b.h264");
        assertThat(get.url()).startsWith("http://127.0.0.1:9000/simai-test-bucket/event-clips/a/b.h264?")
                .contains("X-Amz-Expires=300", "X-Amz-Signature=");
        assertThat(get.expiresAt()).isAfter(Instant.now().plusSeconds(290));
        assertThat(get.toString()).doesNotContain("X-Amz-Signature");

        try (ObjectStorage none = new ObjectStorage(new StorageProperties("", null, null, false, null, null,
                Duration.ofMinutes(1), null))) {
            assertThat(none.configured()).isFalse();
            assertThatThrownBy(() -> none.signGet("k")).isInstanceOf(BusinessException.class)
                    .extracting(e -> ((BusinessException) e).getErrorCode()).isEqualTo(ErrorCode.Storage.STORAGE_NOT_CONFIGURED);
        }
    }

    private static Map<String, Object> clip(UUID eventId, UUID clipId, String sha, long size) {
        return Map.of("purpose", "EVENT_CLIP", "content_type", "video/h264", "size_bytes", size, "sha256", sha,
                "event_id", eventId.toString(), "clip_id", clipId.toString(), "camera_id", "cam_A");
    }

    private static Map<String, Object> photo() {
        return Map.of("purpose", "TICKET_PHOTO", "content_type", "image/webp", "size_bytes", 2048, "sha256", SHA);
    }

    private ResultActions sign(String token, Map<String, Object> body) throws Exception {
        var request = post("/api/uploads/sign").contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(body));
        if (token != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return mvc.perform(request);
    }

    private JsonNode ok(ResultActions result) throws Exception {
        String body = result.andExpect(status().isOk()).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);
        return mapper.readTree(body);
    }

    private String device() {
        return issued("device", "register", "--id", "upload-" + tag, "--kind", "LAPTOP");
    }

    private String jwt(String role) {
        return jwt(role, String.valueOf(userId));
    }

    private String jwt(String role, String subject) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(subject).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
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
