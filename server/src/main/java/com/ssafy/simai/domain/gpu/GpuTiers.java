package com.ssafy.simai.domain.gpu;

import java.util.regex.Pattern;

/**
 * 켜기 패널의 GPU 추천·VRAM 등급(S15P31A101-705, 설계 docs/design/S15P31A101-705-gpu-panel-settings-page.md, 명세 3.12 최소 사양).
 * AWS·RunPod 공통 규칙이고 이 한 곳에서만 정한다(화면은 서버가 준 tier·recommended 를 그대로 그린다).
 * <ul>
 * <li>VRAM 은 GPU 1장 기준(AWS 인스턴스가 여러 장이어도 한 프로세스 묶음이 한 장을 쓴다). 모르면 등급도 비운다(null).</li>
 * <li>80GB급 RECOMMENDED / 48GB급 BELOW_RECOMMENDED(Tier 0 + 분석 동시 실행 최소, VLM 몫 미결) / 24GB급 LIMITED(Tier 0 관제만:
 * 부팅이 lobby 서비스를 tier0 로 줄여 세팅 맵 굽기·VLM·아바타가 꺼진다) / 그 미만 RISKY(Tier 0 최소 사양 미만, 개발·시험용).
 * 경계는 74·44·22GB 다: 공급자가 MiB 를 GB 로 환산한 값(AWS L40S 45,776 MiB → 45, A10G 22,888 MiB → 22)도 판매 등급 칸에 들게
 * 부팅의 기존 상수(simai_bootstrap 76,000·45,000·22,500 MiB)와 맞췄다.</li>
 * <li>추천(recommended): H100·H200 계열이면서 80GB급(또는 VRAM 모름). 시연 기준 GPU(H100)이고 TensorRT 엔진 캐시가 GPU 종류별이라
 * 쓰던 종류를 지키는 편이 기동이 빠르다.</li>
 * </ul>
 * 막지 않는다: 등급은 표시·정렬·확인창용이고 켜기를 거부하지 않는다.
 */
final class GpuTiers {

    static final String RECOMMENDED = "RECOMMENDED";
    static final String BELOW_RECOMMENDED = "BELOW_RECOMMENDED";
    static final String LIMITED = "LIMITED";
    static final String RISKY = "RISKY";

    /** H100·H200 계열 이름(공급자 ID·표시 이름·AWS "p5.48xlarge · H100 ×8" 모두). GH200 처럼 앞에 글자가 붙은 것은 빼고 H1000 같은 것도 뺀다 */
    private static final Pattern FAMILY = Pattern.compile("(?i)(?<![a-z0-9])H(100|200)(?![0-9])");

    private GpuTiers() {
    }

    /** 80GB급 하한(GB). 76,000 MiB ≈ 74.2 GiB */
    static final int RECOMMENDED_MIN_GB = 74;
    /** 48GB급 하한(GB). 45,000 MiB ≈ 43.9 GiB */
    static final int BELOW_RECOMMENDED_MIN_GB = 44;
    /** 24GB급 하한(GB). 22,500 MiB ≈ 22.0 GiB */
    static final int LIMITED_MIN_GB = 22;

    /** VRAM 등급. memoryGb 를 모르면 null */
    static String tier(Integer memoryGb) {
        if (memoryGb == null) {
            return null;
        }
        if (memoryGb >= RECOMMENDED_MIN_GB) {
            return RECOMMENDED;
        }
        if (memoryGb >= BELOW_RECOMMENDED_MIN_GB) {
            return BELOW_RECOMMENDED;
        }
        if (memoryGb >= LIMITED_MIN_GB) {
            return LIMITED;
        }
        return RISKY;
    }

    /** 정렬 순서(작을수록 위). 모르는 등급은 맨 뒤 */
    static int rank(String tier) {
        if (tier == null) {
            return 4;
        }
        return switch (tier) {
            case RECOMMENDED -> 0;
            case BELOW_RECOMMENDED -> 1;
            case LIMITED -> 2;
            default -> 3;
        };
    }

    /** 추천: H100·H200 계열 + 80GB급(VRAM 을 모르면 이름만 본다) */
    static boolean recommended(String gpuType, String name, Integer memoryGb) {
        boolean family = (gpuType != null && FAMILY.matcher(gpuType).find()) || (name != null && FAMILY.matcher(name).find());
        return family && (memoryGb == null || memoryGb >= RECOMMENDED_MIN_GB);
    }

    /** 칸 재고 → 켤 수 있는지. HIGH·MEDIUM·LOW → true, NONE·칸 없음 → false, UNKNOWN(AWS 처럼 실시간 재고를 모름) → null */
    static Boolean available(String stock) {
        if (stock == null || "NONE".equals(stock)) {
            return false;
        }
        if ("UNKNOWN".equals(stock)) {
            return null;
        }
        return true;
    }
}
