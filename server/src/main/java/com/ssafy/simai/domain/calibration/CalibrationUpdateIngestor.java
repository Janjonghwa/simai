package com.ssafy.simai.domain.calibration;

import static com.ssafy.simai.domain.tier0.i1.I1Json.json;
import static com.ssafy.simai.domain.tier0.i1.I1Json.utc;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * calibration_update(Tier 0 → EC2, I1, 명세 12.2·4.4·4.5) → calibration_update_message 에 추가만 한다. 현재 상태(DRIFTED 등)는 이
 * 기록에서 읽는다(CalibrationViews). 버전 행(calibration_version)은 덮어쓰지 않는다(AGENTS 6): 처음 보는 버전이면 만들고, 이미
 * 있으면 같은 보정 내용(calibration_hash = content_hash)인지만 확인하며, 모르던 이전 버전만 채운다.
 * 버전 행에 쓰므로 계약 스키마 전체로 검사한다. 관리자 업로드(CalibrationService)와 같은 버전 잠금을 쓴다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class CalibrationUpdateIngestor implements I1MessageIngestor {

    private static final Logger log = LoggerFactory.getLogger(CalibrationUpdateIngestor.class);
    private static final String SCHEMA = "messages/calibration_update";

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final SiteLookup sites;

    CalibrationUpdateIngestor(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper,
            TransactionTemplate transaction, SiteLookup sites) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
        this.sites = sites;
    }

    @Override
    public String msgType() {
        return "calibration_update";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(2);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        List<String> problems = schemas.violations(SCHEMA, mapper.writeValueAsString(envelope.toContractJson(mapper)));
        if (!problems.isEmpty()) {
            // 검사 문구는 경로와 규칙만 담는다(값 없음)
            throw new InvalidI1Message("calibration_update 가 계약에 맞지 않는다: " + problems);
        }
        JsonNode p = envelope.payload();
        String hash = p.get("calibration_hash").asString();
        String siteId = sites.siteId();
        return transaction.execute(status -> {
            MapSqlParameterSource params = new MapSqlParameterSource("v", p.get("calibration_version").asString())
                    .addValue("previous", I1Json.text(p, "previous_calibration_version")).addValue("msgId", envelope.msgId());
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.calibration:' || :v))", params);
            if (!jdbc.queryForList("SELECT 1 FROM calibration_update_message WHERE msg_id = :msgId", params).isEmpty()) {
                return Outcome.DUPLICATE;
            }
            List<String> known = jdbc.queryForList(
                    "SELECT calibration_hash FROM calibration_version WHERE calibration_version = :v FOR UPDATE", params, String.class);
            if (known.isEmpty()) {
                insertVersion(siteId, p, receivedAt);
            } else {
                if (!known.getFirst().equals(hash) && !legacyUploadMatches(params, hash)) {
                    // 같은 버전 ID 에 다른 보정 내용. 버전은 덮어쓰지 않는다(4.5)
                    throw new InvalidI1Message("같은 calibration_version 에 다른 calibration_hash");
                }
                // 이전 버전은 이 버전이 켜지기 전 버전이라 바뀌지 않는다. 업로드로 만든 행은 모르므로(null) Tier 0 값으로 채운다
                jdbc.update("""
                        UPDATE calibration_version SET previous_calibration_version =
                            (SELECT calibration_version FROM calibration_version WHERE calibration_version = :previous)
                        WHERE calibration_version = :v AND previous_calibration_version IS NULL
                        """, params);
            }
            jdbc.update("""
                    INSERT INTO calibration_update_message (msg_id, gateway_id, calibration_version, calibration_state, reason,
                        sent_at, received_at, payload)
                    VALUES (:msgId, :gateway, :v, :state, :reason, :sentAt, :receivedAt, CAST(:payload AS jsonb))
                    """, params.addValue("gateway", envelope.sourceId()).addValue("state", p.get("calibration_state").asString())
                    .addValue("reason", p.get("reason").asString()).addValue("sentAt", utc(envelope.sentAt()))
                    .addValue("receivedAt", utc(receivedAt)).addValue("payload", mapper.writeValueAsString(p)));
            return Outcome.STORED;
        });
    }

    /** 처음 만든 버전 행에는 Tier 0 가 알린 그대로 남긴다. 이전 버전을 EC2 가 모르면(그 알림을 받기 전) 참조할 수 없어 비워 둔다 */
    private void insertVersion(String siteId, JsonNode p, Instant receivedAt) {
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, previous_calibration_version, state, provider_source,
                    reason, effective_at, calibration_ref, calibration_hash, operating_reprojection, created_at)
                VALUES (:v, :site, (SELECT calibration_version FROM calibration_version WHERE calibration_version = :previous),
                    :state, :source, :reason, :effectiveAt, :ref, :hash, CAST(:cameras AS jsonb), :receivedAt)
                """, new MapSqlParameterSource()
                .addValue("v", p.get("calibration_version").asString()).addValue("site", siteId)
                .addValue("previous", I1Json.text(p, "previous_calibration_version"))
                .addValue("state", p.get("calibration_state").asString()).addValue("source", p.get("provider_source").asString())
                .addValue("reason", p.get("reason").asString()).addValue("effectiveAt", I1Json.requiredTime(p, "effective_at"))
                .addValue("ref", p.get("calibration_ref").asString()).addValue("hash", p.get("calibration_hash").asString())
                .addValue("cameras", json(p, "cameras")).addValue("receivedAt", utc(receivedAt)));
    }

    /**
     * 이 변경 전 업로드는 버전 행 해시를 문서 원문 sha256 으로 남겼다. 올린 문서의 content_hash 를 다시 재서 Tier 0 값과 같으면
     * 같은 보정이므로 잘못 적힌 해시만 바로잡는다(보정 내용은 바뀌지 않는다).
     */
    private boolean legacyUploadMatches(MapSqlParameterSource params, String hash) {
        List<byte[]> documents = jdbc.queryForList(
                "SELECT content FROM calibration_document WHERE calibration_version = :v", params, byte[].class);
        if (documents.isEmpty() || !CalibrationContentHash.of(mapper.readTree(documents.getFirst())).equals(hash)) {
            return false;
        }
        jdbc.update("UPDATE calibration_version SET calibration_hash = :hash WHERE calibration_version = :v",
                new MapSqlParameterSource(params.getValues()).addValue("hash", hash));
        log.info("보정 버전 해시를 content_hash 로 바로잡음: calibration_version={}", params.getValue("v"));
        return true;
    }
}
