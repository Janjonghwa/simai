package com.ssafy.simai.global.contract;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Component;

/**
 * contracts/ JSON Schema(2020-12) 검사. 스키마는 빌드 때 jar 의 contracts/ 에 넣는다(contracts.gradle).
 * 지금은 관리자가 올리는 보정 문서를 검사한다(S15P31A101-120).
 */
@Component
public class ContractSchemas {

    private static final String ID_PREFIX = "https://simai.dev/contracts/";

    private final JsonSchemaFactory factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012,
            builder -> builder.schemaMappers(m -> m.mapPrefix(ID_PREFIX, "classpath:contracts/")));
    private final Map<String, JsonSchema> schemas = new ConcurrentHashMap<>();

    /** @param path 예: providers/calibration. @return 어긋난 곳(비면 통과) */
    public List<String> violations(String path, String json) {
        JsonSchema schema = schemas.computeIfAbsent(path,
                p -> factory.getSchema(SchemaLocation.of(ID_PREFIX + p + ".schema.json")));
        return schema.validate(json, InputFormat.JSON).stream().map(ValidationMessage::getMessage).toList();
    }
}
