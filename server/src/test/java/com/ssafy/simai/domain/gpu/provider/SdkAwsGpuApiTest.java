package com.ssafy.simai.domain.gpu.provider;

import static org.assertj.core.api.Assertions.assertThat;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.awscore.exception.AwsErrorDetails;
import software.amazon.awssdk.core.exception.SdkClientException;
import software.amazon.awssdk.services.ec2.model.Ec2Exception;
import software.amazon.awssdk.services.ec2.model.HttpTokensState;
import software.amazon.awssdk.services.ec2.model.InstanceInterruptionBehavior;
import software.amazon.awssdk.services.ec2.model.MarketType;
import software.amazon.awssdk.services.ec2.model.ResourceType;
import software.amazon.awssdk.services.ec2.model.RunInstancesRequest;
import software.amazon.awssdk.services.ec2.model.ShutdownBehavior;
import software.amazon.awssdk.services.ec2.model.SpotInstanceType;
import tools.jackson.databind.json.JsonMapper;

/** AWS SDK 요청 모양·가격 문서 읽기·오류 코드 매핑(S15P31A101-703). 네트워크를 쓰지 않는다 */
class SdkAwsGpuApiTest {

    @Test
    void RunInstances_요청은_시작_템플릿_스팟_IMDSv2_홉2_종료시_지우기_태그를_싣는다() {
        AwsGpuApi.RunRequest run = new AwsGpuApi.RunRequest("lt-0123456789abcdef0", "$Default", "g6e.2xlarge", "ap-northeast-2b",
                "subnet-0ddd3333eeee4444f", List.of("sg-0123456789abcdef0"), true, "IyEvYmluL2Jhc2g=",
                Map.of("simai:managed", "gpu"), "launch-1");
        RunInstancesRequest req = SdkAwsGpuApi.runInstancesRequest(run);
        assertThat(req.launchTemplate().launchTemplateId()).isEqualTo("lt-0123456789abcdef0");
        assertThat(req.launchTemplate().version()).isEqualTo("$Default");
        assertThat(req.instanceTypeAsString()).isEqualTo("g6e.2xlarge");
        assertThat(req.minCount()).isEqualTo(1);
        assertThat(req.maxCount()).isEqualTo(1);
        assertThat(req.subnetId()).isEqualTo("subnet-0ddd3333eeee4444f");
        assertThat(req.placement()).isNull();
        assertThat(req.securityGroupIds()).containsExactly("sg-0123456789abcdef0");
        assertThat(req.userData()).isEqualTo("IyEvYmluL2Jhc2g=");
        assertThat(req.clientToken()).isEqualTo("launch-1");
        assertThat(req.metadataOptions().httpTokens()).isEqualTo(HttpTokensState.REQUIRED);
        assertThat(req.metadataOptions().httpPutResponseHopLimit()).isEqualTo(2);
        assertThat(req.instanceInitiatedShutdownBehavior()).isEqualTo(ShutdownBehavior.TERMINATE);
        assertThat(req.instanceMarketOptions().marketType()).isEqualTo(MarketType.SPOT);
        assertThat(req.instanceMarketOptions().spotOptions().spotInstanceType()).isEqualTo(SpotInstanceType.ONE_TIME);
        assertThat(req.instanceMarketOptions().spotOptions().instanceInterruptionBehavior()).isEqualTo(InstanceInterruptionBehavior.TERMINATE);
        assertThat(req.tagSpecifications()).extracting(t -> t.resourceType()).containsExactly(ResourceType.INSTANCE, ResourceType.VOLUME);
        assertThat(req.tagSpecifications().getFirst().tags()).extracting(t -> t.key() + "=" + t.value()).containsExactly("simai:managed=gpu");

        // 서브넷이 없으면 가용 영역 지정(기본 VPC), 온디맨드면 시장 옵션 없음, 보안 그룹이 없으면 시작 템플릿의 것
        RunInstancesRequest plain = SdkAwsGpuApi.runInstancesRequest(new AwsGpuApi.RunRequest("lt-0123456789abcdef0", "$Default",
                "g6e.2xlarge", "ap-northeast-2a", null, List.of(), false, "eA==", Map.of(), "launch-2"));
        assertThat(plain.subnetId()).isNull();
        assertThat(plain.placement().availabilityZone()).isEqualTo("ap-northeast-2a");
        assertThat(plain.instanceMarketOptions()).isNull();
        assertThat(plain.hasSecurityGroupIds()).isFalse();
    }

    @Test
    void Price_List_문서에서_온디맨드_시간당_USD_를_읽는다() {
        String doc = """
                {"product":{"attributes":{"instanceType":"g6e.2xlarge","regionCode":"ap-northeast-2"}},
                 "terms":{"OnDemand":{"ABC.JRTCKXETXF":{"priceDimensions":{"ABC.JRTCKXETXF.6YS6EN2CT7":
                   {"unit":"Hrs","pricePerUnit":{"USD":"2.7565000000"}}}}}}}""";
        assertThat(SdkAwsGpuApi.onDemandFromPriceList(JsonMapper.builder().build(), List.of("not json", doc)))
                .contains(new BigDecimal("2.7565000000"));
        assertThat(SdkAwsGpuApi.onDemandFromPriceList(JsonMapper.builder().build(), List.of("{\"terms\":{}}"))).isEmpty();
    }

    @Test
    void AWS_오류_코드를_공급자_실패_코드로_바꾼다() {
        assertThat(SdkAwsGpuApi.failure("인스턴스 만들기", ec2("InsufficientInstanceCapacity", 500)).code()).isEqualTo("OUT_OF_STOCK");
        assertThat(SdkAwsGpuApi.failure("인스턴스 만들기", ec2("VcpuLimitExceeded", 400)).code()).isEqualTo("REJECTED");
        assertThat(SdkAwsGpuApi.failure("인스턴스 만들기", ec2("VcpuLimitExceeded", 400)).getMessage()).contains("VcpuLimitExceeded");
        assertThat(SdkAwsGpuApi.failure("인스턴스 만들기", ec2("UnauthorizedOperation", 403)).code()).isEqualTo("REJECTED");
        assertThat(SdkAwsGpuApi.failure("인스턴스 만들기", ec2("RequestLimitExceeded", 503)).code()).isEqualTo("UNAVAILABLE");
        assertThat(SdkAwsGpuApi.failure("인스턴스 만들기",
                SdkClientException.create("Unable to load credentials from any of the providers in the chain")).code()).isEqualTo("NOT_CONFIGURED");
        assertThat(SdkAwsGpuApi.failure("인스턴스 만들기", SdkClientException.create("Connect timed out")).code()).isEqualTo("UNAVAILABLE");
    }

    private static Ec2Exception ec2(String code, int status) {
        return (Ec2Exception) Ec2Exception.builder().awsErrorDetails(AwsErrorDetails.builder().errorCode(code).build()).statusCode(status)
                .build();
    }
}
