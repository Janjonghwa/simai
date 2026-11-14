package com.ssafy.simai.domain.gpu.provider;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.gpu.provider.GpuProvider.GpuOffer;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.InstanceState;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.LaunchSpec;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.Network;
import com.ssafy.simai.domain.gpu.provider.GpuProvider.OfferQuery;
import com.ssafy.simai.global.properties.GpuProperties;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * AWS 서울 GPU 공급자(S15P31A101-703). 가짜 AWS 로 재고·가격 표, 만들기 요청(시작 템플릿·서브넷·보안 그룹·스팟·태그·user-data 에 비밀 원문 없음),
 * 끄기, 상태 매핑, 설정·자격 증명 없을 때 "준비 안 됨"을 본다. 실제 AWS 를 부르지 않는다.
 */
class AwsGpuProviderTest {

    static final String IMAGE = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/simai/tier0@sha256:" + "a".repeat(64);
    static final String G6E = "g6e.2xlarge";
    static final String P5 = "p5.48xlarge";

    private final FakeAwsGpuApi api = new FakeAwsGpuApi();
    private AwsGpuProvider provider;

    static GpuProperties.Aws aws(boolean enabled, String image) {
        Map<String, String> extra = new LinkedHashMap<>();
        extra.put("SIMAI_TIER0_GPU_RESERVE_MB", "8192");
        extra.put("SIMAI_TIER0_PINNED_SPKI_SHA256", ""); // 빈 값은 넣지 않는다
        return new GpuProperties.Aws(enabled, "ap-northeast-2", "", "", image, "simai/gpu-boot", null, null, null, null, extra);
    }

    @BeforeEach
    void setUp() {
        GpuProperties.Aws config = aws(true, IMAGE);
        provider = new AwsGpuProvider(new GpuProperties(null, config, null, null, null, null, null));
        provider.useApi(api, config);
        api.zones.put(G6E, Set.of("ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"));
        api.zones.put(P5, Set.of("ap-northeast-2a"));
        api.specs.put(G6E, new AwsGpuApi.GpuSpec("NVIDIA L40S", 1, 48));
        api.subnets.put("subnet-0aaa1111bbbb2222c", "ap-northeast-2a");
        api.subnets.put("subnet-0ddd3333eeee4444f", "ap-northeast-2b");
        api.onDemand.put(G6E, new BigDecimal("2.7565"));
        api.pricingFails.add(P5);
    }

    @Test
    void 꺼져_있거나_이미지_자격_증명이_없으면_준비_안_됨이고_AWS_를_부르지_않는다() {
        assertThat(provider.configured()).isTrue();

        provider.useApi(api, aws(false, IMAGE));
        assertThat(provider.configured()).isFalse();
        assertThatThrownBy(() -> provider.offers(query(false))).isInstanceOfSatisfying(GpuProviderException.class,
                e -> assertThat(e.code()).isEqualTo("NOT_CONFIGURED"));
        assertThatThrownBy(() -> provider.terminate("i-0123456789abcdef0")).isInstanceOfSatisfying(GpuProviderException.class,
                e -> assertThat(e.code()).isEqualTo("NOT_CONFIGURED"));

        provider.useApi(api, aws(true, ""));
        assertThat(provider.configured()).isFalse();
        assertThatThrownBy(() -> provider.create(spec(false, List.of()))).isInstanceOfSatisfying(GpuProviderException.class,
                e -> assertThat(e.code()).isEqualTo("NOT_CONFIGURED"));

        provider.useApi(api, aws(true, IMAGE));
        api.credentials = false;
        assertThat(provider.configured()).isFalse();
        assertThat(api.calls).isEmpty();
    }

    @Test
    void 온디맨드_재고_표는_파는_가용_영역만_싣고_서브넷_없는_곳은_NONE_가격은_Price_List_없으면_설정_표다() throws Exception {
        List<GpuOffer> offers = provider.offers(query(false));
        // RunPod 이름(인스턴스 종류 꼴이 아님)은 묻지 않는다
        assertThat(api.callsStartingWith("offeredZones")).containsExactly("offeredZones [" + G6E + ", " + P5 + "]");
        assertThat(offers).extracting(o -> o.gpuType() + "/" + o.region() + "/" + o.stock() + "/" + o.pricePerHourUsd()).containsExactly(
                G6E + "/ap-northeast-2a/UNKNOWN/2.7565", G6E + "/ap-northeast-2b/UNKNOWN/2.7565", G6E + "/ap-northeast-2c/NONE/2.7565",
                P5 + "/ap-northeast-2a/UNKNOWN/75.9552");
        assertThat(offers.getFirst().gpuName()).isEqualTo(G6E + " · NVIDIA L40S ×1");
        assertThat(offers.getFirst().memoryGb()).isEqualTo(48);
        assertThat(offers.getLast().gpuName()).isEqualTo(P5); // GPU 정보를 모르면 종류 이름만

        // 온디맨드 가격은 기억한다(패널을 열 때마다 Price List 를 부르지 않는다)
        provider.offers(query(false));
        assertThat(api.callsStartingWith("onDemandPrice " + G6E)).hasSize(1);
    }

