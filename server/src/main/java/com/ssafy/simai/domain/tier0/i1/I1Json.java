package com.ssafy.simai.domain.tier0.i1;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.StringJoiner;
import java.util.UUID;
import tools.jackson.databind.JsonNode;

/**
 * I1 payload 값을 계약 타입대로인지 보고 SQL 값으로 바꾼다. 틀리면 InvalidI1Message(칸 이름만, 값은 넣지 않음).
 * 없음과 JSON null 은 모두 SQL NULL 이다(NOT NULL 칸이면 DB 가 거부한다).
 * 배열은 PostgreSQL 배열 문자열로 만들어 CAST 한다. JSON null 은 원소 NULL 로 둔다(NaN 으로 바꾸지 않는다).
 */
public final class I1Json {

    private I1Json() {
    }

    public static boolean absent(JsonNode node) {
        return node == null || node.isNull() || node.isMissingNode();
    }

    private static InvalidI1Message wrongType(String field, String expected) {
        return new InvalidI1Message(field + " 이 " + expected + " 가 아니다");
    }

    public static String text(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        if (absent(node)) {
            return null;
        }
        if (!node.isString()) {
            throw wrongType(field, "문자열");
        }
        return node.stringValue();
    }

    public static String requiredText(JsonNode parent, String field) {
        String value = text(parent, field);
        if (value == null || value.isBlank()) {
            throw new InvalidI1Message(field + " 이 비었다");
        }
        return value;
    }

    public static UUID uuid(JsonNode parent, String field) {
        String value = text(parent, field);
        if (value == null) {
            return null;
        }
        try {
            return UUID.fromString(value);
        } catch (IllegalArgumentException e) {
            throw wrongType(field, "UUID");
        }
    }

    public static UUID requiredUuid(JsonNode parent, String field) {
        UUID value = uuid(parent, field);
        if (value == null) {
            throw new InvalidI1Message(field + " 이 비었다");
        }
        return value;
    }

    private static Double finite(JsonNode node, String field) {
        if (!node.isNumber()) {
            throw wrongType(field, "수");
        }
        double value = node.doubleValue();
        if (!Double.isFinite(value)) {
            throw wrongType(field, "유한한 수");
        }
        return value;
    }

    public static Double number(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        return absent(node) ? null : finite(node, field);
    }

    /** int 칸(ms 길이·차수·순번). 넘치면 음수로 감기지 않고 거부한다 */
    public static Integer integer(JsonNode parent, String field) {
        Double value = number(parent, field);
        if (value == null) {
            return null;
        }
        long rounded = Math.round(value);
        if (rounded < Integer.MIN_VALUE || rounded > Integer.MAX_VALUE) {
            throw new InvalidI1Message(field + " 이 int 범위를 넘는다");
        }
        return (int) rounded;
    }

    /** 정수 칸(journal_offset 등). 소수·범위 밖은 거부한다 */
    public static Long whole(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        if (absent(node)) {
            return null;
        }
        if (!node.isIntegralNumber() || !node.canConvertToLong()) {
            throw wrongType(field, "정수");
        }
        return node.longValue();
    }

    public static long requiredWhole(JsonNode parent, String field) {
        Long value = whole(parent, field);
        if (value == null) {
            throw new InvalidI1Message(field + " 이 비었다");
        }
        return value;
    }

    public static Boolean bool(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        if (absent(node)) {
            return null;
        }
        if (!node.isBoolean()) {
            throw wrongType(field, "불린");
        }
        return node.booleanValue();
    }

    public static OffsetDateTime time(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        if (absent(node)) {
            return null;
        }
        if (!node.isIntegralNumber() || !node.canConvertToLong()) {
            throw wrongType(field, "정수 ms 시각");
        }
        return OffsetDateTime.ofInstant(Instant.ofEpochMilli(node.longValue()), ZoneOffset.UTC);
    }

    public static OffsetDateTime requiredTime(JsonNode parent, String field) {
        OffsetDateTime value = time(parent, field);
        if (value == null) {
            throw new InvalidI1Message(field + " 이 비었다");
        }
        return value;
    }

    public static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    public static String json(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        return absent(node) ? null : node.toString();
    }

    /** 계약 vec2·vec3: 길이가 정해진 유한한 수 배열. required 가 아니면 없거나 null 일 수 있다 */
    public static List<Double> vector(JsonNode parent, String field, int length, boolean required) {
        JsonNode node = parent.get(field);
        if (absent(node)) {
            if (required) {
                throw new InvalidI1Message(field + " 이 비었다");
            }
            return null;
        }
        if (!node.isArray() || node.size() != length) {
            throw wrongType(field, length + "차원 배열");
        }
        List<Double> values = new ArrayList<>(length);
        for (JsonNode element : node.values()) {
            if (absent(element)) {
                throw wrongType(field, "수 배열");
            }
            values.add(finite(element, field));
        }
        return values;
    }

    /** {1.5,NULL,2.0}. 원소 null 은 NULL */
    public static String arrayLiteral(List<?> values) {
        if (values == null) {
            return null;
        }
        StringJoiner out = new StringJoiner(",", "{", "}");
        values.forEach(v -> out.add(v == null ? "NULL" : v.toString()));
        return out.toString();
    }

    public static String booleanArray(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        if (absent(node)) {
            return null;
        }
        if (!node.isArray()) {
            throw wrongType(field, "배열");
        }
        List<Boolean> values = new ArrayList<>();
        for (JsonNode element : node.values()) {
            if (absent(element)) {
                values.add(null);
            } else if (element.isBoolean()) {
                values.add(element.booleanValue());
            } else {
                throw wrongType(field, "불린 배열");
            }
        }
        return arrayLiteral(values);
    }

    /** 문자열 배열: 원소를 큰따옴표로 감싸고 \ 와 " 를 이스케이프한다. */
    public static String textArray(JsonNode parent, String field) {
        JsonNode node = parent.get(field);
        if (absent(node)) {
            return null;
        }
        if (!node.isArray()) {
            throw wrongType(field, "배열");
        }
        StringJoiner out = new StringJoiner(",", "{", "}");
        for (JsonNode element : node.values()) {
            if (absent(element)) {
                out.add("NULL");
            } else if (element.isString()) {
                out.add('"' + element.stringValue().replace("\\", "\\\\").replace("\"", "\\\"") + '"');
            } else {
                throw wrongType(field, "문자열 배열");
            }
        }
        return out.toString();
    }

    /**
     * 관절 좌표 [[x,y,z], null, …] → {{x,y,z},{NULL,NULL,NULL},…}. 다차원 배열은 직사각형이어야 해서 null 관절도 3칸을 채운다.
     * 좌표 원소 하나만 null 인 관절은 계약에 없다(관절 단위로 null).
     */
    public static String joints(JsonNode skeleton) {
        JsonNode node = skeleton.get("joints");
        if (absent(node)) {
            return null;
        }
        if (!node.isArray()) {
            throw wrongType("joints", "배열");
        }
        StringJoiner out = new StringJoiner(",", "{", "}");
        for (JsonNode joint : node.values()) {
            if (absent(joint)) {
                out.add("{NULL,NULL,NULL}");
                continue;
            }
            if (!joint.isArray() || joint.size() != 3) {
                throw wrongType("joints", "3차원 좌표 배열");
            }
            List<Double> xyz = new ArrayList<>(3);
            for (JsonNode c : joint.values()) {
                if (absent(c)) {
                    throw wrongType("joints", "수 좌표");
                }
                xyz.add(finite(c, "joints"));
            }
            out.add(arrayLiteral(xyz));
        }
        return out.toString();
    }
}
