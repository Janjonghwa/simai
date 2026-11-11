package com.ssafy.simai.domain.report;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.TreeMap;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 보고서 내용 해시(`sha256:<64hex>`). 객체 키를 정렬한 JSON 의 SHA-256 이다(서명·제출이 같은 규칙을 쓴다). */
final class ReportHash {

    private ReportHash() {
    }

    static String of(ObjectMapper mapper, JsonNode content) {
        return digest(mapper.writeValueAsString(sorted(mapper, content)));
    }

    private static JsonNode sorted(ObjectMapper mapper, JsonNode n) {
        if (n.isObject()) {
            TreeMap<String, JsonNode> entries = new TreeMap<>();
            n.properties().forEach(e -> entries.put(e.getKey(), e.getValue()));
            ObjectNode result = mapper.createObjectNode();
            entries.forEach((k, v) -> result.set(k, sorted(mapper, v)));
            return result;
        }
        if (n.isArray()) {
            ArrayNode result = mapper.createArrayNode();
            n.forEach(v -> result.add(sorted(mapper, v)));
            return result;
        }
        return n;
    }

    private static String digest(String value) {
        try {
            return "sha256:" + HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
