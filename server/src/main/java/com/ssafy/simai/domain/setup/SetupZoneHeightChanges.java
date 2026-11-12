package com.ssafy.simai.domain.setup;

import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 구역 상면 높이 차이 판정(S15P31A101-630, docs/design/S15P31A101-630-zone-height-change.md).
 * <p>I5 {@code site_setup} 1.15.0 스냅샷·결과의 {@code zone_height_changes} 를 세션에 남길 모양으로 고르고,
 * 구역 목록 응답({@code GET·PUT /api/setup-sessions/{id}/zones}, {@code POST .../zones/seed})에 구역별 {@code height_change} 를 붙인다.
 * 서버는 하트비트를 스키마로 검사하지 않으므로 여기서 모양·상태별 불변식을 본다. 표시용이라 어긋나면 저장하지 않고 이전 값을 둔다.
 * 구역 확인({@code needs_review})은 자동 처리하지 않는다(판정은 확인 순서·묶음 확인을 돕는 참고 표시다).
 */
public final class SetupZoneHeightChanges {

    private static final int MAX_JSON_BYTES = 256 * 1024;
    private static final int MAX_ZONES = 100;
    private static final int MAX_ID_LEN = 128;
    private static final Set<String> STATUSES = Set.of("UNCHANGED", "CHANGE_SUSPECTED", "UNKNOWN");
    private static final Set<String> REASONS = Set.of(
            "WORLD_NOT_ANCHORED",
            "NO_PREVIOUS_SCENE",
            "REUSE_SCENE",
            "INSUFFICIENT_COVERAGE",
            "SCENE_UNREADABLE");

    private SetupZoneHeightChanges() {
    }

    /** @return 저장할 zone_height_changes JSON 문자열, 이 스냅샷에 없거나 어긋나면 null */
    public static String fromSnapshot(ObjectMapper mapper, JsonNode snapshot) {
        if (snapshot == null || !snapshot.isObject()) {
            return null;
        }
        JsonNode node = snapshot.get("zone_height_changes");
        if (node == null || !node.isObject()) {
            return null;
        }
        if (!nullableVersionId(node.get("previous_scene_version"))
                || !nullableVersionId(node.get("compared_scene_version"))) {
            return null;
        }
        if (!finiteInRange(node.get("cell_m"), 0.02, 0.50)
                || !finiteInRange(node.get("height_diff_threshold_m"), 0.02, 1.00)
                || !finiteInRange(node.get("changed_area_ratio_threshold"), 0.01, 1.00)) {
            return null;
        }
        JsonNode zones = node.get("zones");
        if (zones == null || !zones.isArray() || zones.size() > MAX_ZONES) {
            return null;
        }
        Set<String> seenIds = new HashSet<>();
        for (JsonNode item : zones) {
            if (!validZoneItem(item, seenIds)) {
                return null;
            }
        }
        String json = mapper.writeValueAsString(node);
        return json.length() > MAX_JSON_BYTES ? null : json;
    }

    private static boolean validZoneItem(JsonNode item, Set<String> seenIds) {
        if (item == null || !item.isObject()) {
            return false;
        }
        JsonNode zoneIdNode = item.get("zone_id");
        if (zoneIdNode == null || !zoneIdNode.isString()) {
            return false;
        }
        String zoneId = zoneIdNode.asString();
        if (zoneId.isBlank() || zoneId.length() > 100 || !seenIds.add(zoneId)) {
            return false;
        }
        String status = item.path("status").asString("");
        if (!STATUSES.contains(status)) {
            return false;
        }
        JsonNode totalNode = item.get("total_cells");
        JsonNode comparedNode = item.get("compared_cells");
        if (totalNode == null || !totalNode.isIntegralNumber() || totalNode.asLong() < 0
                || comparedNode == null || !comparedNode.isIntegralNumber() || comparedNode.asLong() < 0
                || comparedNode.asLong() > totalNode.asLong()) {
            return false;
        }
        JsonNode reasonNode = item.get("reason");
        JsonNode ratioNode = item.get("changed_area_ratio");
        JsonNode maxDiffNode = item.get("max_height_diff_m");
        JsonNode maxXyNode = item.get("max_diff_xy");
        JsonNode changedCellsNode = item.get("changed_cells");

        if ("UNKNOWN".equals(status)) {
            return reasonNode != null && reasonNode.isString() && REASONS.contains(reasonNode.asString())
                    && isNull(ratioNode) && isNull(maxDiffNode) && isNull(maxXyNode) && isNull(changedCellsNode);
        }
        if (!isNull(reasonNode) || comparedNode.asLong() < 1) {
            return false;
        }
        if (changedCellsNode == null || !changedCellsNode.isIntegralNumber()
                || changedCellsNode.asLong() < 0 || changedCellsNode.asLong() > comparedNode.asLong()) {
            return false;
        }
        if (!finiteInRange(ratioNode, 0.0, 1.0) || !finiteNonNegative(maxDiffNode)) {
            return false;
        }
        if ("CHANGE_SUSPECTED".equals(status)) {
            return validVec2(maxXyNode);
        }
        return isNull(maxXyNode) || validVec2(maxXyNode);
    }

