package com.ssafy.simai.domain.gpu.provider;

import com.ssafy.simai.global.properties.GpuProperties;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * AWS 서울 GPU 공급자(S15P31A101-703, 설계 docs/design/S15P31A101-703-aws-gpu-provider.md). 기본 꺼짐(simai.gpu.aws.enabled).
 * <ul>
 * <li>재고·가격: 인스턴스 종류(설정의 선호 GPU 칸) × 가용 영역(설정의 선호 지역 칸). DescribeInstanceTypeOfferings 로 파는 곳만 싣고,
 * 실시간 재고 API 가 없어 stock 은 UNKNOWN(만들어 봐야 안다, 용량 없음 거절은 OUT_OF_STOCK)이다. 설정한 서브넷이 없는 가용 영역은 NONE.
 * 가격은 온디맨드면 Price List(6시간 기억, 못 읽으면 설정 표), 스팟이면 DescribeSpotPriceHistory 의 가용 영역별 지금 가격이다.</li>
 * <li>만들기: RunInstances(시작 템플릿 + 인스턴스 종류 + 가용 영역의 서브넷 + 보안 그룹 + 스팟 여부 + 태그 + user-data).
 * user-data 는 같은 GPU 이미지를 docker 로 띄우는 스크립트이고 G2 부팅 환경 변수만 담는다. 부팅 비밀값·TLS 키·HF 토큰은 원문을 넣지 않고
 * 인스턴스가 역할(IMDSv2)로 Secrets Manager 에서 받는다(SIMAI_SECRETS_SOURCE=aws-secretsmanager, -700 simai-gpu-boot).</li>
 * <li>끄기: TerminateInstances(이미 없으면 성공). 상태: DescribeInstances(pending·running → RUNNING, stopping·stopped → STOPPED,
 * shutting-down·terminated·없음 → GONE).</li>
 * </ul>
 * 공개 IP·직접 포트라 인스턴스는 G1 등록에 SIMAI_PUBLIC_HOST·SIMAI_PUBLIC_PORT_&lt;n&gt;(듣는 포트 그대로)를 싣는다.
 */
@Component
public class AwsGpuProvider implements GpuProvider {

    public static final String ID = "AWS";
    private static final Logger log = LoggerFactory.getLogger(AwsGpuProvider.class);
    static final Pattern INSTANCE_TYPE = Pattern.compile("^[a-z][a-z0-9-]*\\.[a-z0-9-]+$");
    static final Pattern LAUNCH_TEMPLATE = Pattern.compile("^lt-[0-9a-f]{8,17}$");
    private static final Pattern INSTANCE_ID = Pattern.compile("^i-[0-9a-f]{8,17}$");
    /** ECR 등 레지스트리 이미지(태그·다이제스트). 셸 따옴표 안에 그대로 넣으므로 안전한 글자만 */
    private static final Pattern IMAGE = Pattern.compile("^[a-z0-9.-]+(:[0-9]{1,5})?/[a-z0-9._/-]+(:[A-Za-z0-9._-]{1,128})?(@sha256:[0-9a-f]{64})?$");
    private static final Pattern DIGEST = Pattern.compile("@(sha256:[0-9a-f]{64})$");
    private static final Pattern ENV_NAME = Pattern.compile("^[A-Z][A-Z0-9_]{0,63}$");
    /** 비밀로 보이는 이름은 user-data 에 넣지 않는다(이 두 이름은 비밀 원문이 아니라 출처·이름이다) */
    private static final Pattern SECRET_LIKE = Pattern.compile("TOKEN|SECRET|PASSW|PRIVATE|API_KEY|CREDENTIAL");
    private static final Set<String> SECRET_LIKE_ALLOWED = Set.of("SIMAI_SECRETS_SOURCE", "SIMAI_AWS_SECRET_ID");
    private static final String ENV_END = "SIMAI_ENV_END";
    /** user-data 상한(AWS: Base64 전 16 KB) */
    private static final int MAX_USER_DATA = 16 * 1024;
    private static final int CONTROL_PORT = 17000;
    private static final int VIDEO_PORT = 17001;
    private static final Duration PRICE_TTL = Duration.ofHours(6);
    private static final Duration PRICE_FALLBACK_TTL = Duration.ofMinutes(30);