    @Test
    void 스팟_재고_표는_가용_영역별_지금_스팟_가격이다() throws Exception {
        api.spot.put(G6E + "\nap-northeast-2a", new BigDecimal("1.4211"));
        api.spot.put(G6E + "\nap-northeast-2b", new BigDecimal("1.5100"));
        List<GpuOffer> offers = provider.offers(query(true));
        assertThat(offers).filteredOn(o -> o.gpuType().equals(G6E)).extracting(GpuOffer::pricePerHourUsd)
                .containsExactly(new BigDecimal("1.4211"), new BigDecimal("1.5100"), null);
        assertThat(api.callsStartingWith("onDemandPrice")).isEmpty();
    }

    @Test
    void 만들기는_시작_템플릿_서브넷_보안_그룹_스팟_태그로_부르고_user_data_에는_비밀_원문이_없다() throws Exception {
        GpuProvider.CreatedInstance created = provider.create(spec(true, List.of("subnet-0aaa1111bbbb2222c", "subnet-0ddd3333eeee4444f")));
        assertThat(created.instanceId()).isEqualTo("i-0123456789abcdef0");

        AwsGpuApi.RunRequest run = api.runs.getFirst();
        assertThat(run.launchTemplateId()).isEqualTo("lt-0123456789abcdef0");
        assertThat(run.launchTemplateVersion()).isEqualTo("$Default");
        assertThat(run.instanceType()).isEqualTo(G6E);
        assertThat(run.availabilityZone()).isEqualTo("ap-northeast-2b");
        assertThat(run.subnetId()).isEqualTo("subnet-0ddd3333eeee4444f"); // 그 가용 영역의 서브넷
        assertThat(run.securityGroupIds()).containsExactly("sg-0123456789abcdef0");
        assertThat(run.spot()).isTrue();
        assertThat(run.tags()).containsEntry("simai:managed", "gpu").containsEntry("simai:launch-id", LAUNCH_ID)
                .containsEntry("Name", "simai-gpu-11111111");
        assertThat(run.clientToken()).isEqualTo(LAUNCH_ID);

        String script = new String(Base64.getDecoder().decode(run.userDataBase64()), StandardCharsets.UTF_8);
        assertThat(script).startsWith("#!/bin/bash\n").contains("IMAGE='" + IMAGE + "'")
                .contains("\nSIMAI_GPU_BOOT_ID=gpu-boot-aws\n").contains("\nSIMAI_EC2_BASE_URL=https://simai.example\n")
                .contains("\nSIMAI_GPU_PROVIDER=AWS\n").contains("\nSIMAI_PRICE_PER_HOUR_USD=1.4211\n")
                .contains("\nSIMAI_GPU_LAUNCH_ID=" + LAUNCH_ID + "\n")
                .contains("\nSIMAI_SECRETS_SOURCE=aws-secretsmanager\n").contains("\nSIMAI_AWS_SECRET_ID=simai/gpu-boot\n")
                .contains("\nSIMAI_AWS_REGION=ap-northeast-2\n").contains("\nSIMAI_IMAGE_DIGEST=sha256:" + "a".repeat(64) + "\n")
                .contains("\nSIMAI_PUBLIC_PORT_17000=17000\n").contains("\nSIMAI_PUBLIC_PORT_17001=17001\n")
                .contains("\nSIMAI_TIER0_GPU_RESERVE_MB=8192\n")
                .contains("-p 17000:17000 -p 17001:17001").contains("--gpus all")
                .contains("SIMAI_INSTANCE_ID=%s\\nSIMAI_REGION=%s\\nSIMAI_PUBLIC_HOST=%s");
        // 비밀 원문·참조 글자가 없다: 비밀 이름은 줄로도 없고, 인스턴스가 Secrets Manager 에서 받는다
        assertThat(script).doesNotContain("SIMAI_GPU_BOOT_SECRET=").doesNotContain("SIMAI_TIER0_TLS_PRIVATE_KEY=")
                .doesNotContain("HF_TOKEN=").doesNotContain("RUNPOD_SECRET").doesNotContain("SIMAI_TIER0_PINNED_SPKI_SHA256=");
        // 환경 파일 블록은 따옴표 친 heredoc 이라 $·백틱이 풀리지 않는다
        assertThat(script).contains("<<'SIMAI_ENV_END'\n");
    }

