package com.ssafy.simai.domain.scene;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 분석 워커가 보낸 사람 지운 배경 묶음(-363)으로 static_reconstruct 작업을 한 번만 등록한다(S15P31A101-403, 명세 3.2·5.3·12.5).
 * <ul>
 * <li>멱등 키는 묶음 폴더 이름(bundle_id)이다. 같은 묶음은 input 을 보지 않고 처음 만든 작업과 그 지금 상태를 돌려준다
 * (응답을 잃은 재전송·워커 재시작·상태 조회). 같은 묶음의 동시 요청은 advisory 잠금으로 줄 세운다.</li>
 * <li>새 작업은 관리자 등록(admin job enqueue)과 같은 job_request 계약 검사(AnalysisJobService)에 더해 Scene 등록 규칙과 같은 보정 검사
 * (이 현장의 PROVISIONAL·CALIBRATED 보정)를 거친다. 결과 Scene 은 지금처럼 SceneJobListener 가 CANDIDATE 로 붙인다.</li>
 * <li>등록하는 input 에 clean_background_bundle_id 로 묶음 ID 를 싣는다(출처·멱등 조회). 새 표를 만들지 않는다.</li>
 * <li>선택 request_id 가 있으면 해당 {@code scene_regenerate_request} 의 {@code static_reconstruct_job_id} 를 연결하고
 * {@code RECONSTRUCTING} 으로 전이한다(S15P31A101-428).</li>
 * </ul>
 * 배경 사진은 정적 3D(Visual 층) 입력이고 Tier 0 위치·위험 판정에 들어가지 않는다(규칙 1).
 */
@Service
public class StaticReconstructSubmitService {

    private static final Logger log = LoggerFactory.getLogger(StaticReconstructSubmitService.class);

    /** 작업 입력에 싣는 묶음 ID 키(계약 static_reconstruct_submit) */
    public static final String BUNDLE_KEY = "clean_background_bundle_id";
    /** -364 단계 상한 합 3780초 + 여유. -372 세션 도구 안내(admin job enqueue --timeout-ms)와 같은 값 */
    static final int TIMEOUT_MS = 4_200_000;
    /** -372 세션 도구 안내와 같은 값. 반납(LIVE_EXP_BLOCKED)은 시도로 세지 않는다 */
    static final int MAX_ATTEMPTS = 2;

    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final AnalysisJobService jobs;
    private final SiteLookup sites;
    private final SceneRegenerateService regenerateService;

    public StaticReconstructSubmitService(NamedParameterJdbcTemplate jdbc, TransactionTemplate transaction, AnalysisJobService jobs,
            SiteLookup sites, SceneRegenerateService regenerateService) {
        this.jdbc = jdbc;
        this.transaction = transaction;
        this.jobs = jobs;
        this.sites = sites;
        this.regenerateService = regenerateService;
    }

    /** @param created 이 요청으로 작업을 만들었으면 true. errorCode 는 FAILED·TIMED_OUT 의 error.code, 없으면 null */
    public record Submitted(String bundleId, String jobId, boolean created, String jobStatus, String errorCode) {
    }

    public Submitted submit(String workerId, String bundleId, JsonNode input) {
        return submit(workerId, bundleId, input, null);
    }

    /**
     * @param input 계약 검사를 통과한 static_reconstruct_submit payload.input
     * @param requestId 선택 재생성 요청 ID(S15P31A101-428), 없으면 null
     * @throws BusinessException INVALID_WORKER_MESSAGE(input 의 묶음 ID 불일치), SCENE_CALIBRATION_INVALID, INVALID_JOB
     */
    public Submitted submit(String workerId, String bundleId, JsonNode input, Long requestId) {
        if (input == null || !input.isObject()) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        JsonNode declared = input.get(BUNDLE_KEY);
        if (declared != null && !bundleId.equals(declared.asString(null))) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        return transaction.execute(status -> {
            MapSqlParameterSource params = new MapSqlParameterSource("b", bundleId);
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.static-bundle:' || :b))", params);
            List<String> existing = jdbc.queryForList("""
                    SELECT job_id FROM analysis_job
                    WHERE job_type = 'static_reconstruct' AND input->>'clean_background_bundle_id' = :b
                    ORDER BY created_at, job_id LIMIT 1
                    """, params, String.class);
            if (!existing.isEmpty()) {
                String existingJobId = existing.getFirst();
                if (requestId != null) {
                    regenerateService.onStaticReconstructSubmitted(requestId, existingJobId);
                }
                return current(bundleId, existingJobId, false);
            }
            String calibration = input.path("calibration_version").asString(null);
            List<String> state = jdbc.queryForList(
                    "SELECT state FROM calibration_version WHERE calibration_version = :cal AND site_id = :site",
                    new MapSqlParameterSource().addValue("cal", calibration).addValue("site", sites.siteId()), String.class);
            if (state.isEmpty() || !SceneActivationService.CALIBRATION_STATES.contains(state.getFirst())) {
                throw new BusinessException(ErrorCode.Scene.SCENE_CALIBRATION_INVALID);
            }
            ObjectNode stored = ((ObjectNode) input).deepCopy();
            stored.put(BUNDLE_KEY, bundleId);
            String jobId = jobs.enqueue(new JobSpec("static_reconstruct", stored, MAX_ATTEMPTS, TIMEOUT_MS, null, null));
            if (requestId != null) {
                regenerateService.onStaticReconstructSubmitted(requestId, jobId);
            }
            log.info("배경 묶음 {} 의 static_reconstruct 작업 {} 를 등록했다(워커 {}, 보정 {})", bundleId, jobId, workerId, calibration);
            return current(bundleId, jobId, true);
        });
    }

    private Submitted current(String bundleId, String jobId, boolean created) {
        Map<String, Object> row = jdbc.queryForMap(
                "SELECT status, error->>'code' AS code FROM analysis_job WHERE job_id = :job", Map.of("job", jobId));
        return new Submitted(bundleId, jobId, created, (String) row.get("status"), (String) row.get("code"));
    }
}