    private volatile GpuProperties.Aws aws;
    private volatile AwsGpuApi api;
    private final Map<String, CachedPrice> onDemand = new ConcurrentHashMap<>();

    private record CachedPrice(BigDecimal price, Instant until) {
    }

    public AwsGpuProvider(GpuProperties properties) {
        this.aws = properties.aws();
        this.api = new SdkAwsGpuApi(properties.aws());
    }

    /** 시험에서 가짜 AWS 를 쓸 때만 부른다(운영은 설정값 + AWS SDK) */
    public void useApi(AwsGpuApi api, GpuProperties.Aws aws) {
        this.api = api;
        this.aws = aws;
        onDemand.clear();
    }

    @Override
    public String id() {
        return ID;
    }

    /** 켜져 있고(enabled) 이미지가 있고 자격 증명을 찾을 수 있어야 한다. 아니면 설정 화면·재고 표에 "준비 안 됨" */
    @Override
    public boolean configured() {
        return aws.enabled() && IMAGE.matcher(aws.image()).matches() && api.credentialsAvailable();
    }

    @Override
    public List<GpuOffer> offers(OfferQuery query) throws GpuProviderException {
        requireLaunchable();
        List<String> types = query.gpuTypes().stream().filter(t -> INSTANCE_TYPE.matcher(t).matches()).distinct().toList();
        if (types.isEmpty()) {
            return List.of();
        }
        Map<String, Set<String>> offered = api.offeredZones(types);
        Set<String> usable = null;
        if (!query.network().subnetIds().isEmpty()) {
            usable = new HashSet<>(api.subnetZones(query.network().subnetIds()).values());
        }
        Map<String, AwsGpuApi.GpuSpec> specs = api.gpuSpecs(types);
        Map<String, BigDecimal> spot = query.interruptible() ? api.spotPrices(types) : Map.of();
        List<GpuOffer> offers = new ArrayList<>();
        for (String type : types) {
            BigDecimal onDemandPrice = query.interruptible() ? null : onDemandPrice(type);
            AwsGpuApi.GpuSpec spec = specs.get(type);
            String name = spec == null ? type : type + " · " + spec.gpuName() + " ×" + spec.gpuCount();
            Integer memory = spec == null ? null : spec.gpuMemoryGb();
            for (String zone : new TreeSet<>(offered.getOrDefault(type, Set.of()))) {
                if (!zone.startsWith(aws.region())) {
                    continue;
                }
                String stock = usable == null || usable.contains(zone) ? "UNKNOWN" : "NONE";
                BigDecimal price = query.interruptible() ? spot.get(type + "\n" + zone) : onDemandPrice;
                offers.add(new GpuOffer(type, name, memory, zone, zone, stock, price));
            }
        }
        return offers;
    }

    @Override
    public CreatedInstance create(LaunchSpec spec) throws GpuProviderException {
        requireLaunchable();
        if (spec.templateId() == null || !LAUNCH_TEMPLATE.matcher(spec.templateId()).matches()) {
            throw new GpuProviderException("NOT_CONFIGURED", "AWS 시작 템플릿 ID(lt-…)가 설정되지 않았다");
        }
        if (!INSTANCE_TYPE.matcher(spec.gpuType()).matches() || !spec.region().startsWith(aws.region())) {
            throw new GpuProviderException("REJECTED", "AWS 인스턴스 종류·가용 영역 형식이 틀리다");
        }
        String subnet = null;
        if (!spec.network().subnetIds().isEmpty()) {
            Map<String, String> zones = api.subnetZones(spec.network().subnetIds());
            subnet = spec.network().subnetIds().stream().filter(s -> spec.region().equals(zones.get(s))).findFirst()
                    .orElseThrow(() -> new GpuProviderException("REJECTED", "가용 영역 " + spec.region() + " 에 설정한 서브넷이 없다"));
        }
        byte[] script = userData(spec).getBytes(StandardCharsets.UTF_8);
        if (script.length > MAX_USER_DATA) {
            throw new GpuProviderException("REJECTED", "user-data 가 16 KB 를 넘는다");
        }
        String launchId = spec.plainEnv().getOrDefault("SIMAI_GPU_LAUNCH_ID", spec.name());
        Map<String, String> tags = new LinkedHashMap<>();
        tags.put("Name", spec.name());
        // IAM 정책이 이 태그가 붙은 인스턴스만 지우게 묶는다(설계 문서 IAM)
        tags.put("simai:managed", "gpu");
        tags.put("simai:launch-id", launchId);
        String id = api.run(new AwsGpuApi.RunRequest(spec.templateId(), aws.launchTemplateVersion(), spec.gpuType(), spec.region(), subnet,
                spec.network().securityGroupIds(), spec.interruptible(), Base64.getEncoder().encodeToString(script), tags,
                launchId.length() > 64 ? launchId.substring(0, 64) : launchId));
        log.info("AWS 인스턴스 {} 만듦: {} / {}, 스팟 {}", id, spec.gpuType(), spec.region(), spec.interruptible());
        return new CreatedInstance(id, null);
    }

