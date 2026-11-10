package com.ssafy.simai.domain.setup;

import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 세션이 "왜 기다리는지"(wait_reason, S15P31A101-583, docs/design/setup-wait-reason.md). 이미 서버에 있는 상태만으로 고른다:
 * site_setup 작업·대여(analysis_job·job_lease), 워커 대여 요청 시각(worker.last_seen_at), 워커 반납 사유(analysis_job.error),
 * Tier 0 health 지연(system_health.pipeline_lag_ms), 세팅 하트비트의 카메라별 배경 수집률. 정상 진행 중이면 사유가 없다(null).
 * 표시용이며 작업 대여·위험 판정에 쓰지 않는다.
 */
final class SetupWaitReason {

    /** 워커 대여 long-poll 은 최대 30 s(계약 job_lease_request wait_ms). 그보다 여유 있게 지나도 다음 요청이 없으면 대여를 묻지 않는 것이다 */
    static final Duration WORKER_POLL_STALE = Duration.ofSeconds(45);
    /** Tier 0 health(1 Hz) 중 이 시간 안에 받은 것만 지금 지연의 근거로 쓴다 */
    static final Duration TIER0_HEALTH_WINDOW = Duration.ofSeconds(15);
    /** 워커가 Tier 0 우선으로 작업을 반납할 때 싣는 오류 코드(ai/services/worker/runner.py _defer) */
    static final String TIER0_RELEASE_CODE = "TIER0_PRIORITY";

    private static final Set<String> TERMINAL_SESSION = Set.of("COMPLETED", "FAILED");
    /** 이 단계부터는 관리자가 구역을 그리거나 뒤에서 굽는 중이라 기다림 표시가 필요 없다 */
    private static final Set<String> READY_OR_LATER = Set.of("READY_FOR_ZONES", "BAKING_TEXTURE", "COMPLETED");

    enum Code {
        /** site_setup 을 처리할 수 있는 워커가 등록돼 있지 않다 */
        NO_WORKER,
        /** 워커가 다른 작업을 대여해 처리 중이다(워커는 한 번에 하나) */
        WORKER_BUSY_OTHER_JOB,
        /** 워커가 대여를 묻고 있다. 곧 가져간다 */
        WAITING_FOR_WORKER,
        /**
         * 워커 접수 문이 Tier 0 우선 규칙으로 세팅을 받지 않는다. site_setup 은 지연 예산 초과만으로는 막히지 않고(S15P31A101-566)
         * 표시 파일 이상·기동 중·지연 모름 등에서 막힌다
         */
        WORKER_PAUSED_TIER0,
        /** 워커가 대여를 묻지 않는데 그 까닭을 서버가 모른다(워커 꺼짐·연결 끊김, 또는 근거 없는 접수 중지) */
        WORKER_NOT_POLLING,
        /** 워커가 작업을 받았고 아직 첫 진행 정보가 없다 */
        WORKER_STARTING,
        /** 배경 수집 중 사람 지운 배경이 덜 모인 카메라가 있다 */
        BACKGROUND_INCOMPLETE
    }

    /** WORKER_PAUSED_TIER0 의 근거. 워커가 직접 알린 반납인지, Tier 0 health(지연 모름)로 서버가 추정한 것인지 */
    enum Basis { WORKER_RELEASE, TIER0_HEALTH }

