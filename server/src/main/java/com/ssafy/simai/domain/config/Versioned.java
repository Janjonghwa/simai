package com.ssafy.simai.domain.config;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.security.SecureRandom;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;

/** 구역·작업면처럼 버전을 더해 가는 항목의 공통 도우미(명세 11.4: 덮어쓰지 않고 새 버전). */
public final class Versioned {

    private static final SecureRandom RANDOM = new SecureRandom();

    private Versioned() {
    }

    /** 사람이 읽기 쉬운 짧은 ID(예: z-1a2b3c4d). 겹치면 저장이 실패하고 다시 만든다 */
    public static String newId(String prefix) {
        byte[] bytes = new byte[4];
        RANDOM.nextBytes(bytes);
        return prefix + "-" + HexFormat.of().formatHex(bytes);
    }

    public static Long millis(OffsetDateTime time) {
        return time == null ? null : time.toInstant().toEpochMilli();
    }

    /** 유한한 수 n 개. 아니면 code 로 실패한다 */
    public static double[] finite(List<Double> values, int n, ErrorCode code) {
        if (values == null || values.size() != n) {
            throw new BusinessException(code);
        }
        double[] out = new double[n];
        for (int i = 0; i < n; i++) {
            Double v = values.get(i);
            if (v == null || !Double.isFinite(v)) {
                throw new BusinessException(code);
            }
            out[i] = v;
        }
        return out;
    }

    public static boolean blank(String value) {
        return value == null || value.isBlank();
    }
}
