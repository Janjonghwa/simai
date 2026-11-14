package com.ssafy.simai.domain.gpu.provider;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

/**
 * GPU 공급자(S15P31A101-702). 화면 켜기·끄기·재고 조회·자동 정지가 이 인터페이스만 부른다. RunPod 먼저(RunPodProvider),
 * AWS 서울 GPU(S15P31A101-703)는 같은 인터페이스로 붙는다(id "AWS"). 공급자 차이는 여기 아래로 숨긴다.
 * <p>세션 토큰·기동 설정은 공급자와 무관하다(G1, 인스턴스가 부팅 비밀값으로 EC2 에 등록). 공급자는 인스턴스를 만들고 끄기만 한다.
 * 비밀 값(공급자 API 키)은 구현이 설정에서만 읽고 예외 문구·로그에 싣지 않는다.
 */
public interface GpuProvider {

    /** gpu_settings_version.provider·gpu_session.provider 값(RUNPOD, AWS) */
    String id();

    /** API 키 등 호출에 필요한 설정이 있는지. 없으면 켜기·재고 조회가 503 */
    boolean configured();

    /** 지금 재고·시간당 가격(GPU 종류 × 지역). 재고가 없는 칸도 NONE 으로 싣는다(공급자가 아는 만큼) */
    List<GpuOffer> offers(OfferQuery query) throws GpuProviderException;

    /** 인스턴스를 만든다. 성공하면 공급자 인스턴스 ID(G1 등록의 instance_id 와 같은 값) */
    CreatedInstance create(LaunchSpec spec) throws GpuProviderException;

    /** 인스턴스를 없앤다(과금이 멈추게). 이미 없으면 성공이다(같은 결과) */
    void terminate(String instanceId) throws GpuProviderException;

    /** 인스턴스 상태. 없으면 GONE */
    InstanceState state(String instanceId) throws GpuProviderException;

    /** 재고 조회 조건 */
    record OfferQuery(String cloudType, int gpuCount) {
    }

    /**
     * 재고 한 칸.
     * @param gpuType 공급자 GPU 종류 ID(만들기에 그대로 쓴다)
     * @param stock HIGH·MEDIUM·LOW·NONE
     * @param pricePerHourUsd 온디맨드 시간당 가격(GPU 1대 × gpuCount). 모르면 null
     */
    record GpuOffer(String gpuType, String gpuName, Integer memoryGb, String region, String regionName, String stock,
            BigDecimal pricePerHourUsd) {

        public boolean inStock() {
            return !"NONE".equals(stock);
        }
    }

    /**
     * 만들기 요청.
     * @param plainEnv 비밀 아닌 환경 변수(부팅 비밀값 ID·EC2 주소·가격 등)
     * @param secretEnv 환경 변수 이름 → 공급자 비밀 저장소 이름. 구현이 공급자 참조 문법으로 넣는다(원문은 서버에 없다)
     */
    record LaunchSpec(String name, String templateId, String gpuType, String region, String cloudType, boolean interruptible,
            int gpuCount, Map<String, String> plainEnv, Map<String, String> secretEnv) {
    }

    /** 만든 인스턴스. costPerHourUsd 는 공급자가 응답에 실은 값(모르면 null) */
    record CreatedInstance(String instanceId, BigDecimal costPerHourUsd) {
    }

    /** 인스턴스 상태 */
    enum InstanceState {
        RUNNING, STOPPED, GONE, UNKNOWN
    }
}
