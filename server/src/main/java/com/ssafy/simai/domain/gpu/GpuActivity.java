package com.ssafy.simai.domain.gpu;

import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Map;
import java.util.Optional;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * GPU 를 끄면 안 되는 일과 마지막으로 GPU 를 쓴 시각(S15P31A101-702). 판단은 EC2 배포 보류(deploy/ec2/deploy.sh ACTIVE_SQL)와 같다.
 * <ul>
 * <li>관제(LIVE): 진행 중 작업(work_unit IN_PROGRESS, 현장 무관). 관제 중 Tier 0 를 끊지 않는다(명세 3.2, 규칙 5)</li>
 * <li>세팅: COMPLETED·FAILED 가 아닌 세팅 세션 또는 대기·실행 중인 맵 보강 다시 굽기(같은 GPU 의 site_setup 작업)</li>
 * <li>녹화 업로드: 녹화 조각 업로드 서명(upload_grant RECORDING_SEGMENT)이 최근 uploadHold 안에 있음(deploy.sh 10분과 같은 기준)</li>
 * <li>분석 작업: 유효한 작업 대여(job_lease ACTIVE, 만료 전). 관제 뒤 사람 지운 배경 묶음 등 GPU 워커가 일하는 중</li>
 * </ul>
 */
@Component
public class GpuActivity {

    /** 끄면 안 되는 까닭. 화면 끄기는 LIVE·SETUP 만 막고, 자동 정지는 넷 다 본다 */
    public enum Busy {
        LIVE, SETUP, UPLOAD, JOB
    }

    private static final String LIVE = "SELECT EXISTS (SELECT 1 FROM work_unit WHERE status = 'IN_PROGRESS')";
    private static final String SETUP = """
            SELECT EXISTS (SELECT 1 FROM setup_session s WHERE s.status NOT IN ('COMPLETED', 'FAILED')
                OR EXISTS (SELECT 1 FROM setup_supplement_rebake r
                           WHERE r.setup_session_id = s.setup_session_id AND r.status IN ('QUEUED', 'RUNNING')))
            """;
    private static final String UPLOAD = """
            SELECT EXISTS (SELECT 1 FROM upload_grant WHERE purpose = 'RECORDING_SEGMENT' AND issued_at > :since)
            """;
    private static final String JOB = """
            SELECT EXISTS (SELECT 1 FROM job_lease WHERE status = 'ACTIVE' AND lease_expires_at > :now)
            """;

    private final NamedParameterJdbcTemplate jdbc;

    public GpuActivity(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** 화면 끄기를 막는 까닭(관제 먼저). 없으면 비어 있다 */
    public Optional<Busy> stopBlock() {
        if (exists(LIVE, Map.of())) {
            return Optional.of(Busy.LIVE);
        }
        if (exists(SETUP, Map.of())) {
            return Optional.of(Busy.SETUP);
        }
        return Optional.empty();
    }

    /** 자동 정지를 막는 까닭. 없으면 비어 있다 */
    public Optional<Busy> autoStopBlock(Instant now, Duration uploadHold) {
        Optional<Busy> block = stopBlock();
        if (block.isPresent()) {
            return block;
        }
        if (exists(UPLOAD, Map.of("since", utc(now.minus(uploadHold))))) {
            return Optional.of(Busy.UPLOAD);
        }
        if (exists(JOB, Map.of("now", utc(now)))) {
            return Optional.of(Busy.JOB);
        }
        return Optional.empty();
    }

    /**
     * 마지막으로 GPU 를 쓴 시각: 실행(work_run) 종료, 세팅 세션 갱신, 작업 대여 활동, 녹화 조각 업로드 서명 중 가장 늦은 것.
     * 없으면 비어 있다(호출자가 켠 시각과 견준다).
     */
    public Optional<Instant> lastUse() {
        OffsetDateTime last = jdbc.queryForObject("""
                SELECT greatest(
                    (SELECT max(ended_at) FROM work_run_window),
                    (SELECT max(updated_at) FROM setup_session),
                    (SELECT max(greatest(leased_at, last_heartbeat_at, submitted_at)) FROM job_lease),
                    (SELECT max(issued_at) FROM upload_grant WHERE purpose = 'RECORDING_SEGMENT'))
                """, Map.of(), OffsetDateTime.class);
        return Optional.ofNullable(last).map(OffsetDateTime::toInstant);
    }

    private boolean exists(String sql, Map<String, ?> params) {
        return Boolean.TRUE.equals(jdbc.queryForObject(sql, new MapSqlParameterSource(params), Boolean.class));
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, java.time.ZoneOffset.UTC);
    }
}