    private static boolean nullableVersionId(JsonNode node) {
        if (node == null || node.isNull()) {
            return true;
        }
        if (!node.isString()) {
            return false;
        }
        String s = node.asString();
        return !s.isBlank() && s.length() <= MAX_ID_LEN;
    }

    private static boolean isNull(JsonNode node) {
        return node == null || node.isNull();
    }

    private static boolean finiteInRange(JsonNode node, double min, double max) {
        if (node == null || !node.isNumber()) {
            return false;
        }
        double v = node.doubleValue();
        return Double.isFinite(v) && v >= min && v <= max;
    }

    private static boolean finiteNonNegative(JsonNode node) {
        if (node == null || !node.isNumber()) {
            return false;
        }
        double v = node.doubleValue();
        return Double.isFinite(v) && v >= 0.0;
    }

    private static boolean validVec2(JsonNode node) {
        if (node == null || !node.isArray() || node.size() != 2) {
            return false;
        }
        return node.get(0).isNumber() && Double.isFinite(node.get(0).doubleValue())
                && node.get(1).isNumber() && Double.isFinite(node.get(1).doubleValue());
    }

    /** 저장된 zone_height_changes JSON 에서 zone_id → 항목 맵을 뽑는다 */
    public static Map<String, JsonNode> indexByZoneId(ObjectMapper mapper, String zoneHeightChangesRaw) {
        if (zoneHeightChangesRaw == null || zoneHeightChangesRaw.isBlank()) {
            return Map.of();
        }
        JsonNode root = mapper.readTree(zoneHeightChangesRaw);
        JsonNode zones = root.path("zones");
        if (!zones.isArray()) {
            return Map.of();
        }
        Map<String, JsonNode> out = new LinkedHashMap<>();
        for (JsonNode item : zones) {
            String zoneId = item.path("zone_id").asString(null);
            if (zoneId != null && !zoneId.isBlank()) {
                out.put(zoneId, item);
            }
        }
        return out;
    }

    /**
     * 이어 쓰는 구역({@code zoneId != null})의 높이 차이 판정 객체를 돌려준다.
     * 워커가 보낸 항목이 있으면 그대로 쓰고, 아직 없거나 빠졌으면 세션 상태에 맞는 사유로 {@code UNKNOWN} 을 채운다(모르면 비운다).
     * 새로 그린 구역({@code zoneId == null})은 비교 대상 이전 구역이 아니므로 {@code null}.
     */
    public static JsonNode resolveForZone(ObjectMapper mapper, String zoneId, Map<String, JsonNode> indexed,
            boolean reuseMode, Boolean worldAnchorAccepted, boolean hasPreviousScene) {
        if (zoneId == null || zoneId.isBlank()) {
            return mapper.nullNode();
        }
        JsonNode found = indexed.get(zoneId);
        if (found != null && found.isObject()) {
            return found.deepCopy();
        }
        String reason;
        if (reuseMode) {
            reason = "REUSE_SCENE";
        } else if (Boolean.FALSE.equals(worldAnchorAccepted)) {
            reason = "WORLD_NOT_ANCHORED";
        } else if (!hasPreviousScene) {
            reason = "NO_PREVIOUS_SCENE";
        } else {
            reason = "INSUFFICIENT_COVERAGE";
        }
        ObjectNode fallback = mapper.createObjectNode();
        fallback.put("zone_id", zoneId);
        fallback.put("status", "UNKNOWN");
        fallback.put("reason", reason);
        fallback.putNull("changed_area_ratio");
        fallback.putNull("max_height_diff_m");
        fallback.putNull("max_diff_xy");
        fallback.put("total_cells", 0);
        fallback.put("compared_cells", 0);
        fallback.putNull("changed_cells");
        return fallback;
    }
}
