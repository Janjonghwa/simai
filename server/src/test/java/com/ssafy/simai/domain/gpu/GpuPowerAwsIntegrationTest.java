package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.gpu.provider.AwsGpuApi;
import com.ssafy.simai.domain.gpu.provider.AwsGpuProvider;
import com.ssafy.simai.domain.gpu.provider.FakeAwsGpuApi;
import com.ssafy.simai.domain.gpu.provider.RunPodProvider;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.properties.GpuProperties;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Instant;
import java.util.Base64;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
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
import tools.jackson.databind.node.ObjectNode;

/**
 * 화면 GPU 켜기·끄기의 AWS 경로(S15P31A101-703). 가짜 AWS(AwsGpuApi)로 설정 칸(시작 템플릿·서브넷·보안 그룹)·재고 표·켜기 요청·끄기를 보고,
 * 기본(꺼짐)일 때 "준비 안 됨"(503)인지 본다. 실제 AWS 를 부르지 않는다. 다른 시험이 남긴 상태는 이 트랜잭션 안에서만 닫는다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class GpuPowerAwsIntegrationTest {

    private static final String IMAGE = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/simai/tier0@sha256:" + "b".repeat(64);
    private static final String G6E = "g6e.2xlarge";
    private static final String G7E = "g7e.2xlarge";
    private static final String G7E4 = "g7e.4xlarge";

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    AwsGpuProvider awsProvider;

    @Autowired
    GpuPowerService power;

    @Autowired
    GpuSessionService sessions;

    @Autowired
    GpuProperties properties;

    @Autowired
    RunPodProvider runpod;

    private final FakeAwsGpuApi api = new FakeAwsGpuApi();
    private String admin;

    @BeforeEach
    void setUp() {
        power.useClock(Clock.systemUTC());
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = greatest(now(), actual_started_at) WHERE status = 'IN_PROGRESS'");
        jdbc.update("UPDATE setup_session SET status = 'FAILED' WHERE status NOT IN ('COMPLETED', 'FAILED')");
        jdbc.update("UPDATE setup_supplement_rebake SET status = 'CANCELLED' WHERE status IN ('QUEUED', 'RUNNING')");
        for (String open : jdbc.queryForList("""
                SELECT s.session_id::text FROM gpu_session s
                WHERE NOT EXISTS (SELECT 1 FROM gpu_session_event e WHERE e.session_id = s.session_id AND e.kind = 'ENDED')
                """, String.class)) {
            sessions.end(UUID.fromString(open), GpuSessionService.EndReason.SUPERSEDED);
        }
        for (String open : jdbc.queryForList("""
                SELECT l.launch_id::text FROM gpu_launch l WHERE NOT EXISTS (SELECT 1 FROM gpu_launch_event e
                    WHERE e.launch_id = l.launch_id AND e.kind IN ('CREATE_FAILED', 'STOPPED', 'GONE'))
                """, String.class)) {
            jdbc.update("INSERT INTO gpu_launch_event (launch_id, kind, at) VALUES (?::uuid, 'GONE', now())", open);
        }
        Long adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now())"
                + " RETURNING user_id", Long.class, "gpu-aws-admin-" + UUID.randomUUID().toString().substring(0, 8));
        admin = "Bearer " + jwt(adminId);

        api.zones.put(G6E, Set.of("ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"));
        api.zones.put(G7E, Set.of("ap-northeast-2b"));
        api.specs.put(G6E, new AwsGpuApi.GpuSpec("NVIDIA L40S", 1, 48));
        api.subnets.put("subnet-0aaa1111bbbb2222c", "ap-northeast-2a");
        api.subnets.put("subnet-0ddd3333eeee4444f", "ap-northeast-2b");
        api.onDemand.put(G6E, new BigDecimal("2.7565"));
        api.onDemand.put(G7E, new BigDecimal("4.1348"));
        // S15P31A101-841: AWS 허용 목록은 g7e.4xlarge 하나(사용자 결정 11-23). 2c 는 설정 서브넷이 없어 NONE
        api.zones.put(G7E4, Set.of("ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"));
        api.specs.put(G7E4, new AwsGpuApi.GpuSpec("NVIDIA RTX PRO 6000", 1, 96));
        api.onDemand.put(G7E4, new BigDecimal("4.9155"));
    }

    @AfterEach
    void tearDown() {
        awsProvider.useApi(new FakeAwsGpuApi(), properties.aws()); // 기본(꺼짐)으로 되돌린다
        runpod.useEndpoints("https://api.runpod.io/v2", "https://api.runpod.io", "");
    }

    @Test
    void 기본은_꺼져_있어_AWS_는_준비_안_됨이고_재고_조회와_켜기가_503_이다() throws Exception {
        awsProvider.useApi(api, properties.aws());
        save(awsSettings("lt-0123456789abcdef0", false)).andExpect(status().isCreated());
        mvc.perform(get("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(jsonPath("$.providers[1].id").value("AWS"))
                .andExpect(jsonPath("$.providers[1].configured").value(false))
                .andExpect(jsonPath("$.providers[1].default").value(false))
                .andExpect(jsonPath("$.providers[0].default").value(true)); // S15P31A101-841: AWS 가 준비 안 됐으면 RunPod 이 기본
        mvc.perform(get("/api/gpu/offers").param("provider", "AWS").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("GPU_PROVIDER_NOT_CONFIGURED"));
        launch("AWS").andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("GPU_PROVIDER_NOT_CONFIGURED"));
        assertThat(api.calls).isEmpty();
    }

    @Test
    void AWS_설정이면_재고_표를_가용_영역으로_보이고_켜면_시작_템플릿_서브넷으로_만들고_끄면_지운다() throws Exception {
        awsProvider.useApi(api, enabled());
        save(awsSettings("lt-0123456789abcdef0", false)).andExpect(status().isCreated())
                .andExpect(jsonPath("$.aws_launch_template_id").value("lt-0123456789abcdef0"))
                .andExpect(jsonPath("$.aws_subnet_ids[1]").value("subnet-0ddd3333eeee4444f"))
                .andExpect(jsonPath("$.aws_security_group_ids[0]").value("sg-0123456789abcdef0"));
        mvc.perform(get("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(jsonPath("$.settings.provider").value("AWS"))
                .andExpect(jsonPath("$.providers[1].configured").value(true));

        // 선호 가용 영역 2a 먼저. 실시간 재고를 모르니 UNKNOWN(고를 수 있음)
        // S15P31A101-841: AWS 표는 허용 목록(g7e.4xlarge)만 행이 된다. 저장한 선호(g6e·g7e.2xlarge)와 카탈로그의 p5.48xlarge(시간당 약 76달러,
        // 예산 약 200달러)는 묻지도 싣지도 않는다
        api.zones.put("p5.48xlarge", Set.of("ap-northeast-2a"));
        api.specs.put("p5.48xlarge", new AwsGpuApi.GpuSpec("NVIDIA H100", 8, 80));
        api.zones.put("g5.xlarge", Set.of("ap-northeast-2a"));
        mvc.perform(get("/api/gpu/offers").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.provider").value("AWS"))
                .andExpect(jsonPath("$.default.gpu_type").value(G7E4))
                .andExpect(jsonPath("$.default.region").value("ap-northeast-2a"))
                .andExpect(jsonPath("$.gpu_types.length()").value(1))
                .andExpect(jsonPath("$.gpu_types[0].gpu_type").value(G7E4))
                .andExpect(jsonPath("$.gpu_types[0].name").value(G7E4 + " · NVIDIA RTX PRO 6000 ×1"))
                .andExpect(jsonPath("$.gpu_types[0].tier").value("RECOMMENDED"))
                .andExpect(jsonPath("$.gpu_types[0].available").isEmpty())
                .andExpect(jsonPath("$.cells[0].gpu_type").value(G7E4))
                .andExpect(jsonPath("$.cells[0].region").value("ap-northeast-2a"))
                .andExpect(jsonPath("$.cells[0].stock").value("UNKNOWN"))
                .andExpect(jsonPath("$.cells[0].available").isEmpty())
                .andExpect(jsonPath("$.cells[0].price_per_hour_usd").value(4.9155));
        assertThat(api.calls).noneMatch(c -> c.contains("p5.48xlarge") || c.contains(G6E) || c.contains(G7E));
        // 허용 목록 밖 종류는 API 로도 켜지 않는다(저장한 선호에 있어도)
        for (String type : new String[] {"p5.48xlarge", G7E, "g6e.4xlarge", G6E}) {
            launchCell("AWS", type, "ap-northeast-2a").andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_GPU_SETTINGS"));
        }
        assertThat(api.runs).isEmpty();

        JsonNode launch = mapper.readTree(launch().andExpect(status().isCreated()).andReturn().getResponse()
                .getContentAsString(StandardCharsets.UTF_8));
        assertThat(launch.get("provider").stringValue()).isEqualTo("AWS");
        assertThat(launch.get("instance_id").stringValue()).isEqualTo("i-0123456789abcdef0");
        AwsGpuApi.RunRequest run = api.runs.getFirst();
        assertThat(run.launchTemplateId()).isEqualTo("lt-0123456789abcdef0");
        assertThat(run.instanceType()).isEqualTo(G7E4);
        assertThat(run.subnetId()).isEqualTo("subnet-0aaa1111bbbb2222c");
        assertThat(run.securityGroupIds()).containsExactly("sg-0123456789abcdef0");
        assertThat(run.spot()).isFalse();
        assertThat(run.clientToken()).isEqualTo(launch.get("launch_id").stringValue());
        String script = new String(Base64.getDecoder().decode(run.userDataBase64()), StandardCharsets.UTF_8);
        assertThat(script).contains("\nSIMAI_GPU_PROVIDER=AWS\n").contains("\nSIMAI_EC2_BASE_URL=https://ec2.test\n")
                .contains("\nSIMAI_PRICE_PER_HOUR_USD=4.9155\n").contains("\nSIMAI_SECRETS_SOURCE=aws-secretsmanager\n")
                .doesNotContain("SIMAI_GPU_BOOT_SECRET=").doesNotContain("RUNPOD_SECRET");
        assertThat(jdbc.queryForObject("SELECT provider || '/' || gpu_type || '/' || region FROM gpu_launch WHERE launch_id = ?::uuid",
                String.class, launch.get("launch_id").stringValue())).isEqualTo("AWS/" + G7E4 + "/ap-northeast-2a");
        current().andExpect(jsonPath("$.power").value("STARTING"));

        mvc.perform(post("/api/gpu/stop").header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isNoContent());
        assertThat(api.terminated).containsExactly("i-0123456789abcdef0");
        current().andExpect(jsonPath("$.launch.state").value("STOPPED")).andExpect(jsonPath("$.power").value("OFF"));
    }

    @Test
    void 시작_템플릿이_없으면_켜지_않고_용량_없음은_재고_없음으로_남긴다() throws Exception {
        awsProvider.useApi(api, enabled());
        save(awsSettings(null, true)).andExpect(status().isCreated());
        launch("AWS").andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("GPU_PROVIDER_NOT_CONFIGURED"));
        assertThat(api.runs).isEmpty();

        save(awsSettings("lt-0123456789abcdef0", true)).andExpect(status().isCreated());
        api.spot.put(G7E4 + "\nap-northeast-2a", new BigDecimal("1.4211"));
        api.runFailure = new com.ssafy.simai.domain.gpu.provider.GpuProviderException("OUT_OF_STOCK", "AWS 용량 없음");
        launch().andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_OUT_OF_STOCK"));
        current().andExpect(jsonPath("$.launch.state").value("FAILED")).andExpect(jsonPath("$.launch.failure_code").value("OUT_OF_STOCK"))
                .andExpect(jsonPath("$.launch.price_per_hour_usd").value(1.4211)).andExpect(jsonPath("$.launch.interruptible").value(true));
    }

    @Test
    void AWS_설정_칸은_형식을_확인한다() throws Exception {
        ObjectNode runpodNames = awsSettings("lt-0123456789abcdef0", false);
        runpodNames.putArray("preferred_gpu_types").add("NVIDIA H100 80GB HBM3"); // AWS 는 인스턴스 종류여야 한다
        save(runpodNames).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_GPU_SETTINGS"));
        ObjectNode badZone = awsSettings("lt-0123456789abcdef0", false);
        badZone.putArray("preferred_regions").add("AP-JP-1");
        save(badZone).andExpect(status().isBadRequest());
        ObjectNode badSubnet = awsSettings("lt-0123456789abcdef0", false);
        badSubnet.putArray("aws_subnet_ids").add("subnet-xyz");
        save(badSubnet).andExpect(status().isBadRequest());
        ObjectNode badTemplate = awsSettings("template-1", false);
        save(badTemplate).andExpect(status().isBadRequest());
    }

    // ---- S15P31A101-841 공급자 탭

    @Test
    void 기본_공급자는_AWS_가_준비되면_AWS_이고_시작_템플릿이_없으면_RunPod_이다() throws Exception {
        awsProvider.useApi(api, enabled());
        save(awsSettings(null, false)).andExpect(status().isCreated());
        assertThat(power.defaultProvider()).isEqualTo("RUNPOD"); // 서버 설정은 됐지만 시작 템플릿이 없어 켤 수 없다
        save(runpodSettingsWithAwsTemplate()).andExpect(status().isCreated());
        assertThat(power.defaultProvider()).isEqualTo("AWS"); // 설정 공급자(선호값 주인)가 RunPod 이어도 AWS 가 기본
        mvc.perform(get("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(jsonPath("$.settings.provider").value("RUNPOD"))
                .andExpect(jsonPath("$.providers[0].id").value("RUNPOD"))
                .andExpect(jsonPath("$.providers[0].default").value(false))
                .andExpect(jsonPath("$.providers[1].id").value("AWS"))
                .andExpect(jsonPath("$.providers[1].default").value(true));
        FakeAwsGpuApi noCredentials = new FakeAwsGpuApi();
        noCredentials.credentials = false; // 자격 증명이 없으면(준비 안 됨) 다시 RunPod
        awsProvider.useApi(noCredentials, enabled());
        assertThat(power.defaultProvider()).isEqualTo("RUNPOD");
    }

    @Test
    void 설정_공급자가_아닌_AWS_를_고르면_서버_기본_선호로_재고와_켜기를_하고_시작_템플릿은_설정_것을_쓴다() throws Exception {
        awsProvider.useApi(api, enabled());
        api.zones.put(G7E4, Set.of("ap-northeast-2b", "ap-northeast-2c"));
        save(runpodSettingsWithAwsTemplate()).andExpect(status().isCreated());

        // 선호값은 RunPod 것이라 AWS 는 서버 기본 선호(g7e.4xlarge 먼저, 가용 영역 2a → 2d)를 쓴다. 2a 에는 g7e.4xlarge 가 없다.
        // 표 행은 g7e.4xlarge 하나뿐이다(카탈로그의 p5.48xlarge·g6e 없음)
        api.zones.put("p5.48xlarge", Set.of("ap-northeast-2a"));
        api.specs.put("p5.48xlarge", new AwsGpuApi.GpuSpec("NVIDIA H100", 8, 80));
        String body = mvc.perform(get("/api/gpu/offers").param("provider", "AWS").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);
        assertThat(mapper.readTree(body).get("gpu_types").valueStream().map(g -> g.get("gpu_type").stringValue()).toList())
                .containsExactly(G7E4);
        for (String type : new String[] {"p5.48xlarge", G7E, "g6e.4xlarge"}) {
            launchCell("AWS", type, "ap-northeast-2b").andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_GPU_SETTINGS"));
        }
        assertThat(api.runs).isEmpty();
        mvc.perform(get("/api/gpu/offers").param("provider", "AWS").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.provider").value("AWS"))
                .andExpect(jsonPath("$.interruptible").value(false))
                .andExpect(jsonPath("$.default.gpu_type").value("g7e.4xlarge"))
                .andExpect(jsonPath("$.default.region").value("ap-northeast-2b"));
        mvc.perform(get("/api/gpu/offers").param("provider", "GCP").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_GPU_SETTINGS"));

        // provider 를 실어 켜면 AWS 로, 시작 템플릿·서브넷·보안 그룹은 지금 설정 버전 것
        JsonNode launch = mapper.readTree(launch("AWS").andExpect(status().isCreated()).andReturn().getResponse()
                .getContentAsString(StandardCharsets.UTF_8));
        assertThat(launch.get("provider").stringValue()).isEqualTo("AWS");
        assertThat(launch.get("gpu_type").stringValue()).isEqualTo("g7e.4xlarge");
        AwsGpuApi.RunRequest run = api.runs.getFirst();
        assertThat(run.launchTemplateId()).isEqualTo("lt-0123456789abcdef0");
        assertThat(run.instanceType()).isEqualTo("g7e.4xlarge");
        assertThat(run.subnetId()).isEqualTo("subnet-0ddd3333eeee4444f");
        assertThat(run.securityGroupIds()).containsExactly("sg-0123456789abcdef0");

        // 켜는 중이면 다른 공급자로도 켜지 않는다(한 시점에 GPU 하나). RunPod 키만 있으면 되고 RunPod 은 부르지 않는다
        runpod.useEndpoints("http://127.0.0.1:1/v2", "http://127.0.0.1:1", "test-only-runpod-key");
        launch("RUNPOD").andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_ALREADY_ON"));
        launch(null).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_ALREADY_ON"));
        assertThat(api.runs).hasSize(1);
        mvc.perform(post("/api/gpu/stop").header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isNoContent());
        assertThat(api.terminated).containsExactly("i-0123456789abcdef0");
    }

    @Test
    void 공급자_없이_켜면_기본_공급자_AWS_로_켠다() throws Exception {
        awsProvider.useApi(api, enabled());
        api.zones.put("g7e.4xlarge", Set.of("ap-northeast-2a"));
        save(runpodSettingsWithAwsTemplate()).andExpect(status().isCreated());
        mvc.perform(get("/api/gpu/offers").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.provider").value("AWS"));
        launch().andExpect(status().isCreated()).andExpect(jsonPath("$.provider").value("AWS"))
                .andExpect(jsonPath("$.gpu_type").value("g7e.4xlarge")).andExpect(jsonPath("$.region").value("ap-northeast-2a"));
        assertThat(api.runs).hasSize(1);
    }

    // ---- 도우미

    /** 설정 공급자 RunPod(선호값은 RunPod 것) + AWS 시작 템플릿·서브넷·보안 그룹 */
    private ObjectNode runpodSettingsWithAwsTemplate() {
        ObjectNode body = awsSettings("lt-0123456789abcdef0", false);
        body.put("provider", "RUNPOD");
        body.putArray("preferred_gpu_types").add("NVIDIA H100 80GB HBM3");
        body.putArray("preferred_regions").add("AP-JP-1");
        body.put("runpod_template_id", "tpl-simai");
        return body;
    }

    private ResultActions launchCell(String provider, String gpuType, String region) throws Exception {
        ObjectNode body = mapper.createObjectNode();
        body.put("provider", provider);
        body.put("gpu_type", gpuType);
        body.put("region", region);
        return mvc.perform(post("/api/gpu/launches").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(body)));
    }

    private ResultActions launch(String provider) throws Exception {
        ObjectNode body = mapper.createObjectNode();
        if (provider != null) {
            body.put("provider", provider);
        }
        return mvc.perform(post("/api/gpu/launches").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(body)));
    }

    private GpuProperties.Aws enabled() {
        GpuProperties.Aws base = properties.aws();
        return new GpuProperties.Aws(true, base.region(), "", "", IMAGE, base.secretId(), base.launchTemplateVersion(), base.timeout(),
                base.pricingRegion(), base.onDemandPrices(), base.extraEnv());
    }

    private ObjectNode awsSettings(String launchTemplate, boolean spot) {
        ObjectNode body = mapper.createObjectNode();
        body.put("provider", "AWS");
        body.putArray("preferred_gpu_types").add(G6E).add(G7E);
        body.putArray("preferred_regions").add("ap-northeast-2a").add("ap-northeast-2b");
        body.put("cloud_type", "SECURE");
        body.put("interruptible", spot);
        body.putNull("max_price_per_hour_usd");
        body.put("auto_stop_idle_minutes", 30);
        body.putNull("runpod_template_id");
        if (launchTemplate == null) {
            body.putNull("aws_launch_template_id");
        } else {
            body.put("aws_launch_template_id", launchTemplate);
        }
        body.putArray("aws_subnet_ids").add("subnet-0aaa1111bbbb2222c").add("subnet-0ddd3333eeee4444f");
        body.putArray("aws_security_group_ids").add("sg-0123456789abcdef0");
        return body;
    }

    private ResultActions save(ObjectNode body) throws Exception {
        return mvc.perform(post("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(body)));
    }

    private ResultActions launch() throws Exception {
        return mvc.perform(post("/api/gpu/launches").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content("{}"));
    }

    private ResultActions current() throws Exception {
        return mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isOk());
    }

    private String jwt(Long userId) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(userId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, "ADMIN").build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
}
