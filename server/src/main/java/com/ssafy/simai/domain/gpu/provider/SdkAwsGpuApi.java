package com.ssafy.simai.domain.gpu.provider;

import com.ssafy.simai.global.properties.GpuProperties;
import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.core.exception.SdkClientException;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.ec2.Ec2Client;
import software.amazon.awssdk.services.ec2.model.DescribeInstanceTypeOfferingsRequest;
import software.amazon.awssdk.services.ec2.model.DescribeInstanceTypesRequest;
import software.amazon.awssdk.services.ec2.model.DescribeInstancesRequest;
import software.amazon.awssdk.services.ec2.model.DescribeSpotPriceHistoryRequest;
import software.amazon.awssdk.services.ec2.model.DescribeSubnetsRequest;
import software.amazon.awssdk.services.ec2.model.Filter;
import software.amazon.awssdk.services.ec2.model.GpuDeviceInfo;
import software.amazon.awssdk.services.ec2.model.HttpTokensState;
import software.amazon.awssdk.services.ec2.model.InstanceInterruptionBehavior;
import software.amazon.awssdk.services.ec2.model.InstanceMarketOptionsRequest;
import software.amazon.awssdk.services.ec2.model.InstanceMetadataEndpointState;
import software.amazon.awssdk.services.ec2.model.InstanceMetadataOptionsRequest;
import software.amazon.awssdk.services.ec2.model.LaunchTemplateSpecification;
import software.amazon.awssdk.services.ec2.model.LocationType;
import software.amazon.awssdk.services.ec2.model.MarketType;
import software.amazon.awssdk.services.ec2.model.Placement;
import software.amazon.awssdk.services.ec2.model.ResourceType;
import software.amazon.awssdk.services.ec2.model.RunInstancesRequest;
import software.amazon.awssdk.services.ec2.model.RunInstancesResponse;
import software.amazon.awssdk.services.ec2.model.ShutdownBehavior;
import software.amazon.awssdk.services.ec2.model.SpotInstanceType;
import software.amazon.awssdk.services.ec2.model.SpotMarketOptions;
import software.amazon.awssdk.services.ec2.model.SpotPrice;
import software.amazon.awssdk.services.ec2.model.Tag;
import software.amazon.awssdk.services.ec2.model.TagSpecification;
import software.amazon.awssdk.services.ec2.model.TerminateInstancesRequest;
import software.amazon.awssdk.services.pricing.PricingClient;
import software.amazon.awssdk.services.pricing.model.FilterType;
import software.amazon.awssdk.services.pricing.model.GetProductsRequest;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * AWS SDK 로 부르는 {@link AwsGpuApi}(S15P31A101-703). 클라이언트는 처음 부를 때 만든다(AWS 를 끈 서버는 만들지 않는다).
 * 자격 증명: 전용 IAM 사용자 키가 있으면 그것, 없으면 기본 자격 증명 체인(인스턴스 역할·AWS_* 환경 변수). 키·user-data 는 로그에 싣지 않는다.
 * 이 클래스는 시험에서 부르지 않는다(실제 AWS). 요청 모양은 {@link #runInstancesRequest(RunRequest)} 를 시험한다.
 */
final class SdkAwsGpuApi implements AwsGpuApi {

    private static final Logger log = LoggerFactory.getLogger(SdkAwsGpuApi.class);
    /** 자격 증명 확인 결과를 기억하는 시간(설정 화면이 열릴 때마다 체인을 다시 찾지 않게) */
    private static final Duration CREDENTIALS_TTL = Duration.ofMinutes(5);
    /** 용량 없음(같은 종류를 다른 가용 영역·종류로 다시 고르면 된다) */
    private static final Set<String> OUT_OF_STOCK = Set.of("InsufficientInstanceCapacity", "InsufficientCapacity",
            "InsufficientHostCapacity", "InsufficientReservedInstanceCapacity", "SpotMaxPriceTooLow", "InsufficientCapacityOnHost");
    private static final Set<String> THROTTLED = Set.of("RequestLimitExceeded", "Throttling", "ThrottlingException",
            "ServiceUnavailable", "Unavailable", "InternalError", "InternalFailure");
    private static final Set<String> NOT_FOUND = Set.of("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed");

    private final GpuProperties.Aws aws;
    private final ObjectMapper json = JsonMapper.builder().build();
    private volatile AwsCredentialsProvider credentials;
    private volatile Ec2Client ec2;
    private volatile PricingClient pricing;
    private volatile Instant credentialsCheckedAt = Instant.EPOCH;
    private volatile boolean credentialsOk;

    SdkAwsGpuApi(GpuProperties.Aws aws) {
        this.aws = aws;
    }

    @Override
    public boolean credentialsAvailable() {
        Instant now = Instant.now();
        if (credentialsCheckedAt.plus(CREDENTIALS_TTL).isAfter(now)) {
            return credentialsOk;
        }
        boolean ok;
        try {
            ok = credentials().resolveCredentials() != null;
        } catch (SdkException e) {
            log.warn("AWS 자격 증명을 찾지 못했다: {}", e.getClass().getSimpleName());
            ok = false;
        }
        credentialsOk = ok;
        credentialsCheckedAt = now;
        return ok;
    }

    @Override
    public Map<String, Set<String>> offeredZones(List<String> instanceTypes) throws GpuProviderException {
        try {
            Map<String, Set<String>> zones = new LinkedHashMap<>();
            ec2().describeInstanceTypeOfferingsPaginator(DescribeInstanceTypeOfferingsRequest.builder()
                            .locationType(LocationType.AVAILABILITY_ZONE)
                            .filters(Filter.builder().name("instance-type").values(instanceTypes).build()).build())
                    .instanceTypeOfferings()
                    .forEach(o -> zones.computeIfAbsent(o.instanceTypeAsString(), k -> new LinkedHashSet<>()).add(o.location()));
            return zones;
        } catch (SdkException e) {
            throw failure("가용 영역 조회", e);
        }
    }

    @Override
    public Map<String, GpuSpec> gpuSpecs(List<String> instanceTypes) throws GpuProviderException {
        Map<String, GpuSpec> specs = new HashMap<>();
        // 모르는 종류가 하나라도 있으면 묶음 전체가 거절되므로 하나씩 묻는다(후보는 10개 이하)
        for (String type : instanceTypes) {
            try {
                ec2().describeInstanceTypes(DescribeInstanceTypesRequest.builder().instanceTypesWithStrings(type).build())
                        .instanceTypes().forEach(info -> {
                            if (info.gpuInfo() == null || info.gpuInfo().gpus().isEmpty()) {
                                return;
                            }
                            GpuDeviceInfo gpu = info.gpuInfo().gpus().getFirst();
                            Integer memory = gpu.memoryInfo() == null || gpu.memoryInfo().sizeInMiB() == null ? null
                                    : Math.round(gpu.memoryInfo().sizeInMiB() / 1024f);
                            String name = ((gpu.manufacturer() == null ? "" : gpu.manufacturer() + " ") + gpu.name()).strip();
                            specs.put(info.instanceTypeAsString(), new GpuSpec(name, gpu.count() == null ? 1 : gpu.count(), memory));
                        });
            } catch (AwsServiceException e) {
                log.warn("AWS 인스턴스 종류 {} 정보 조회 실패: {}", type, code(e)); // 표시 정보일 뿐이라 계속한다
            } catch (SdkException e) {
                throw failure("인스턴스 종류 조회", e);
            }
        }
        return specs;
    }

    @Override
    public Map<String, String> subnetZones(List<String> subnetIds) throws GpuProviderException {
        try {
            Map<String, String> zones = new LinkedHashMap<>();
            ec2().describeSubnets(DescribeSubnetsRequest.builder().subnetIds(subnetIds).build()).subnets()
                    .forEach(s -> zones.put(s.subnetId(), s.availabilityZone()));
            return zones;
        } catch (SdkException e) {
            throw failure("서브넷 조회", e);
        }
    }

    @Override
    public Map<String, BigDecimal> spotPrices(List<String> instanceTypes) throws GpuProviderException {
        try {
            Map<String, SpotPrice> latest = new HashMap<>();
            ec2().describeSpotPriceHistoryPaginator(DescribeSpotPriceHistoryRequest.builder().instanceTypesWithStrings(instanceTypes)
                            .productDescriptions("Linux/UNIX").startTime(Instant.now()).build())
                    .spotPriceHistory().forEach(p -> latest.merge(p.instanceTypeAsString() + "\n" + p.availabilityZone(), p,
                            (a, b) -> a.timestamp() != null && b.timestamp() != null && b.timestamp().isAfter(a.timestamp()) ? b : a));
            Map<String, BigDecimal> prices = new HashMap<>();
            latest.forEach((k, p) -> {
                try {
                    prices.put(k, new BigDecimal(p.spotPrice()));
                } catch (NumberFormatException | NullPointerException e) {
                    // 모르는 가격은 비운다
                }
            });
            return prices;
        } catch (SdkException e) {
            throw failure("스팟 가격 조회", e);
        }
    }

    @Override
    public Optional<BigDecimal> onDemandPrice(String instanceType) throws GpuProviderException {
        try {
            List<String> products = pricing().getProducts(GetProductsRequest.builder().serviceCode("AmazonEC2").formatVersion("aws_v1")
                    .filters(term("instanceType", instanceType), term("regionCode", aws.region()), term("operatingSystem", "Linux"),
                            term("tenancy", "Shared"), term("preInstalledSw", "NA"), term("capacitystatus", "Used"),
                            term("licenseModel", "No License required"))
                    .maxResults(10).build()).priceList();
            return onDemandFromPriceList(json, products);
        } catch (SdkException e) {
            throw failure("온디맨드 가격 조회", e);
        }
    }

    /** Price List 문서(JSON 문자열)에서 terms.OnDemand.*.priceDimensions.*.pricePerUnit.USD 의 첫 양수 */
    static Optional<BigDecimal> onDemandFromPriceList(ObjectMapper json, List<String> products) {
        for (String product : products) {
            JsonNode onDemand;
            try {
                onDemand = json.readTree(product).path("terms").path("OnDemand");
            } catch (RuntimeException e) {
                continue;
            }
            for (JsonNode term : onDemand) {
                for (JsonNode dim : term.path("priceDimensions")) {
                    String usd = dim.path("pricePerUnit").path("USD").asString("");
                    try {
                        BigDecimal value = new BigDecimal(usd);
                        if (value.signum() > 0) {
                            return Optional.of(value);
                        }
                    } catch (NumberFormatException e) {
                        // 다음 항목
                    }
                }
            }
        }
        return Optional.empty();
    }

    @Override
    public String run(RunRequest request) throws GpuProviderException {
        try {
            RunInstancesResponse res = ec2().runInstances(runInstancesRequest(request));
            if (res.instances().isEmpty() || res.instances().getFirst().instanceId() == null) {
                throw new GpuProviderException("BAD_RESPONSE", "AWS RunInstances 응답에 인스턴스가 없다");
            }
            return res.instances().getFirst().instanceId();
        } catch (SdkException e) {
            throw failure("인스턴스 만들기", e);
        }
    }

    /** RunInstances 요청 모양(시험 대상). 공개 IP 는 서브넷 자동 할당(기본 VPC 는 켜져 있다)·시작 템플릿에 맡긴다 */
    static RunInstancesRequest runInstancesRequest(RunRequest r) {
        List<Tag> tags = new ArrayList<>();
        r.tags().forEach((k, v) -> tags.add(Tag.builder().key(k).value(v).build()));
        RunInstancesRequest.Builder b = RunInstancesRequest.builder()
                .launchTemplate(LaunchTemplateSpecification.builder().launchTemplateId(r.launchTemplateId())
                        .version(r.launchTemplateVersion()).build())
                .instanceType(r.instanceType())
                .minCount(1).maxCount(1)
                .userData(r.userDataBase64())
                .clientToken(r.clientToken())
                // 컨테이너(도커 브리지)가 IMDSv2 로 역할 자격을 받으려면 홉 2 가 필요하다. 토큰 필수(IMDSv1 끔)
                .metadataOptions(InstanceMetadataOptionsRequest.builder().httpTokens(HttpTokensState.REQUIRED)
                        .httpEndpoint(InstanceMetadataEndpointState.ENABLED).httpPutResponseHopLimit(2).build())
                // 안에서 꺼지면(shutdown) 지운다: 멈춘 인스턴스의 EBS 과금이 남지 않게
                .instanceInitiatedShutdownBehavior(ShutdownBehavior.TERMINATE)
                .tagSpecifications(TagSpecification.builder().resourceType(ResourceType.INSTANCE).tags(tags).build(),
                        TagSpecification.builder().resourceType(ResourceType.VOLUME).tags(tags).build());
        if (r.subnetId() != null) {
            b.subnetId(r.subnetId());
        } else {
            b.placement(Placement.builder().availabilityZone(r.availabilityZone()).build());
        }
        if (!r.securityGroupIds().isEmpty()) {
            b.securityGroupIds(r.securityGroupIds());
        }
        if (r.spot()) {
            b.instanceMarketOptions(InstanceMarketOptionsRequest.builder().marketType(MarketType.SPOT)
                    .spotOptions(SpotMarketOptions.builder().spotInstanceType(SpotInstanceType.ONE_TIME)
                            .instanceInterruptionBehavior(InstanceInterruptionBehavior.TERMINATE).build())
                    .build());
        }
        return b.build();
    }

    @Override
    public void terminate(String instanceId) throws GpuProviderException {
        try {
            ec2().terminateInstances(TerminateInstancesRequest.builder().instanceIds(instanceId).build());
        } catch (AwsServiceException e) {
            if (NOT_FOUND.contains(code(e))) {
                return; // 이미 없음도 같은 결과
            }
            throw failure("인스턴스 지우기", e);
        } catch (SdkException e) {
            throw failure("인스턴스 지우기", e);
        }
    }

    @Override
    public Optional<String> state(String instanceId) throws GpuProviderException {
        try {
            return ec2().describeInstances(DescribeInstancesRequest.builder().instanceIds(instanceId).build()).reservations().stream()
                    .flatMap(res -> res.instances().stream()).filter(i -> instanceId.equals(i.instanceId())).findFirst()
                    .map(i -> i.state() == null ? null : i.state().nameAsString());
        } catch (AwsServiceException e) {
            if (NOT_FOUND.contains(code(e))) {
                return Optional.empty();
            }
            throw failure("인스턴스 조회", e);
        } catch (SdkException e) {
            throw failure("인스턴스 조회", e);
        }
    }

    // ---------------------------------------------------------------- 내부

    private static software.amazon.awssdk.services.pricing.model.Filter term(String field, String value) {
        return software.amazon.awssdk.services.pricing.model.Filter.builder().type(FilterType.TERM_MATCH).field(field).value(value).build();
    }

    private AwsCredentialsProvider credentials() {
        AwsCredentialsProvider c = credentials;
        if (c == null) {
            synchronized (this) {
                if (credentials == null) {
                    credentials = aws.staticKey()
                            ? StaticCredentialsProvider.create(AwsBasicCredentials.create(aws.accessKey(), aws.secretKey()))
                            : DefaultCredentialsProvider.builder().build();
                }
                c = credentials;
            }
        }
        return c;
    }

    private Ec2Client ec2() {
        Ec2Client c = ec2;
        if (c == null) {
            synchronized (this) {
                if (ec2 == null) {
                    ec2 = Ec2Client.builder().region(Region.of(aws.region())).credentialsProvider(credentials())
                            .overrideConfiguration(o -> o.apiCallTimeout(aws.timeout())).build();
                }
                c = ec2;
            }
        }
        return c;
    }

    private PricingClient pricing() {
        PricingClient c = pricing;
        if (c == null) {
            synchronized (this) {
                if (pricing == null) {
                    pricing = PricingClient.builder().region(Region.of(aws.pricingRegion())).credentialsProvider(credentials())
                            .overrideConfiguration(o -> o.apiCallTimeout(aws.timeout())).build();
                }
                c = pricing;
            }
        }
        return c;
    }

    private static String code(AwsServiceException e) {
        return e.awsErrorDetails() == null || e.awsErrorDetails().errorCode() == null ? "" : e.awsErrorDetails().errorCode();
    }

    /** AWS 예외 → GpuProviderException. 문구에는 오류 코드·HTTP 상태만(요청 내용·자격 증명 없음) */
    static GpuProviderException failure(String what, SdkException e) {
        if (e instanceof AwsServiceException ase) {
            String code = code(ase);
            int status = ase.statusCode();
            log.warn("AWS {} 실패: {} (HTTP {})", what, code, status);
            if (OUT_OF_STOCK.contains(code)) {
                return new GpuProviderException("OUT_OF_STOCK", "AWS 에 그 인스턴스 종류·가용 영역 용량이 없다(" + code + ")");
            }
            if (status >= 500 || status == 429 || THROTTLED.contains(code)) {
                return new GpuProviderException("UNAVAILABLE", "AWS " + what + " 일시 실패(" + code + ", HTTP " + status + ")");
            }
            // VcpuLimitExceeded·InstanceLimitExceeded(서비스 할당량)·UnauthorizedOperation(IAM)·InvalidParameter* 등
            return new GpuProviderException("REJECTED", "AWS " + what + " 거절(" + code + ", HTTP " + status + ")");
        }
        String message = e.getMessage() == null ? "" : e.getMessage().toLowerCase(Locale.ROOT);
        if (e instanceof SdkClientException && message.contains("credentials")) {
            log.warn("AWS {} 실패: 자격 증명 없음", what);
            return new GpuProviderException("NOT_CONFIGURED", "AWS 자격 증명이 없다(인스턴스 역할 또는 SIMAI_GPU_AWS_ACCESS_KEY_ID)");
        }
        log.warn("AWS {} 연결 실패: {}", what, e.getClass().getSimpleName());
        return new GpuProviderException("UNAVAILABLE", "AWS " + what + " 연결 실패");
    }
}
