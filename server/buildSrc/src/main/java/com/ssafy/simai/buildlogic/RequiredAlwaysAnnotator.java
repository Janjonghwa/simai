package com.ssafy.simai.buildlogic;

import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.Map;
import java.util.Set;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.databind.JsonNode;
import com.sun.codemodel.JDefinedClass;
import com.sun.codemodel.JFieldVar;
import org.jsonschema2pojo.AbstractAnnotator;

/**
 * 계약의 필수 필드는 null 이어도 키를 싣는다(@JsonInclude(ALWAYS)). 생성 클래스는 NON_NULL 이라 그대로 두면
 * 필수 nullable 필드(evidence_ref, base_config_version, ground_anchor_xyz 등)가 직렬화에서 빠져 계약 위반이 된다.
 * 선택 필드는 NON_NULL 그대로(null = 키 없음). jsonschema2pojo 는 클래스를 만들 때(propertyInclusion) 그 스키마의
 * required 를 주고, 필드마다(propertyField)는 $ref 를 푼 필드 스키마만 주므로 클래스별 required 를 기억해 둔다.
 */
public class RequiredAlwaysAnnotator extends AbstractAnnotator {

    private final Map<JDefinedClass, Set<String>> requiredByClass = new IdentityHashMap<>();

    @Override
    public void propertyInclusion(JDefinedClass clazz, JsonNode schema) {
        Set<String> required = new HashSet<>();
        schema.path("required").forEach(name -> required.add(name.asText()));
        requiredByClass.put(clazz, required);
    }

    @Override
    public void propertyField(JFieldVar field, JDefinedClass clazz, String propertyName, JsonNode propertyNode) {
        if (requiredByClass.getOrDefault(clazz, Set.of()).contains(propertyName)) {
            field.annotate(JsonInclude.class).param("value", JsonInclude.Include.ALWAYS);
        }
    }
}
