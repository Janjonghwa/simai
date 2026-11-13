package com.ssafy.simai.domain.setup;

import java.util.Map;
import java.util.TreeMap;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 스냅샷의 쌍별 홀드아웃 재투영 오차를 세션에 남길 모양으로 바꾼다(S15P31A101-602,
 * docs/design/S15P31A101-602-setup-camera-summary.md). 원천은 I5 site_setup {@code metrics.joint_ba} 의
 * {@code pair_holdout{before,after}}(1.4.0)·{@code pair_holdout_median_px}(1.1.0)·{@code holdout_worsening_tolerance_px} 다.
 * <pre>
 * {"holdout_worsening_tolerance_px": 1.0 | null,
 *  "pairs": {"cam_A-cam_B": {"before": {n, invalid, median, p90, p95} | null, "after": {...} | null, "median_px": 0.8 | null}}}
 * </pre>
 * 쌍 키는 워커가 보낸 그대로다({@code <camera_id>-<camera_id>}, 카메라 ID 에 '-' 가 있을 수 있어 나누지 않는다).
 * 표시용이라 이 스냅샷에 값이 없거나 형식이 어긋나면 null 을 돌려주고(세션은 이전 값을 유지), 스냅샷 자체를 거부하지 않는다.
 */
final class SetupPairHoldout {

    private static final int MAX_PAIRS = 256;

    private SetupPairHoldout() {
    }

    /** @return 저장할 JSON 문자열, 이 스냅샷에 쓸 값이 없으면 null */
    static String fromSnapshot(ObjectMapper mapper, JsonNode snapshot) {
        JsonNode jointBa = snapshot.path("metrics").path("joint_ba");
        if (!jointBa.isObject()) {
            return null;
        }
        JsonNode stats = jointBa.get("pair_holdout");
        JsonNode medians = jointBa.get("pair_holdout_median_px");
        boolean hasStats = stats != null && !stats.isNull();
        boolean hasMedians = medians != null && !medians.isNull();
        if (!hasStats && !hasMedians) {
            return null;
        }
        if ((hasStats && !stats.isObject()) || (hasMedians && !medians.isObject())) {
            return null;
        }
        Map<String, ObjectNode> pairs = new TreeMap<>();
        if (hasStats) {
            for (Map.Entry<String, JsonNode> e : stats.properties()) {
                JsonNode pair = e.getValue();
                if (e.getKey().isBlank() || !pair.isObject()) {
                    return null;
                }
                ObjectNode before = holdoutStats(mapper, pair.get("before"));
                ObjectNode after = holdoutStats(mapper, pair.get("after"));
                if (before == null || after == null) {
                    return null;
                }
                ObjectNode out = entry(mapper, pairs, e.getKey());
                out.set("before", before);
                out.set("after", after);
            }
        }
        if (hasMedians) {
            for (Map.Entry<String, JsonNode> e : medians.properties()) {
                JsonNode value = e.getValue();
                if (e.getKey().isBlank()) {
                    return null;
                }
                if (value.isNull()) {
                    entry(mapper, pairs, e.getKey()).putNull("median_px");
                } else if (isNonNegative(value)) {
                    entry(mapper, pairs, e.getKey()).set("median_px", value.deepCopy());
                } else {
                    return null;
                }
            }
        }
        if (pairs.isEmpty() || pairs.size() > MAX_PAIRS) {
            return null;
        }
        ObjectNode root = mapper.createObjectNode();
        JsonNode tolerance = jointBa.get("holdout_worsening_tolerance_px");
        if (tolerance != null && isNonNegative(tolerance)) {
            root.set("holdout_worsening_tolerance_px", tolerance.deepCopy());
        } else {
            root.putNull("holdout_worsening_tolerance_px");
        }
        ObjectNode out = root.putObject("pairs");
        pairs.forEach(out::set);
        return mapper.writeValueAsString(root);
    }

    private static ObjectNode entry(ObjectMapper mapper, Map<String, ObjectNode> pairs, String key) {
        return pairs.computeIfAbsent(key, k -> {
            ObjectNode node = mapper.createObjectNode();
            node.putNull("before");
            node.putNull("after");
            node.putNull("median_px");
            return node;
        });
    }

    /** 계약 holdout_stats(n, invalid 는 0 이상 정수, median·p90·p95 는 0 이상 수 또는 null). 어긋나면 null */
    private static ObjectNode holdoutStats(ObjectMapper mapper, JsonNode node) {
        if (node == null || !node.isObject()) {
            return null;
        }
        ObjectNode out = mapper.createObjectNode();
        for (String field : new String[] {"n", "invalid"}) {
            JsonNode v = node.get(field);
            if (v == null || !v.isIntegralNumber() || v.asLong() < 0) {
                return null;
            }
            out.put(field, v.asLong());
        }
        for (String field : new String[] {"median", "p90", "p95"}) {
            JsonNode v = node.get(field);
            if (v == null || v.isNull()) {
                out.putNull(field);
            } else if (isNonNegative(v)) {
                out.set(field, v.deepCopy());
            } else {
                return null;
            }
        }
        return out;
    }

    private static boolean isNonNegative(JsonNode v) {
        return v.isNumber() && Double.isFinite(v.asDouble()) && v.asDouble() >= 0;
    }
}
