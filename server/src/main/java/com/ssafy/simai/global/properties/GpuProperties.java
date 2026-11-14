package com.ssafy.simai.global.properties;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * GPU 켜기·끄기·자동 정지 설정(S15P31A101-702, 설계 docs/design/S15P31A101-702-gpu-provider-runpod.md).
 * 화면에서 바꾸는 값(선호 GPU·지역·가격 상한·자동 정지 분·템플릿 ID)은 DB 의 GPU 설정 버전이고, 여기 값은 그것이 없을 때의 기본값과
 * 운영 고정값이다. 비밀 값은 RunPod API 키 하나뿐이고 환경 변수(SIMAI_RUNPOD_API_KEY)로만 받는다(로그·응답에 싣지 않는다).
 *
 * @param runpod RunPod 호출 설정
 * @param defaults DB 설정 버전이 없을 때 쓰는 기본값
 * @param bootTimeout 켠 뒤 G1 등록이 이만큼 없으면 오류로 보고 끈다
 * @param failedGrace 세션이 FAILED 를 이만큼 이어 보고하고 Tier 0 I1 도 없으면 끈다
 * @param uploadHold 녹화 조각 업로드 서명이 이만큼 안에 있었으면 업로드 중으로 보고 자동 정지하지 않는다(deploy.sh 보류와 같은 10분)
 * @param sweepInterval 자동 정지 점검 주기
 */
@ConfigurationProperties("simai.gpu")
public record GpuProperties(RunPod runpod, Defaults defaults, Duration bootTimeout, Duration failedGrace, Duration uploadHold,
        Duration sweepInterval) {

    public GpuProperties {
        runpod = runpod == null ? new RunPod(null, null, null, null, null, null, null, null) : runpod;
        defaults = defaults == null ? new Defaults(null, null, null, null, null, null, null) : defaults;
        bootTimeout = positive(bootTimeout, Duration.ofMinutes(25));
        failedGrace = positive(failedGrace, Duration.ofMinutes(20));
        uploadHold = positive(uploadHold, Duration.ofMinutes(10));
        sweepInterval = positive(sweepInterval, Duration.ofSeconds(60));
    }

    /**
     * @param apiKey RunPod API 키(EC2 .env SIMAI_RUNPOD_API_KEY). 비어 있으면 켜기·재고 조회가 503 이다
     * @param restBaseUrl Pod 만들기·끄기·조회 REST(https://rest.runpod.io/v1)
     * @param catalogBaseUrl GPU 재고·가격 목록(https://api.runpod.io, 경로 /v2/catalog/gpus)
     * @param timeout 요청 하나의 시간 제한
     * @param bootId 인스턴스에 넣을 부팅 비밀값 ID(비밀 아님, admin gpu-boot register --id)
     * @param ec2BaseUrl 인스턴스가 등록할 EC2 주소. 비면 구성 묶음 공개 주소(SIMAI_PUBLIC_BASE_URL)
     * @param secretEnv 인스턴스 환경 변수 이름 → RunPod Secret 이름. 값은 {{ RUNPOD_SECRET_이름 }} 참조로만 넣는다(서버는 원문을 모른다)
     * @param podName 만들 Pod 이름 앞부분
     */
    public record RunPod(String apiKey, String restBaseUrl, String catalogBaseUrl, Duration timeout, String bootId,
            String ec2BaseUrl, Map<String, String> secretEnv, String podName) {

        public RunPod {
            apiKey = apiKey == null ? "" : apiKey.strip();
            restBaseUrl = url(restBaseUrl, "https://rest.runpod.io/v1");
            catalogBaseUrl = url(catalogBaseUrl, "https://api.runpod.io");
            timeout = positive(timeout, Duration.ofSeconds(20));
            bootId = blank(bootId) ? "gpu-boot-runpod" : bootId.strip();
            ec2BaseUrl = blank(ec2BaseUrl) ? "" : ec2BaseUrl.strip().replaceAll("/+$", "");
            if (secretEnv == null || secretEnv.isEmpty()) {
                Map<String, String> fallback = new LinkedHashMap<>();
                fallback.put("SIMAI_GPU_BOOT_SECRET", "simai_gpu_boot_secret");
                fallback.put("SIMAI_TIER0_TLS_PRIVATE_KEY", "simai_tier0_tls_key");
                fallback.put("HF_TOKEN", "hf_token");
                secretEnv = fallback;
            } else {
                secretEnv = new LinkedHashMap<>(secretEnv);
            }
            podName = blank(podName) ? "simai-gpu" : podName.strip();
        }

        public boolean configured() {
            return !apiKey.isEmpty();
        }

        @Override
        public String toString() {
            // 실수로 로그에 찍어도 키가 나가지 않게
            return "RunPod[restBaseUrl=" + restBaseUrl + ", catalogBaseUrl=" + catalogBaseUrl + ", configured=" + configured() + "]";
        }
    }

    /** DB 설정이 없을 때의 기본값. 선호 GPU 는 H100 80GB 계열, 지역은 서울(EC2·게이트웨이)과 가까운 아시아 먼저 */
    public record Defaults(List<String> gpuTypes, List<String> regions, String cloudType, Boolean interruptible,
            Double maxPricePerHourUsd, Integer autoStopIdleMinutes, String templateId) {

        public Defaults {
            gpuTypes = gpuTypes == null || gpuTypes.isEmpty()
                    ? List.of("NVIDIA H100 80GB HBM3", "NVIDIA H100 NVL", "NVIDIA H100 PCIe") : List.copyOf(gpuTypes);
            regions = regions == null || regions.isEmpty() ? List.of("AP-JP-1", "OC-AU-1") : List.copyOf(regions);
            cloudType = blank(cloudType) ? "SECURE" : cloudType.strip();
            interruptible = interruptible != null && interruptible;
            autoStopIdleMinutes = autoStopIdleMinutes == null ? 30 : autoStopIdleMinutes;
            templateId = blank(templateId) ? null : templateId.strip();
        }
    }

    private static boolean blank(String value) {
        return value == null || value.isBlank();
    }

    private static String url(String value, String fallback) {
        return blank(value) ? fallback : value.strip().replaceAll("/+$", "");
    }

    private static Duration positive(Duration value, Duration fallback) {
        return value == null || value.isZero() || value.isNegative() ? fallback : value;
    }
}