    /**
     * 판단 재료.
     *
     * @param sessionStatus      세팅 세션 단계(status)
     * @param progressLeaseId    마지막 세팅 하트비트를 보낸 대여
     * @param backgroundPct      카메라별 배경 수집률(0~100), 순서 유지
     * @param jobStatus          site_setup 작업 상태(QUEUED·LEASED·…), 작업이 없으면 null
     * @param jobCreatedAt       작업을 넣은 시각
     * @param jobErrorCode       작업에 남은 마지막 오류 코드(반납·재시도 사유), 없으면 null
     * @param jobErrorMessage    그 오류 메시지
     * @param lastLeaseActivityAt 이 작업의 대여가 마지막으로 움직인 시각(대여·하트비트·제출 중 가장 늦은 것), 없으면 null
     * @param activeLeaseId      이 작업의 유효 대여, 없으면 null
     * @param activeLeasedAt     그 대여 시각
     * @param eligibleWorkers    site_setup 을 처리할 수 있는(마지막 대여 요청 유형에 든) 폐기되지 않은 워커 수
     * @param workerLastSeenAt   그 워커들의 마지막 대여 요청 시각 중 가장 늦은 것, 없으면 null
     * @param otherJobType       그 워커 중 하나가 지금 유효하게 대여 중인 다른 작업의 유형, 없으면 null
     * @param tier0HealthSamples 최근 창 안에 받은 Tier 0 health 수
     * @param tier0LagMaxMs      그 안의 pipeline_lag_ms 최댓값(관측값, 참고로만 싣는다), 지연 값이 하나도 없으면 null
     * @param tier0LagUnknown    그 안에 지연을 모르는(pipeline_lag_ms 없음) health 가 있었다
     */
    record Facts(String sessionStatus, UUID progressLeaseId, Map<String, Double> backgroundPct, String jobStatus,
            Instant jobCreatedAt, String jobErrorCode, String jobErrorMessage, Instant lastLeaseActivityAt,
            UUID activeLeaseId, Instant activeLeasedAt, int eligibleWorkers, Instant workerLastSeenAt, String otherJobType,
            int tier0HealthSamples, Integer tier0LagMaxMs, boolean tier0LagUnknown) {
    }

    /** 고른 사유. 쓰지 않는 칸은 null(cameras 는 빈 목록) */
    record Reason(Code code, Instant since, Basis basis, String detail, Integer tier0LagMs, Instant workerLastSeenAt,
            String otherJobType, String previousErrorCode, Map<String, Double> cameras) {

        ObjectNode toJson(ObjectMapper mapper, Instant now) {
            ObjectNode out = mapper.createObjectNode();
            out.put("code", code.name());
            out.put("since_ms", since.toEpochMilli());
            out.put("waited_s", Math.max(0, Duration.between(since, now).toSeconds()));
            putText(out, "basis", basis == null ? null : basis.name());
            putText(out, "detail", detail);
            if (tier0LagMs == null) {
                out.putNull("tier0_lag_ms");
            } else {
                out.put("tier0_lag_ms", tier0LagMs);
            }
            if (workerLastSeenAt == null) {
                out.putNull("worker_last_seen_ms");
            } else {
                out.put("worker_last_seen_ms", workerLastSeenAt.toEpochMilli());
            }
            putText(out, "other_job_type", otherJobType);
            putText(out, "previous_error_code", previousErrorCode);
            ArrayNode cams = out.putArray("cameras");
            cameras.forEach((cam, pct) -> cams.addObject().put("camera_id", cam).put("collection_pct", pct));
            return out;
        }

        private static void putText(ObjectNode node, String field, String value) {
            if (value == null) {
                node.putNull(field);
            } else {
                node.put(field, value);
            }
        }
    }

    private SetupWaitReason() {
    }

    /** 이 단계에서 기다림 표시가 생길 수 있는지(끝났거나 구역 단계 이후면 재료를 읽지 않는다) */
    static boolean mayWait(String sessionStatus) {
        return sessionStatus != null && !TERMINAL_SESSION.contains(sessionStatus) && !READY_OR_LATER.contains(sessionStatus);
    }

