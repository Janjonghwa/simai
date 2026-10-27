package com.ssafy.simai.domain.tier0.endpoint;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import tools.jackson.databind.ObjectMapper;

/**
 * tier0_endpoint(Tier 0 → EC2, I1, 명세 3.12·12.6·12.7, FR-138) → tier0_endpoint 표에 한 행 추가.
 * 이 메시지는 노트북에 그대로 전달되므로 다른 I1 메시지와 달리 계약 스키마 전체로 검사한다(ContractSchemas). 정의되지 않은 필드
 * (기기 토큰 같은 비밀 값), 대칭 매핑이 아닌 포트, camera_id 없는 영상 포트, 지문 없음을 거부해 저장하지 않는다.
 * 계약 설명대로 payload.gateway_id 는 봉투 source_id 와 같아야 한다(노트북도 다르면 접속하지 않는다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class Tier0EndpointIngestor implements I1MessageIngestor {

    private static final String SCHEMA = "messages/tier0_endpoint";

    private final Tier0EndpointRepository repository;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;

    Tier0EndpointIngestor(Tier0EndpointRepository repository, ContractSchemas schemas, ObjectMapper mapper) {
        this.repository = repository;
        this.schemas = schemas;
        this.mapper = mapper;
    }

    @Override
    public String msgType() {
        return Tier0EndpointRepository.MSG_TYPE;
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        List<String> problems = schemas.violations(SCHEMA, mapper.writeValueAsString(envelope.toContractJson(mapper)));
        if (!problems.isEmpty()) {
            // 검사 문구는 경로와 규칙만 담는다(값 없음)
            throw new InvalidI1Message("tier0_endpoint 가 계약에 맞지 않는다: " + problems);
        }
        if (!envelope.sourceId().equals(envelope.payload().get("gateway_id").stringValue())) {
            throw new InvalidI1Message("gateway_id 가 source_id 와 다르다");
        }
        return repository.insert(envelope, receivedAt) ? Outcome.STORED : Outcome.DUPLICATE;
    }
}