    @Override
    public void terminate(String instanceId) throws GpuProviderException {
        requireEnabled();
        api.terminate(checkedId(instanceId));
    }

    @Override
    public InstanceState state(String instanceId) throws GpuProviderException {
        requireEnabled();
        Optional<String> state = api.state(checkedId(instanceId));
        if (state.isEmpty()) {
            return InstanceState.GONE;
        }
        return switch (state.get()) {
            case "pending", "running" -> InstanceState.RUNNING;
            case "stopping", "stopped" -> InstanceState.STOPPED;
            case "shutting-down", "terminated" -> InstanceState.GONE;
            default -> InstanceState.UNKNOWN;
        };
    }

    /**
     * 인스턴스 부팅 스크립트(cloud-init user-data, 비밀 원문 없음). 호스트(Deep Learning AMI: NVIDIA 드라이버·Docker·nvidia-container-toolkit·
     * aws cli)에서 IMDSv2 로 인스턴스 ID·가용 영역·공개 IP 를 얻고, 이미지가 없으면 받고(ECR 이면 역할로 로그인), 환경 파일(600)로 컨테이너를 띄운다.
     * 컨테이너 기본 CMD(simai-bootstrap auto)가 SIMAI_SECRETS_SOURCE 를 보고 simai-gpu-boot 로 넘어가 비밀 값을 Secrets Manager 에서 받는다.
     */
    String userData(LaunchSpec spec) throws GpuProviderException {
        Map<String, String> env = new LinkedHashMap<>(spec.plainEnv());
        env.put("SIMAI_GPU_PROVIDER", ID);
        if (!spec.secretEnv().isEmpty()) {
            env.put("SIMAI_SECRETS_SOURCE", "aws-secretsmanager");
            env.put("SIMAI_AWS_SECRET_ID", aws.secretId());
            env.put("SIMAI_AWS_REGION", aws.region());
        }
        Matcher digest = DIGEST.matcher(aws.image());
        if (digest.find()) {
            env.put("SIMAI_IMAGE_DIGEST", digest.group(1));
        }
        // 공개 IP·직접 포트: 듣는 포트 그대로 밖에서 붙는다(-700 register_facts)
        env.put("SIMAI_PUBLIC_PORT_" + CONTROL_PORT, String.valueOf(CONTROL_PORT));
        env.put("SIMAI_PUBLIC_PORT_" + VIDEO_PORT, String.valueOf(VIDEO_PORT));
        aws.extraEnv().forEach((k, v) -> {
            if (v != null && !v.isBlank()) {
                env.putIfAbsent(k, v.strip());
            }
        });
        StringBuilder lines = new StringBuilder();
        for (Map.Entry<String, String> e : env.entrySet()) {
            String name = e.getKey();
            String value = e.getValue() == null ? "" : e.getValue();
            if (!ENV_NAME.matcher(name).matches() || spec.secretEnv().containsKey(name)
                    || SECRET_LIKE.matcher(name).find() && !SECRET_LIKE_ALLOWED.contains(name)) {
                throw new GpuProviderException("REJECTED", "user-data 에 넣을 수 없는 환경 변수 이름: " + name);
            }
            if (value.indexOf('\n') >= 0 || value.indexOf('\r') >= 0 || value.indexOf('\0') >= 0 || value.contains(ENV_END)) {
                throw new GpuProviderException("REJECTED", "환경 변수 " + name + " 값에 줄바꿈이 있다");
            }
            lines.append(name).append('=').append(value).append('\n');
        }
        return """
                #!/bin/bash
                # simai GPU 자동 부팅(S15P31A101-703). EC2 서버가 켤 때마다 만든다. 비밀 값 없음: 부팅 비밀값·Tier 0 TLS 키·HF 토큰은
                # 컨테이너(simai-gpu-boot)가 인스턴스 역할로 Secrets Manager 에서 받는다.
                set -euo pipefail
                exec >>/var/log/simai-gpu-user-data.log 2>&1
                IMAGE='%s'
                IMDS=http://169.254.169.254
                tok=$(curl -fsS -X PUT "$IMDS/latest/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
                md() { curl -fsS -H "X-aws-ec2-metadata-token: $tok" "$IMDS/latest/meta-data/$1"; }
                INSTANCE_ID=$(md instance-id)
                ZONE=$(md placement/availability-zone)
                PUBLIC_IP=$(md public-ipv4 || true)
                [ -n "$PUBLIC_IP" ] || echo "simai: 공개 IPv4 가 없다(서브넷 자동 할당 확인). 등록하지 못한다"
                umask 077
                ENV_FILE=/run/simai-gpu.env
                cat > "$ENV_FILE" <<'%s'
                %s%s
                printf 'SIMAI_INSTANCE_ID=%%s\\nSIMAI_REGION=%%s\\nSIMAI_PUBLIC_HOST=%%s\\n' "$INSTANCE_ID" "$ZONE" "$PUBLIC_IP" >> "$ENV_FILE"
                if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
                  case "$IMAGE" in
                    *.dkr.ecr.*.amazonaws.com/*)
                      REGISTRY=${IMAGE%%%%/*}
                      ECR_REGION=$(echo "$REGISTRY" | cut -d. -f4)
                      aws ecr get-login-password --region "$ECR_REGION" | docker login --username AWS --password-stdin "$REGISTRY" ;;
                  esac
                  for i in 1 2 3; do docker pull "$IMAGE" && break; sleep 15; done
                fi
                docker rm -f simai-gpu >/dev/null 2>&1 || true
                docker run -d --name simai-gpu --restart no --gpus all --ipc host \\
                  -p %d:%d -p %d:%d --env-file "$ENV_FILE" "$IMAGE"
                echo "simai: 컨테이너 시작 $(date -u +%%FT%%TZ)"
                """.formatted(aws.image(), ENV_END, lines, ENV_END, CONTROL_PORT, CONTROL_PORT, VIDEO_PORT, VIDEO_PORT);
    }

