package com.ssafy.simai.domain.scene;

import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.util.List;
import java.util.Map;
import org.springframework.context.event.EventListener;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;

/** 성공 제출과 같은 트랜잭션에서 Scene 후보를 추가한다. 활성화는 관리자가 별도로 한다. */
@Component
public class SceneJobListener {

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectStorage storage;

    public SceneJobListener(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectStorage storage) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.storage = storage;
    }

    @EventListener
    public void finished(JobFinished event) {
        if (!"static_reconstruct".equals(event.jobType()) || !"SUCCEEDED".equals(event.status())) {
            return;
        }
        // 대여 재전송은 JobLeaseService 에서 걸러진다. 이벤트 재호출에도 처음 결과를 유지한다.
        if (!jdbc.queryForList("SELECT 1 FROM scene_version WHERE job_id = :job", Map.of("job", event.jobId())).isEmpty()) {
            return;
        }
        JsonNode output = event.result();
        if (output == null || !schemas.violations("experiments/scene_output", output.toString()).isEmpty()) {
            invalid();
        }
        String version = output.path("scene_version").asString();
        String calibration = output.path("calibration_version").asString();
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("job", event.jobId())
                .addValue("v", version).addValue("cal", calibration);
        List<String> sites = jdbc.queryForList("""
                SELECT c.site_id FROM analysis_job j
                JOIN calibration_version c ON c.calibration_version = :cal
                WHERE j.job_id = :job AND j.input->>'calibration_version' = :cal
                """, params, String.class);
        if (sites.isEmpty()) {
            invalid();
        }
        // 계약에서 site_id와 보정 입력 출처는 선택이다. 생략된 입력 근거를 지어내지 않는다.
        String declaredSite = output.at("/coordinate_frame/site_id").asString(null);
        JsonNode provenanceCalibration = output.path("input_provenance").get("calibration");
        if ((declaredSite != null && !declaredSite.equals(sites.getFirst()))
                || (provenanceCalibration != null && !calibration.equals(provenanceCalibration.path("calibration_version").asString()))) {
            invalid();
        }
        params.addValue("site", sites.getFirst());
        // 현재 제출 대여의 출력만 내준다. 다른 작업·대여·버킷의 객체를 열람자에게 서명하지 않는다.
        String lease = jdbc.queryForObject("""
                SELECT l.lease_id::text FROM job_lease l JOIN analysis_job j ON j.job_id = l.job_id AND j.attempt = l.attempt
                WHERE l.job_id = :job AND l.status = 'SUBMITTED'
                """, params, String.class);
        String prefix = storage.uri("static-3d/" + event.jobId() + "/" + lease + "/");
        String gltf = null;
        for (JsonNode asset : output.path("assets")) {
            String kind = asset.path("kind").asString();
            if (!"GLTF_VISUAL".equals(kind) && !"SURFACE_SOURCE_MAP".equals(kind)) {
                continue;
            }
            String uri = asset.path("uri").asString();
            String name = uri.startsWith(prefix) ? uri.substring(prefix.length()) : "";
            if (!name.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,127}") || name.contains("..")) {
                invalid();
            }
            if ("GLTF_VISUAL".equals(kind)) {
                if (gltf != null) {
                    invalid();
                }
                gltf = uri.substring(storage.uri("").length());
            }
        }
        if (gltf == null) {
            invalid();
        }
        params.addValue("gltf", gltf).addValue("output", output.toString())
                .addValue("quality", output.path("quality").toString())
                .addValue("provenance", output.path("input_provenance").toString())
                .addValue("upper", output.at("/input_provenance/is_upper_bound").asBoolean());
        try {
            jdbc.update("""
                    INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, quality,
                        input_provenance, is_upper_bound, job_id, scene_output, requested_by)
                    SELECT :v, :site, :cal, 'CANDIDATE', :gltf, CAST(:quality AS jsonb), CAST(:provenance AS jsonb),
                        :upper, :job, CAST(:output AS jsonb), requested_by FROM analysis_job WHERE job_id = :job
                    ON CONFLICT (job_id) WHERE job_id IS NOT NULL DO NOTHING
                    """, params);
        } catch (DuplicateKeyException e) {
            // 다른 작업이 같은 Scene ID 를 주장하면 덮어쓰지 않고 제출 전체를 되돌린다.
            throw new BusinessException(ErrorCode.Scene.SCENE_EXISTS);
        }
    }

    private static void invalid() {
        throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
    }
}
