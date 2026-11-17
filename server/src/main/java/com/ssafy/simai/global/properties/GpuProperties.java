package com.ssafy.simai.global.properties;

import java.math.BigDecimal;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * GPU 켜기·끄기·자동 정지 설정(S15P31A101-702, 설계 docs/design/S15P31A101-702-gpu-provider-runpod.md).
 * 화면에서 바꾸는 값(선호 GPU·지역·가격 상한·자동 정지 분·템플릿 ID)은 DB 의 GPU 설정 버전이고, 여기 값은 그것이 없을 때의 기본값과
 * 운영 고정값이다. 비밀 값은 RunPod API 키 하나뿐이고 환경 변수(SIMAI_RUNPOD_API_KEY)로만 받는다(로그·응답에 싣지 않는다).
 * AWS(S15P31A101-703)는 자격 증명을 AWS 기본 자격 증명 체인(인스턴스 역할·AWS_* 환경 변수) 또는 전용 IAM 사용자 키(.env)로만 받는다.
 *
 * @param runpod RunPod 호출 설정
 * @param aws AWS 서울 GPU 호출 설정(기본 꺼짐)
 * @param defaults DB 설정 버전이 없을 때 쓰는 기본값
 * @param bootTimeout 켠 뒤 G1 등록이 이만큼 없으면 오류로 보고 끈다
 * @param failedGrace 세션이 FAILED 를 이만큼 이어 보고하고 Tier 0 I1 도 없으면 끈다
 * @param uploadHold 녹화 조각 업로드 서명이 이만큼 안에 있었으면 업로드 중으로 보고 자동 정지하지 않는다(deploy.sh 보류와 같은 10분)
 * @param sweepInterval 자동 정지 점검 주기
 * @param presenceMaxExtension 관리자 화면 조작 신호(presence)만으로 유휴 자동 정지를 미룰 수 있는 최대 시간. 마지막 실제 사용(GpuActivity.lastUse)과
 *        준비 완료 중 늦은 시각부터 센다(S15P31A101-774, 설계 docs/design/S15P31A101-774-gpu-presence-visible-cap.md). 기본 90분 = 유휴 기본 30분의
 *        3배: 관제·세팅 전 화면 점검·준비에 충분하고, 조작만 이어진 세션의 과금을 한 세션 최대 약 1.5시간(H100 시간당 약 4.4 USD 기준 약 6.6 USD)으로 묶는다.
 *        LIVE·세팅·업로드·분석 작업 중에는 이 상한과 무관하게 끄지 않는다
 */
