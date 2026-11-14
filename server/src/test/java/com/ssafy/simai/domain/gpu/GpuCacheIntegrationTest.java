package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;
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
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * GPU 캐시 서명(S15P31A101-700): 열린 GPU 세션의 워커 토큰만, 키 형식 검사, 없으면 miss·있으면 받을 주소, 올리기는 없을 때만 조건부 서명.
 * 객체 저장소는 가짜(MockitoBean)다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class GpuCacheIntegrationTest {

    private static final Pattern TOKEN = Pattern.compile("토큰\\(이번 한 번만 표시\\): (\\S+)");
    private static final String SHA = "ab".repeat(32);

    @Autowired
    MockMvc mvc;

    @Autowired
    AdminCommands commands;

    @Autowired
    ObjectMapper mapper;

    @MockitoBean
    ObjectStorage storage;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String workerId = "worker-gpu-" + suffix;
    private final String bootId = "gpu-boot-" + suffix;
    private String bootSecret;

    @BeforeEach
    void setUp() {
        issued("worker", "register", "--id", workerId, "--job-types", "vlm_rationale");
        bootSecret = issued("gpu-boot", "register", "--id", bootId, "--worker-id", workerId);
        when(storage.configured()).thenReturn(true);
        when(storage.headOrAbsentIfForbidden(anyString())).thenReturn(Optional.empty());
        when(storage.signGet(anyString())).thenAnswer(call -> new ObjectStorage.SignedRequest(
                "https://objects.test/" + call.getArgument(0) + "?sig=get", Map.of(), Instant.now().plusSeconds(300)));
        when(storage.signPut(anyString(), anyString(), anyLong(), anyString(), eq(true))).thenAnswer(call ->
                new ObjectStorage.SignedRequest("https://objects.test/" + call.getArgument(0) + "?sig=put",
                        Map.of("if-none-match", "*", "content-type", "application/x-tar"), Instant.now().plusSeconds(300)));
    }

    @Test
    void 세션_워커는_없으면_miss_있으면_받을_주소를_받는다() throws Exception {
        String worker = registerSession();
        sign(worker, "GET", "trt-engine", "nvidia-h100-80gb-hbm3/sm_90/0123456789abcdef", null, null)
                .andExpect(status().isOk())
                .andExpect(header().string(HttpHeaders.CACHE_CONTROL, "no-store"))
                .andExpect(jsonPath("$.hit").value(false))
                .andExpect(jsonPath("$.object_key")
                        .value("job-objects/gpu_cache/trt-engine/nvidia-h100-80gb-hbm3/sm_90/0123456789abcdef.tar"))
                .andExpect(jsonPath("$.url").doesNotExist());
        // 존재 확인은 403 을 없음으로 보는 HEAD 만 쓴다(다른 경로의 head 는 403 을 오류로 본다, S15P31A101-710)
        verify(storage, never()).head(anyString());

        when(storage.headOrAbsentIfForbidden("job-objects/gpu_cache/reid/cope-1.tar"))
                .thenReturn(Optional.of(new ObjectStorage.ObjectHead(1234, SHA)));
        MvcResult hit = sign(worker, "GET", "reid", "cope-1", null, null).andExpect(status().isOk()).andReturn();
        JsonNode body = mapper.readTree(hit.getResponse().getContentAsString(StandardCharsets.UTF_8));
        assertThat(body.get("hit").booleanValue()).isTrue();
        assertThat(body.get("sha256").stringValue()).isEqualTo(SHA);
        assertThat(body.get("size_bytes").longValue()).isEqualTo(1234);
        assertThat(body.get("method").stringValue()).isEqualTo("GET");
        assertThat(body.get("url").stringValue()).startsWith("https://objects.test/job-objects/gpu_cache/reid/cope-1.tar");
    }

    @Test
    void 올리기는_없을_때만_조건부로_서명하고_있으면_덮어쓰지_않는다() throws Exception {
        String worker = registerSession();
        sign(worker, "PUT", "vllm-compile", "sm_90/abc", 10_000L, SHA)
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.hit").value(false))
                .andExpect(jsonPath("$.method").value("PUT"))
                .andExpect(jsonPath("$.headers.if-none-match").value("*"));
        verify(storage).signPut("job-objects/gpu_cache/vllm-compile/sm_90/abc.tar", GpuCacheService.CONTENT_TYPE, 10_000L, SHA,
                true);

        when(storage.headOrAbsentIfForbidden("job-objects/gpu_cache/vllm-compile/sm_90/abd.tar"))
                .thenReturn(Optional.of(new ObjectStorage.ObjectHead(9, SHA)));
        sign(worker, "PUT", "vllm-compile", "sm_90/abd", 10_000L, SHA)
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.hit").value(true))
                .andExpect(jsonPath("$.url").doesNotExist());
        verify(storage, never()).signPut(eq("job-objects/gpu_cache/vllm-compile/sm_90/abd.tar"), anyString(), anyLong(),
                anyString(), eq(true));
    }

    @Test
    void 키_형식_종류_크기_해시가_틀리면_400() throws Exception {
        String worker = registerSession();
        sign(worker, "GET", "weights", "a", null, null).andExpect(status().isBadRequest());
        sign(worker, "GET", "reid", "../x", null, null).andExpect(status().isBadRequest());
        sign(worker, "GET", "reid", "a/../b", null, null).andExpect(status().isBadRequest());
        sign(worker, "GET", "reid", "/abs", null, null).andExpect(status().isBadRequest());
        sign(worker, "GET", "reid", "Upper", null, null).andExpect(status().isBadRequest());
        sign(worker, "DELETE", "reid", "a", null, null).andExpect(status().isBadRequest());
        sign(worker, "PUT", "reid", "a", null, SHA).andExpect(status().isBadRequest());
        sign(worker, "PUT", "reid", "a", (5L << 30), SHA).andExpect(status().isBadRequest());
        sign(worker, "PUT", "reid", "a", 10L, "nothex").andExpect(status().isBadRequest());
    }

    @Test
    void 세션에_묶이지_않은_워커와_부팅_비밀값은_받지_못한다() throws Exception {
        // 등록 전: 워커 토큰은 있지만 열린 GPU 세션이 없다
        String plain = issued("worker", "rotate", "--id", workerId);
        sign(plain, "GET", "reid", "cope-1", null, null).andExpect(status().isForbidden());
        // 부팅 비밀값은 워커 체인 밖이다
        sign(bootSecret, "GET", "reid", "cope-1", null, null).andExpect(status().isUnauthorized());
        // 세션이 끝나면(종료 보고) 그 워커 토큰은 폐기된다
        JsonNode session = registerPayload();
        String worker = session.at("/worker/token").stringValue();
        sign(worker, "GET", "reid", "cope-1", null, null).andExpect(status().isOk());
        UUID id = UUID.fromString(session.get("session_id").stringValue());
        mvc.perform(post("/api/gpu/sessions/" + id + "/reports").header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(endedBody(id)))
                .andExpect(status().isNoContent());
        sign(worker, "GET", "reid", "cope-1", null, null).andExpect(status().isUnauthorized());
    }

    private ResultActions sign(String token, String method, String kind, String key, Long size, String sha) throws Exception {
        var body = mapper.createObjectNode().put("kind", kind).put("cache_key", key).put("method", method);
        if (size != null) {
            body.put("size_bytes", size);
        }
        if (sha != null) {
            body.put("sha256", sha);
        }
        return mvc.perform(post(GpuPaths.CACHE_SIGN).header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(body)));
    }

    private String registerSession() throws Exception {
        return registerPayload().at("/worker/token").stringValue();
    }

    private JsonNode registerPayload() throws Exception {
        var root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gpu_session_register");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", bootId);
        var p = root.putObject("payload");
        p.put("provider", "RUNPOD");
        p.put("instance_id", "pod-" + suffix);
        p.put("gpu_type", "NVIDIA H100 80GB HBM3");
        p.put("region", "AP-JP-1");
        p.put("public_host", "203.0.113.24");
        p.set("ports", mapper.readTree("[{\"role\":\"CONTROL\",\"public_port\":17000},{\"role\":\"VIDEO\",\"public_port\":17001}]"));
        p.putNull("image_digest");
        p.putNull("price_per_hour_usd");
        MvcResult result = mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(root)))
                .andExpect(status().isCreated()).andReturn();
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8)).get("payload");
    }

    private String endedBody(UUID sessionId) {
        var root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gpu_session_report");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", bootId);
        var p = root.putObject("payload");
        p.put("session_id", sessionId.toString());
        p.put("stage", "ENDED");
        p.put("reason_code", "PROVIDER_STOP");
        p.put("reported_at", Instant.now().toEpochMilli());
        return mapper.writeValueAsString(root);
    }

    private String issued(String... args) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        int code = commands.execute(List.of(args), new CommandIo(print, print, prompt -> null));
        assertThat(code).isZero();
        Matcher matcher = TOKEN.matcher(out.toString(StandardCharsets.UTF_8));
        assertThat(matcher.find()).isTrue();
        return matcher.group(1);
    }
}
