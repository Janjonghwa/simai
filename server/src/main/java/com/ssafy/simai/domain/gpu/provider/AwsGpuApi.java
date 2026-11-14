package com.ssafy.simai.domain.gpu.provider;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * AwsGpuProvider 가 부르는 AWS 호출(S15P31A101-703). 운영은 {@link SdkAwsGpuApi}(AWS SDK), 시험은 가짜 구현을 쓴다(실제 AWS 를 부르지 않는다).
 * 실패는 GpuProviderException(code: NOT_CONFIGURED·OUT_OF_STOCK·REJECTED·UNAVAILABLE·BAD_RESPONSE)으로 바꿔 던진다.
 * 문구·로그에는 AWS 오류 코드와 HTTP 상태만 싣는다(자격 증명·user-data 원문 없음).
 */
public interface AwsGpuApi {

    /** 자격 증명을 찾을 수 있는지(기본 자격 증명 체인 또는 전용 IAM 사용자 키). 결과는 잠시 기억해도 된다 */
    boolean credentialsAvailable();

    /** DescribeInstanceTypeOfferings(가용 영역 단위): 인스턴스 종류 → 그 종류를 파는 가용 영역 이름 */
    Map<String, Set<String>> offeredZones(List<String> instanceTypes) throws GpuProviderException;

    /** DescribeInstanceTypes: 인스턴스 종류 → GPU 정보(모르는 종류는 빠진다) */
    Map<String, GpuSpec> gpuSpecs(List<String> instanceTypes) throws GpuProviderException;

    /** DescribeSubnets: 서브넷 ID → 가용 영역 이름 */
    Map<String, String> subnetZones(List<String> subnetIds) throws GpuProviderException;

    /** DescribeSpotPriceHistory(Linux/UNIX, 지금): "인스턴스 종류\n가용 영역" → 시간당 스팟 가격(USD) */
    Map<String, BigDecimal> spotPrices(List<String> instanceTypes) throws GpuProviderException;

    /** Price List GetProducts: 이 리전·Linux·공유 테넌시 온디맨드 시간당 가격(USD). 못 찾으면 비어 있다 */
    Optional<BigDecimal> onDemandPrice(String instanceType) throws GpuProviderException;

    /** RunInstances. @return 인스턴스 ID */
    String run(RunRequest request) throws GpuProviderException;

    /** TerminateInstances. 이미 없으면 성공 */
    void terminate(String instanceId) throws GpuProviderException;

    /** DescribeInstances 의 상태 이름(pending·running·stopping·stopped·shutting-down·terminated). 없으면 비어 있다 */
    Optional<String> state(String instanceId) throws GpuProviderException;

    /** 인스턴스 종류의 GPU(이름·개수·GPU 1개 메모리 GB) */
    record GpuSpec(String gpuName, int gpuCount, Integer gpuMemoryGb) {
    }

    /**
     * RunInstances 요청(비밀 없음).
     * @param subnetId null 이면 availabilityZone 의 기본 서브넷(기본 VPC)
     * @param securityGroupIds 비면 시작 템플릿의 보안 그룹
     * @param userDataBase64 부팅 스크립트(비밀 원문 없음, Base64)
     * @param clientToken 같은 켠 요청을 두 번 보내도 인스턴스가 하나만 생기게(켠 요청 ID)
     */
    record RunRequest(String launchTemplateId, String launchTemplateVersion, String instanceType, String availabilityZone,
            String subnetId, List<String> securityGroupIds, boolean spot, String userDataBase64, Map<String, String> tags,
            String clientToken) {
    }
}