    @Test
    void 서브넷이_없으면_가용_영역의_기본_서브넷이고_그_영역에_서브넷이_없으면_거절한다() throws Exception {
        provider.create(spec(false, List.of()));
        AwsGpuApi.RunRequest run = api.runs.getFirst();
        assertThat(run.subnetId()).isNull();
        assertThat(run.availabilityZone()).isEqualTo("ap-northeast-2b");
        assertThat(run.spot()).isFalse();

        assertThatThrownBy(() -> provider.create(spec(false, List.of("subnet-0aaa1111bbbb2222c"))))
                .isInstanceOfSatisfying(GpuProviderException.class, e -> assertThat(e.code()).isEqualTo("REJECTED"));
        LaunchSpec noTemplate = new LaunchSpec("simai-gpu-1", null, G6E, "ap-northeast-2b", "SECURE", false, 1, env(), secretEnv(),
                Network.NONE);
        assertThatThrownBy(() -> provider.create(noTemplate)).isInstanceOfSatisfying(GpuProviderException.class,
                e -> assertThat(e.code()).isEqualTo("NOT_CONFIGURED"));
        // 비밀처럼 보이는 이름이 평문 환경 변수로 오면 넣지 않는다
        Map<String, String> leaky = env();
        leaky.put("HF_TOKEN", "hf_should_never_be_here");
        LaunchSpec bad = new LaunchSpec("simai-gpu-1", "lt-0123456789abcdef0", G6E, "ap-northeast-2b", "SECURE", false, 1, leaky,
                secretEnv(), Network.NONE);
        assertThatThrownBy(() -> provider.create(bad)).isInstanceOfSatisfying(GpuProviderException.class,
                e -> assertThat(e.code()).isEqualTo("REJECTED"));
        assertThat(api.runs).hasSize(1);
    }

    @Test
    void 끄기와_상태는_인스턴스_ID_로_부르고_상태를_네_가지로_바꾼다() throws Exception {
        provider.terminate("i-0123456789abcdef0");
        assertThat(api.terminated).containsExactly("i-0123456789abcdef0");
        assertThatThrownBy(() -> provider.terminate("pod-abc")).isInstanceOfSatisfying(GpuProviderException.class,
                e -> assertThat(e.code()).isEqualTo("REJECTED"));

        Map<String, InstanceState> expected = Map.of("pending", InstanceState.RUNNING, "running", InstanceState.RUNNING,
                "stopping", InstanceState.STOPPED, "stopped", InstanceState.STOPPED, "shutting-down", InstanceState.GONE,
                "terminated", InstanceState.GONE, "rebooting?", InstanceState.UNKNOWN);
        for (Map.Entry<String, InstanceState> e : expected.entrySet()) {
            api.states.put("i-0aaaaaaaaaaaaaaaa", e.getKey());
            assertThat(provider.state("i-0aaaaaaaaaaaaaaaa")).as(e.getKey()).isEqualTo(e.getValue());
        }
        assertThat(provider.state("i-0bbbbbbbbbbbbbbbb")).isEqualTo(InstanceState.GONE); // DescribeInstances 에 없음
    }

    // ---- 도우미

    private static final String LAUNCH_ID = "11111111-2222-4333-8444-555555555555";

    private static OfferQuery query(boolean spot) {
        return new OfferQuery("SECURE", 1, List.of(G6E, P5, "NVIDIA H100 80GB HBM3"), List.of("ap-northeast-2a", "ap-northeast-2b"), spot,
                new Network(List.of("subnet-0aaa1111bbbb2222c", "subnet-0ddd3333eeee4444f"), List.of()));
    }

    private static Map<String, String> env() {
        Map<String, String> env = new LinkedHashMap<>();
        env.put("SIMAI_GPU_BOOT_ID", "gpu-boot-aws");
        env.put("SIMAI_EC2_BASE_URL", "https://simai.example");
        env.put("SIMAI_GPU_PROVIDER", "AWS");
        env.put("SIMAI_PRICE_PER_HOUR_USD", "1.4211");
        env.put("SIMAI_GPU_LAUNCH_ID", LAUNCH_ID);
        return env;
    }

    private static Map<String, String> secretEnv() {
        return Map.of("SIMAI_GPU_BOOT_SECRET", "simai_gpu_boot_secret", "SIMAI_TIER0_TLS_PRIVATE_KEY", "simai_tier0_tls_key",
                "HF_TOKEN", "hf_token");
    }

    private static LaunchSpec spec(boolean spot, List<String> subnets) {
        return new LaunchSpec("simai-gpu-11111111", "lt-0123456789abcdef0", G6E, "ap-northeast-2b", "SECURE", spot, 1, env(), secretEnv(),
                new Network(subnets, List.of("sg-0123456789abcdef0")));
    }
}