    /** 기다리는 이유. 끝났거나 구역 단계 이후이거나 정상 진행 중이면 null */
    static Reason derive(Facts f, Instant now) {
        if (!mayWait(f.sessionStatus()) || f.jobStatus() == null) {
            return null;
        }
        if ("QUEUED".equals(f.jobStatus())) {
            return queued(f, now);
        }
        if (!"LEASED".equals(f.jobStatus()) || f.activeLeaseId() == null) {
            return null;
        }
        Instant since = f.activeLeasedAt() == null ? now : f.activeLeasedAt();
        // 이 대여가 아직 세팅 하트비트를 보내지 않았다(접수 문 재확인·준비 중). 앞 대여의 단계·수집률을 이 대여 것으로 보이지 않는다
        if (!f.activeLeaseId().equals(f.progressLeaseId())) {
            return simple(Code.WORKER_STARTING, since);
        }
        if ("COLLECTING_BACKGROUND".equals(f.sessionStatus())) {
            Map<String, Double> lacking = new LinkedHashMap<>();
            f.backgroundPct().forEach((cam, pct) -> {
                if (pct != null && pct < 100.0) {
                    lacking.put(cam, pct);
                }
            });
            if (!lacking.isEmpty()) {
                return new Reason(Code.BACKGROUND_INCOMPLETE, since, null, null, null, null, null, null, lacking);
            }
        }
        return null;
    }

    private static Reason queued(Facts f, Instant now) {
        Instant since = later(f.jobCreatedAt(), f.lastLeaseActivityAt());
        if (since == null) {
            since = now;
        }
        boolean released = TIER0_RELEASE_CODE.equals(f.jobErrorCode());
        String previousError = released ? null : f.jobErrorCode();
        String previousDetail = released ? null : f.jobErrorMessage();
        // 관측한 Tier 0 지연은 참고 사실로만 싣는다. site_setup 은 지연 예산 초과만으로는 막히지 않아(-566) 이유로 삼지 않는다
        Integer lag = f.tier0HealthSamples() > 0 ? f.tier0LagMaxMs() : null;
        if (f.eligibleWorkers() <= 0) {
            return new Reason(Code.NO_WORKER, since, null, previousDetail, null, null, null, previousError, Map.of());
        }
        if (f.otherJobType() != null) {
            return new Reason(Code.WORKER_BUSY_OTHER_JOB, since, null, previousDetail, null, f.workerLastSeenAt(),
                    f.otherJobType(), previousError, Map.of());
        }
        boolean polling = f.workerLastSeenAt() != null && now.isBefore(f.workerLastSeenAt().plus(WORKER_POLL_STALE));
        if (polling) {
            return new Reason(Code.WAITING_FOR_WORKER, since, null, previousDetail, null, f.workerLastSeenAt(), null,
                    previousError, Map.of());
        }
        if (released) {
            // 워커가 직접 알린 반납(Tier 0 우선). 메시지에 접수 문의 이유가 들어 있다
            return new Reason(Code.WORKER_PAUSED_TIER0, since, Basis.WORKER_RELEASE, f.jobErrorMessage(), lag,
                    f.workerLastSeenAt(), null, null, Map.of());
        }
        if (f.tier0HealthSamples() > 0 && f.tier0LagUnknown()) {
            // 워커는 대여를 묻지 않고, 같은 때 Tier 0 가 지연을 모른다고 알린다. 접수 문은 지연을 모르면 모든 유형을 막으므로 그렇게 추정한다
            return new Reason(Code.WORKER_PAUSED_TIER0, since, Basis.TIER0_HEALTH, previousDetail, lag,
                    f.workerLastSeenAt(), null, previousError, Map.of());
        }
        return new Reason(Code.WORKER_NOT_POLLING, since, null, previousDetail, lag, f.workerLastSeenAt(), null,
                previousError, Map.of());
    }

    private static Reason simple(Code code, Instant since) {
        return new Reason(code, since, null, null, null, null, null, null, Map.of());
    }

    private static Instant later(Instant a, Instant b) {
        if (a == null) {
            return b;
        }
        if (b == null) {
            return a;
        }
        return a.isAfter(b) ? a : b;
    }
}
