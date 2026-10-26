package com.ssafy.simai.domain.tier0.i1;

import java.time.Instant;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import tools.jackson.databind.JsonNode;

/**
 * I1 공통 봉투(contracts/messages/envelope.schema.json, 명세 12.1). payload 는 메시지마다 읽는 쪽이 푼다.
 * 모르는 필드는 무시한다(마이너 버전 추가는 하위 호환).
 */
public record I1Envelope(
        String schemaVersion,
        int major,
        String msgType,
        UUID msgId,
        Instant sentAt,
        String sourceId,
        JsonNode payload) {

    private static final Pattern SEMVER = Pattern.compile("^(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)$");

    /** 봉투 필드가 없거나 형식이 틀리면 InvalidI1Message 를 던진다. */
    public static I1Envelope parse(JsonNode root) {
        if (root == null || !root.isObject()) {
            throw new InvalidI1Message("봉투가 JSON 객체가 아니다");
        }
        String schemaVersion = requiredString(root, "schema_version");
        Matcher semver = SEMVER.matcher(schemaVersion);
        if (!semver.matches()) {
            throw new InvalidI1Message("schema_version 이 semver 가 아니다");
        }
        UUID msgId;
        try {
            msgId = UUID.fromString(requiredString(root, "msg_id"));
        } catch (IllegalArgumentException e) {
            throw new InvalidI1Message("msg_id 가 UUID 가 아니다");
        }
        JsonNode sentAt = root.get("sent_at");
        if (sentAt == null || !sentAt.isIntegralNumber()) {
            throw new InvalidI1Message("sent_at 이 정수 ms 가 아니다");
        }
        JsonNode payload = root.get("payload");
        if (payload == null || !payload.isObject()) {
            throw new InvalidI1Message("payload 가 객체가 아니다");
        }
        int major;
        try {
            major = Integer.parseInt(semver.group(1));
        } catch (NumberFormatException e) {
            // 정규식은 자릿수를 막지 않는다. int 를 넘는 메이저는 읽을 수 없는 판과 같다
            throw new InvalidI1Message("schema_version 메이저가 너무 크다");
        }
        return new I1Envelope(
                schemaVersion,
                major,
                requiredString(root, "msg_type"),
                msgId,
                Instant.ofEpochMilli(sentAt.longValue()),
                requiredString(root, "source_id"),
                payload);
    }

    private static String requiredString(JsonNode root, String field) {
        JsonNode node = root.get(field);
        if (node == null || !node.isString() || node.stringValue().isBlank()) {
            throw new InvalidI1Message(field + " 이 비었다");
        }
        return node.stringValue();
    }
}
