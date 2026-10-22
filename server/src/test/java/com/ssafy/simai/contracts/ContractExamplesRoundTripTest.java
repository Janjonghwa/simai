package com.ssafy.simai.contracts;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * contracts/ 에서 생성한 Java 클래스(contracts.gradle)로 contracts/examples 의 메시지 예시를 읽고 다시 써서
 * 같은 JSON 이 나오는지 본다(명세 21.2, S15P31A101-24). 숫자는 값으로 비교한다(1 과 1.0 은 같다).
 * 한계: 선택 필드의 null 과 없음을 구분하지 못한다(둘 다 Java null, 쓰면 키 없음). 필수 필드는 null 이어도 키를 싣는다.
 */
class ContractExamplesRoundTripTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final String NULL_DROPPED = " null 빠짐";
    /**
     * 예시에 null 로 실린 선택 nullable 필드(스키마 required 에 없음). Java 는 null 과 없음을 구분하지 못해 다시 쓰면 빠진다.
     * 여기 없는 null 키가 빠지면(필수 필드 누락 등) 실패한다. 예시에 새 선택 null 이 생기면 스키마에서 선택인지 확인하고 더한다.
     */
    private static final java.util.Set<String> OPTIONAL_NULL_DROPS = java.util.Set.of(
            "health:/payload/cameras/*/sync_valid",
            "health:/payload/laptop_clock_offset",
            "job_callback:/payload/started_at",
            "job_heartbeat:/payload/progress",
            "observation_summary:/payload/cameras/*/last_capture",
            "risk_event:/payload/resolved_at",
            "world_state:/payload/tracks/*/head_z_sigma");
    private static final Comparator<JsonNode> NUMERIC = (a, b) ->
            a.isNumber() && b.isNumber() ? a.decimalValue().compareTo(b.decimalValue()) : a.equals(b) ? 0 : 1;

    static Stream<Path> messageExamples() throws IOException {
        try (Stream<Path> files = Files.list(CONTRACTS.resolve("examples"))) {
            List<Path> examples = files
                    .filter(p -> p.getFileName().toString().endsWith(".json"))
                    .filter(p -> Files.exists(CONTRACTS.resolve("messages").resolve(schemaName(p) + ".schema.json")))
                    .sorted()
                    .toList();
            assertThat(examples).hasSizeGreaterThan(50);
            return examples.stream();
        }
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("messageExamples")
    void exampleRoundTrips(Path example) throws Exception {
        JsonNode original = withoutMeta(MAPPER.readTree(example.toFile()));
        Object message = MAPPER.treeToValue(original, messageClass(schemaName(example)));
        JsonNode written = MAPPER.valueToTree(message);
        String schema = schemaName(example);
        List<String> unexpected = differences(original, written, "").stream()
                .filter(d -> !(d.endsWith(NULL_DROPPED)
                        && OPTIONAL_NULL_DROPS.contains(schema + ":" + d.replace(NULL_DROPPED, "").replaceAll("/[0-9]+", "/*"))))
                .toList();
        assertThat(unexpected).as("다시 쓴 JSON 이 원본과 다른 곳").isEmpty();
    }

    @Test
    void requiredNullStaysAndOptionalAbsentStaysAbsent() throws Exception {
        // 필수 nullable(ground_anchor_xyz)은 null 이어도 키를 싣고, 없던 선택 필드(surface_source·aliases)는 생기지 않는다
        JsonNode doc = withoutMeta(MAPPER.readTree(CONTRACTS.resolve("examples/world_state.json").toFile()));
        WorldState state = MAPPER.treeToValue(doc, WorldState.class);
        JsonNode written = MAPPER.valueToTree(state);
        boolean sawNullAnchor = false;
        for (JsonNode track : written.get("payload").get("tracks")) {
            assertThat(track.has("ground_anchor_xyz")).isTrue();
            if (track.get("ground_anchor_xyz").isNull()) {
                sawNullAnchor = true;
                assertThat(track.has("surface_source")).isFalse();
            }
            assertThat(track.has("evidence_ref")).isFalse();
        }
        assertThat(sawNullAnchor).as("예시에 접지점 null 트랙이 있어야 이 시험이 뜻이 있다").isTrue();
    }

    @Test
    void writingRequiredNullableFieldKeepsNullKey() throws Exception {
        // 서버가 쓰는 risk_plan_request: base_config_version 은 필수이고 null 일 수 있다(빠지면 INVALID_REQUEST)
        JsonNode doc = withoutMeta(MAPPER.readTree(CONTRACTS.resolve("examples/risk_plan_request.json").toFile()));
        RiskPlanRequest request = MAPPER.treeToValue(doc, RiskPlanRequest.class);
        request.getPayload().setBaseConfigVersion(null);
        JsonNode written = MAPPER.valueToTree(request);
        assertThat(written.get("payload").has("base_config_version")).isTrue();
        assertThat(written.get("payload").get("base_config_version").isNull()).isTrue();
    }

    @Test
    void unknownFieldsInOpenObjectsSurvive() throws Exception {
        ObjectNode doc = (ObjectNode) withoutMeta(MAPPER.readTree(CONTRACTS.resolve("examples/world_state.json").toFile()));
        doc.put("future_envelope_field", 1);
        ((ObjectNode) doc.get("payload")).putObject("future_payload_field").put("a", "b");
        ((ObjectNode) doc.get("payload").get("tracks").get(0)).put("future_track_field", true);
        JsonNode written = MAPPER.valueToTree(MAPPER.treeToValue(doc, WorldState.class));
        assertThat(written.equals(NUMERIC, doc)).as("결과 %s", written).isTrue();
    }

    @Test
    void int64MillisecondsAreNotTruncated() throws Exception {
        ObjectNode doc = (ObjectNode) withoutMeta(MAPPER.readTree(CONTRACTS.resolve("examples/world_state.json").toFile()));
        doc.put("sent_at", Long.MAX_VALUE);
        WorldState state = MAPPER.treeToValue(doc, WorldState.class);
        assertThat(state.getSentAt()).isEqualTo(Long.MAX_VALUE);
        assertThat(MAPPER.valueToTree(state).get("sent_at").longValue()).isEqualTo(Long.MAX_VALUE);
    }

    /** 두 JSON 이 다른 경로(키가 한쪽에만 있음, 값이 다름). 숫자는 값으로 비교한다. */
    private static List<String> differences(JsonNode expected, JsonNode actual, String path) {
        List<String> out = new java.util.ArrayList<>();
        if (expected.isObject() && actual.isObject()) {
            java.util.Set<String> keys = new java.util.TreeSet<>(expected.propertyNames());
            keys.addAll(actual.propertyNames());
            for (String key : keys) {
                if (expected.has(key) && expected.get(key).isNull() && !actual.has(key)) {
                    out.add(path + "/" + key + NULL_DROPPED);
                } else if (!expected.has(key) || !actual.has(key)) {
                    out.add(path + "/" + key + (expected.has(key) ? " 빠짐" : " 생김"));
                } else {
                    out.addAll(differences(expected.get(key), actual.get(key), path + "/" + key));
                }
            }
        } else if (expected.isArray() && actual.isArray() && expected.size() == actual.size()) {
            for (int i = 0; i < expected.size(); i++) {
                out.addAll(differences(expected.get(i), actual.get(i), path + "/" + i));
            }
        } else if (!actual.equals(NUMERIC, expected)) {
            out.add(path + ": " + expected + " -> " + actual);
        }
        return out;
    }

    @Test
    void generatedFieldsKeepTheirTypes() throws Exception {
        // nullable 갈래(oneOf [타입, null])는 설명문이 붙어 있어도 그 타입으로 만든다(Object 로 잃지 않음)
        assertThat(Detection.class.getDeclaredField("keypoints2d").getGenericType().getTypeName())
                .isEqualTo("java.util.List<java.util.List<java.lang.Double>>");
        assertThat(WorldTrack.class.getDeclaredField("groundAnchorXyz").getGenericType().getTypeName())
                .isEqualTo("java.util.List<java.lang.Double>");
        assertThat(FrameHeader.class.getDeclaredField("streamEpoch").getType()).isEqualTo(Long.class);
        // 생성 클래스 전체에 타입을 잃은 필드(Object, List<Object>)가 없다. 모르는 필드 맵(additionalProperties)만 Object 값이다
        // 시험 클래스도 같은 패키지라 getResource("") 는 시험 출력 폴더를 준다. 생성 클래스가 있는 main 출력 폴더를 쓴다
        Path dir = Path.of(Detection.class.getProtectionDomain().getCodeSource().getLocation().toURI())
                .resolve(Detection.class.getPackageName().replace('.', '/'));
        List<String> untyped = new java.util.ArrayList<>();
        try (Stream<Path> classes = Files.list(dir)) {
            List<Path> files = classes.filter(f -> f.toString().endsWith(".class")).toList();
            assertThat(files).hasSizeGreaterThan(100);
            for (Path file : files) {
                String name = file.getFileName().toString().replace(".class", "");
                Class<?> clazz = Class.forName(Detection.class.getPackageName() + "." + name);
                for (java.lang.reflect.Field field : clazz.getDeclaredFields()) {
                    String type = field.getGenericType().getTypeName();
                    if (!field.getName().equals("additionalProperties") && (type.equals("java.lang.Object") || type.contains("<java.lang.Object>"))) {
                        untyped.add(name + "." + field.getName() + ": " + type);
                    }
                }
            }
        }
        assertThat(untyped).isEmpty();
    }

    private static String schemaName(Path example) {
        String file = example.getFileName().toString();
        return file.substring(0, file.indexOf('.'));
    }

    private static Class<?> messageClass(String schemaName) throws ClassNotFoundException {
        StringBuilder name = new StringBuilder(ContractExamplesRoundTripTest.class.getPackageName()).append('.');
        for (String part : schemaName.split("_")) {
            name.append(Character.toUpperCase(part.charAt(0))).append(part.substring(1));
        }
        return Class.forName(name.toString());
    }

    /** 밑줄로 시작하는 최상위 키(_expect_error 등)는 예시 설명이라 뺀다(validate_examples.py 와 같다). */
    private static JsonNode withoutMeta(JsonNode doc) {
        ObjectNode copy = ((ObjectNode) doc).deepCopy();
        Iterator<Map.Entry<String, JsonNode>> fields = doc.properties().iterator();
        while (fields.hasNext()) {
            String key = fields.next().getKey();
            if (key.startsWith("_")) {
                copy.remove(key);
            }
        }
        return copy;
    }
}