@ConfigurationProperties("simai.gpu")
public record GpuProperties(RunPod runpod, Aws aws, Defaults defaults, Duration bootTimeout, Duration failedGrace, Duration uploadHold,
        Duration sweepInterval, Duration presenceMaxExtension) {

    public GpuProperties {
        runpod = runpod == null ? new RunPod(null, null, null, null, null, null, null, null) : runpod;
        aws = aws == null ? new Aws(null, null, null, null, null, null, null, null, null, null, null) : aws;
        defaults = defaults == null ? new Defaults(null, null, null, null, null, null, null) : defaults;
        bootTimeout = positive(bootTimeout, Duration.ofMinutes(25));
        failedGrace = positive(failedGrace, Duration.ofMinutes(20));
        uploadHold = positive(uploadHold, Duration.ofMinutes(10));
        sweepInterval = positive(sweepInterval, Duration.ofSeconds(60));
        presenceMaxExtension = positive(presenceMaxExtension, Duration.ofMinutes(90));
    }

    /**
     * @param apiKey RunPod API 키(EC2 .env SIMAI_RUNPOD_API_KEY). 비어 있으면 켜기·재고 조회가 503 이다
     * @param restBaseUrl Pod 만들기·끄기·조회 REST(https://api.runpod.io/v2). REST v1(rest.runpod.io/v1)은 2026-11-15 에 닫혔다(S15P31A101-781)
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
            restBaseUrl = url(restBaseUrl, "https://api.runpod.io/v2");
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

    /**
     * AWS 서울 GPU(S15P31A101-703, 설계 docs/design/S15P31A101-703-aws-gpu-provider.md). 시작 템플릿·서브넷·보안 그룹·인스턴스 종류 후보·
     * 스팟 여부는 화면 「GPU 설정」(DB 버전)이고 여기는 운영 고정값이다. 비밀 값은 전용 IAM 사용자 키뿐이고 비우면 기본 자격 증명 체인을 쓴다.
     *
     * @param enabled 켜야 AWS 를 부른다(기본 꺼짐: 재고 표·설정 화면에 "준비 안 됨")
     * @param region EC2 리전(ap-northeast-2 서울)
     * @param accessKey 전용 IAM 사용자 키 ID(EC2 .env SIMAI_GPU_AWS_ACCESS_KEY_ID). 비면 기본 자격 증명 체인
     * @param secretKey 그 비밀 키(SIMAI_GPU_AWS_SECRET_ACCESS_KEY, 로그·응답에 싣지 않는다)
     * @param image 인스턴스가 띄울 GPU 이미지(ECR 서울 사본 권장, 다이제스트 고정). 비면 켜지 않는다
     * @param secretId 인스턴스가 역할로 읽을 Secrets Manager 비밀 이름(JSON {환경 변수 이름: 값}, 비밀 아님)
     * @param launchTemplateVersion 시작 템플릿 판($Default·$Latest·숫자)
     * @param timeout AWS 호출 하나의 시간 제한
     * @param pricingRegion Price List API 엔드포인트 리전(us-east-1 등 몇 곳에만 있다)
     * @param onDemandPrices Price List 를 못 읽을 때 쓰는 서울 온디맨드 시간당 가격(USD, 인스턴스 1대). 비면 코드 기본표
     * @param extraEnv 인스턴스 컨테이너에 더 넣을 비밀 아닌 환경 변수(예 SIMAI_TIER0_PINNED_SPKI_SHA256). 비밀 이름은 거부한다
     */
    public record Aws(Boolean enabled, String region, String accessKey, String secretKey, String image, String secretId,
            String launchTemplateVersion, Duration timeout, String pricingRegion, Map<String, BigDecimal> onDemandPrices,
            Map<String, String> extraEnv) {

        public Aws {
            enabled = enabled != null && enabled;
            region = blank(region) ? "ap-northeast-2" : region.strip();
            accessKey = accessKey == null ? "" : accessKey.strip();
            secretKey = secretKey == null ? "" : secretKey.strip();
            image = image == null ? "" : image.strip();
            secretId = blank(secretId) ? "simai/gpu-boot" : secretId.strip();
            launchTemplateVersion = blank(launchTemplateVersion) ? "$Default" : launchTemplateVersion.strip();
            timeout = positive(timeout, Duration.ofSeconds(20));
            pricingRegion = blank(pricingRegion) ? "us-east-1" : pricingRegion.strip();
            if (onDemandPrices == null || onDemandPrices.isEmpty()) {
                // 서울 온디맨드 Linux 시간당 USD(인스턴스 1대). 2026-11-14 DoiT 가격 집계(compute.doit.com) 사본, 공식 Price List 를 못 읽을 때만.
                // 근거·날짜는 설계 문서 표. p5.4xlarge 는 서울 가격이 없어(서울 미제공으로 보임) 넣지 않았다
                Map<String, BigDecimal> table = new LinkedHashMap<>();
                table.put("p5.48xlarge", new BigDecimal("75.9552"));
                table.put("p5en.48xlarge", new BigDecimal("87.3485"));
                table.put("p4d.24xlarge", new BigDecimal("30.4103"));
                table.put("g7e.2xlarge", new BigDecimal("4.1348"));
                table.put("g7e.4xlarge", new BigDecimal("4.9155"));
                table.put("g6e.xlarge", new BigDecimal("2.2880"));
                table.put("g6e.2xlarge", new BigDecimal("2.7565"));
                table.put("g6e.4xlarge", new BigDecimal("3.6936"));
                table.put("g5.xlarge", new BigDecimal("1.2370"));
                table.put("g5.2xlarge", new BigDecimal("1.4903"));
                onDemandPrices = table;
            } else {
                onDemandPrices = new LinkedHashMap<>(onDemandPrices);
            }
            if (extraEnv == null || extraEnv.isEmpty()) {
                Map<String, String> fallback = new LinkedHashMap<>();
                fallback.put("SIMAI_TIER0_GPU_RESERVE_MB", "8192"); // RunPod 템플릿과 같은 초기값(deploy/README)
                extraEnv = fallback;
            } else {
                extraEnv = new LinkedHashMap<>(extraEnv);
            }
        }

        public boolean staticKey() {
            return !accessKey.isEmpty() && !secretKey.isEmpty();
        }

        @Override
        public String toString() {
            // 실수로 로그에 찍어도 키가 나가지 않게
            return "Aws[enabled=" + enabled + ", region=" + region + ", image=" + image + ", staticKey=" + staticKey() + "]";
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
