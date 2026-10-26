package com.ssafy.simai.domain.config;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.security.SecureRandom;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.function.Predicate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ArrayNode;

/** 구역·작업면처럼 버전을 더해 가는 항목의 공통 도우미(명세 11.4: 덮어쓰지 않고 새 버전). */
public final class Versioned {

    private static final SecureRandom RANDOM = new SecureRandom();

    private Versioned() {
    }

    /**
     * 사람이 읽기 쉬운 짧은 ID(예: z-1a2b3c4d). 쓰이지 않은 것을 고른다. 저장 실패로 다시 고르지 않는다
     * (PostgreSQL 은 실패한 INSERT 뒤 그 트랜잭션을 더 쓸 수 없다)
     */
    public static String unusedId(String prefix, Predicate<String> used) {
        for (int attempt = 0; attempt < 8; attempt++) {
            byte[] bytes = new byte[4];
            RANDOM.nextBytes(bytes);
            String id = prefix + "-" + HexFormat.of().formatHex(bytes);
            if (!used.test(id)) {
                return id;
            }
        }
        throw new IllegalStateException("쓰이지 않은 ID 를 고르지 못했다: " + prefix);
    }

    /**
     * real(float4) 열에 넣을 값. 넣은 뒤의 값으로 검사하도록 float 로 줄여 돌려준다(1.0 과 1.00000001 은 둘 다 1.0 이 된다).
     * float 로 나타낼 수 없으면 code 로 실패한다
     */
    public static double real(Double value, ErrorCode code) {
        float f = value == null ? Float.NaN : value.floatValue();
        if (!Float.isFinite(f)) {
            throw new BusinessException(code);
        }
        return f;
    }

    /** real 열 값. 없으면 null */
    public static Double real(ResultSet rs, String column) throws SQLException {
        float value = rs.getFloat(column);
        return rs.wasNull() ? null : (double) value;
    }

    /** ST_AsGeoJSON 다각형의 바깥 고리를 꼭짓점 목록으로(닫는 점 뺌) */
    public static ArrayNode polygonXy(JsonNode geoJson, ArrayNode target) {
        JsonNode ring = geoJson.path("coordinates").get(0);
        for (int i = 0; i < ring.size() - 1; i++) {
            target.addArray().add(ring.get(i).get(0).doubleValue()).add(ring.get(i).get(1).doubleValue());
        }
        return target;
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
