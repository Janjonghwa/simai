package com.ssafy.simai.domain.calibration;

import com.ssafy.simai.domain.config.ConfigFiles;
import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import tools.jackson.databind.JsonNode;

/**
 * 보정 문서의 content_hash(= calibration_update.calibration_hash). ai calibration/document.py 의 content_hash 와 같은 값을 낸다:
 * 버전 ID·생성 시각·상태·계약 버전·content_hash 를 뺀 최상위 키를 Python json.dumps(sort_keys=True, separators=(",", ":"),
 * ensure_ascii=False) 와 같은 글자로 써서 UTF-8 sha256 을 잰다. 문서가 스스로 적은 content_hash 는 믿지 않고 다시 잰다.
 * 같은 보정이면 Tier 0 알림과 관리자 업로드가 같은 해시가 되어 같은 버전으로 맞춰진다(CalibrationService·CalibrationUpdateIngestor).
 */
final class CalibrationContentHash {

    private static final Set<String> UNHASHED = Set.of("content_hash", "calibration_version", "created_at", "state", "schema_version");

    private CalibrationContentHash() {
    }

    static String of(JsonNode doc) {
        StringBuilder out = new StringBuilder();
        writeObject(doc, UNHASHED, out);
        return ConfigFiles.sha256(out.toString().getBytes(StandardCharsets.UTF_8));
    }

    private static void write(JsonNode node, StringBuilder out) {
        if (node.isObject()) {
            writeObject(node, Set.of(), out);
        } else if (node.isArray()) {
            out.append('[');
            for (int i = 0; i < node.size(); i++) {
                if (i > 0) {
                    out.append(',');
                }
                write(node.get(i), out);
            }
            out.append(']');
        } else if (node.isString()) {
            string(node.asString(), out);
        } else if (node.isIntegralNumber()) {
            out.append(node.bigIntegerValue());
        } else if (node.isNumber()) {
            out.append(pythonFloat(node.doubleValue()));
        } else if (node.isBoolean()) {
            out.append(node.booleanValue() ? "true" : "false");
        } else {
            out.append("null");
        }
    }

    private static void writeObject(JsonNode node, Set<String> skip, StringBuilder out) {
        List<Map.Entry<String, JsonNode>> entries = new ArrayList<>(node.properties());
        entries.removeIf(e -> skip.contains(e.getKey()));
        // Python 은 코드 포인트 순으로 정렬한다(Java String 은 UTF-16 단위 순)
        entries.sort((a, b) -> compareCodePoints(a.getKey(), b.getKey()));
        out.append('{');
        for (int i = 0; i < entries.size(); i++) {
            if (i > 0) {
                out.append(',');
            }
            string(entries.get(i).getKey(), out);
            out.append(':');
            write(entries.get(i).getValue(), out);
        }
        out.append('}');
    }

    private static int compareCodePoints(String a, String b) {
        int i = 0;
        int j = 0;
        while (i < a.length() && j < b.length()) {
            int ca = a.codePointAt(i);
            int cb = b.codePointAt(j);
            if (ca != cb) {
                return Integer.compare(ca, cb);
            }
            i += Character.charCount(ca);
            j += Character.charCount(cb);
        }
        return Integer.compare(a.length() - i, b.length() - j);
    }

    /** ensure_ascii=False: 따옴표·역슬래시·제어 문자만 이스케이프하고 나머지(한글, U+2028 등)는 그대로 쓴다 */
    private static void string(String s, StringBuilder out) {
        out.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        out.append('"');
    }

    /**
     * Python repr(float): 가장 짧게 되돌아오는 자릿수로, 소수점 위치(decpt)가 -4 이하이거나 16 초과면 지수형(1e-05, 2.5e+20),
     * 아니면 고정형이고 정수 값이면 .0 을 붙인다(100.0).
     */
    static String pythonFloat(double value) {
        if (Double.isNaN(value)) {
            return "NaN";
        }
        if (Double.isInfinite(value)) {
            return value > 0 ? "Infinity" : "-Infinity";
        }
        if (value == 0) {
            return Double.doubleToRawLongBits(value) < 0 ? "-0.0" : "0.0";
        }
        BigDecimal exact = shortest(Math.abs(value));
        String digits = exact.unscaledValue().toString();
        int decpt = digits.length() - exact.scale();
        StringBuilder out = new StringBuilder(value < 0 ? "-" : "");
        if (decpt <= -4 || decpt > 16) {
            out.append(digits.charAt(0));
            if (digits.length() > 1) {
                out.append('.').append(digits, 1, digits.length());
            }
            int exponent = decpt - 1;
            out.append('e').append(exponent < 0 ? '-' : '+');
            int abs = Math.abs(exponent);
            out.append(abs < 10 ? "0" : "").append(abs);
        } else if (decpt <= 0) {
            out.append("0.").append("0".repeat(-decpt)).append(digits);
        } else if (decpt >= digits.length()) {
            out.append(digits).append("0".repeat(decpt - digits.length())).append(".0");
        } else {
            out.append(digits, 0, decpt).append('.').append(digits, decpt, digits.length());
        }
        return out.toString();
    }

    /**
     * 되돌리면 같은 double 이 되는 가장 짧은 십진수(같은 길이면 참값에 가장 가까운 것, Python repr 과 같다).
     * Double.toString 은 한 자리로 충분해도 두 자리를 쓸 때가 있어(5e-324 → 4.9E-324) 참값을 자릿수별로 찾는다.
     * 2의 거듭제곱은 아래쪽 반올림 범위가 위쪽의 절반이라, 가장 가까운 후보가 범위 밖이어도 반대쪽 후보가 들어갈 수 있다.
     * 그래서 자릿수마다 내림·올림 후보를 둘 다 보고, 둘 다 되면 참값에 가까운 쪽(같으면 반올림 결과)을 고른다.
     */
    private static BigDecimal shortest(double positive) {
        BigDecimal exact = new BigDecimal(positive);
        for (int precision = 1; precision <= 17; precision++) {
            BigDecimal down = exact.round(new MathContext(precision, RoundingMode.FLOOR));
            BigDecimal up = exact.round(new MathContext(precision, RoundingMode.CEILING));
            boolean downOk = down.doubleValue() == positive;
            boolean upOk = up.doubleValue() == positive;
            if (downOk && upOk) {
                int closer = exact.subtract(down).compareTo(up.subtract(exact));
                BigDecimal pick = closer < 0 ? down : closer > 0 ? up
                        : exact.round(new MathContext(precision, RoundingMode.HALF_EVEN));
                return pick.stripTrailingZeros();
            }
            if (downOk || upOk) {
                return (downOk ? down : up).stripTrailingZeros();
            }
        }
        throw new IllegalStateException("17자리로도 되돌아오지 않는 double: " + positive);
    }
}