    // ---------------------------------------------------------------- 내부

    private BigDecimal onDemandPrice(String type) {
        Instant now = Instant.now();
        CachedPrice cached = onDemand.get(type);
        if (cached != null && cached.until().isAfter(now)) {
            return cached.price();
        }
        BigDecimal price = null;
        Duration ttl = PRICE_TTL;
        try {
            price = api.onDemandPrice(type).orElse(null);
        } catch (GpuProviderException e) {
            log.warn("AWS {} 온디맨드 가격을 Price List 에서 읽지 못해 설정 표 값을 쓴다: {}", type, e.code());
        }
        if (price == null) {
            price = aws.onDemandPrices().get(type);
            ttl = PRICE_FALLBACK_TTL;
        }
        onDemand.put(type, new CachedPrice(price, now.plus(ttl)));
        return price;
    }

    private void requireEnabled() throws GpuProviderException {
        if (!aws.enabled()) {
            throw new GpuProviderException("NOT_CONFIGURED", "AWS GPU 가 꺼져 있다(SIMAI_GPU_AWS_ENABLED)");
        }
    }

    private void requireLaunchable() throws GpuProviderException {
        requireEnabled();
        if (!IMAGE.matcher(aws.image()).matches()) {
            throw new GpuProviderException("NOT_CONFIGURED", "AWS GPU 이미지(SIMAI_GPU_AWS_IMAGE)가 없거나 형식이 틀리다");
        }
    }

    private static String checkedId(String instanceId) throws GpuProviderException {
        if (instanceId == null || !INSTANCE_ID.matcher(instanceId).matches()) {
            throw new GpuProviderException("REJECTED", "AWS 인스턴스 ID 형식이 아니다");
        }
        return instanceId;
    }
}
