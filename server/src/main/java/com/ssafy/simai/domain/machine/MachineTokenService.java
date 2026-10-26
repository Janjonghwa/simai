package com.ssafy.simai.domain.machine;

import com.ssafy.simai.contracts.JobCallbackPayload.JobType;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.security.TokenHashes;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;

/**
 * 기기·워커 토큰 발급·회전·폐기와 확인(명세 14 "기기마다 발급, 분실·교체 시 폐기", 12.5 워커 토큰, S15P31A101-222).
 * 토큰은 무작위 256비트(Base64url)이고 DB 에는 SHA-256 16진수만 둔다. 원문은 발급·회전 때 관리 명령이 한 번만 출력한다.
 * 회전하면 옛 토큰은 바로 쓸 수 없다. 폐기한 주체는 회전으로 되살리지 않는다(정기 회전이 분실 기기를 되살리지 않게).
 * 다시 쓰려면 reactivate 로 따로 되살린다. 같은 ID 를 다시 등록하지는 않는다.
 * 등록·회전·폐기 API 는 두지 않는다(11.6, 16장 "계정·기기·기준 데이터 생성"). 관리 명령으로만 한다.
 */
@Service
public class MachineTokenService {

    /** I5 작업 유형. 계약 job_request $defs.job_type 에서 만든 enum 을 그대로 쓴다(계약이 바뀌면 따라간다) */
    static final Set<String> JOB_TYPES = Arrays.stream(JobType.values()).map(JobType::value).collect(Collectors.toUnmodifiableSet());
    private static final Pattern ID = Pattern.compile("^[a-z0-9][a-z0-9-]{0,62}$");

    private final NamedParameterJdbcTemplate jdbc;
    private final Clock clock;

    public MachineTokenService(NamedParameterJdbcTemplate jdbc, Clock clock) {
        this.jdbc = jdbc;
        this.clock = clock;
    }

    /** 발급·회전 결과. token 은 원문이라 저장하지 말고 그 주체의 비밀 환경 변수에만 넣는다 */
    public record IssuedToken(String id, String token) {
    }

    public IssuedToken registerDevice(String deviceId, String deviceKind) {
        checkId(deviceId);
        if (!deviceKind.equals("LAPTOP") && !deviceKind.equals("WATCH")) {
            throw new BusinessException(ErrorCode.Machine.INVALID_DEVICE_KIND);
        }
        String token = TokenHashes.newToken();
        int rows = jdbc.update("""
                INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (:id, :kind, :hash, :now)
                ON CONFLICT (device_id) DO NOTHING
                """, new MapSqlParameterSource().addValue("id", deviceId).addValue("kind", deviceKind)
                .addValue("hash", TokenHashes.sha256Hex(token)).addValue("now", now()));
        if (rows == 0) {
            throw new BusinessException(ErrorCode.Machine.MACHINE_EXISTS);
        }
        return new IssuedToken(deviceId, token);
    }

    public IssuedToken registerWorker(String workerId, List<String> requestedJobTypes) {
        checkId(workerId);
        List<String> jobTypes = requestedJobTypes.stream().distinct().toList();
        if (jobTypes.isEmpty() || !JOB_TYPES.containsAll(jobTypes)) {
            throw new BusinessException(ErrorCode.Machine.INVALID_JOB_TYPES);
        }
        String token = TokenHashes.newToken();
        int rows = jdbc.update("""
                INSERT INTO worker (worker_id, token_hash, job_types, issued_at)
                VALUES (:id, :hash, CAST(:jobTypes AS text[]), :now)
                ON CONFLICT (worker_id) DO NOTHING
                """, new MapSqlParameterSource().addValue("id", workerId).addValue("hash", TokenHashes.sha256Hex(token))
                .addValue("jobTypes", "{" + String.join(",", jobTypes) + "}").addValue("now", now()));
        if (rows == 0) {
            throw new BusinessException(ErrorCode.Machine.MACHINE_EXISTS);
        }
        return new IssuedToken(workerId, token);
    }

    /** 새 토큰으로 바꾼다. 옛 토큰은 바로 쓸 수 없다. 폐기된 주체는 바꾸지 않는다(reactivate 를 쓴다) */
    public IssuedToken rotate(MachineKind kind, String id) {
        String token = TokenHashes.newToken();
        int rows = jdbc.update("UPDATE " + kind.table() + " SET token_hash = :hash, rotated_at = :now WHERE "
                        + kind.idColumn() + " = :id AND revoked_at IS NULL",
                new MapSqlParameterSource().addValue("id", id).addValue("hash", TokenHashes.sha256Hex(token))
                        .addValue("now", now()));
        if (rows == 0) {
            throw new BusinessException(exists(kind, id) ? ErrorCode.Machine.MACHINE_REVOKED : ErrorCode.Machine.MACHINE_NOT_FOUND);
        }
        return new IssuedToken(id, token);
    }

    /** 폐기된 주체를 새 토큰으로 되살린다. 폐기 전 토큰은 계속 쓸 수 없다 */
    public IssuedToken reactivate(MachineKind kind, String id) {
        String token = TokenHashes.newToken();
        int rows = jdbc.update("UPDATE " + kind.table() + " SET token_hash = :hash, rotated_at = :now, revoked_at = NULL WHERE "
                        + kind.idColumn() + " = :id AND revoked_at IS NOT NULL",
                new MapSqlParameterSource().addValue("id", id).addValue("hash", TokenHashes.sha256Hex(token))
                        .addValue("now", now()));
        if (rows == 0) {
            throw new BusinessException(exists(kind, id) ? ErrorCode.Machine.MACHINE_NOT_REVOKED : ErrorCode.Machine.MACHINE_NOT_FOUND);
        }
        return new IssuedToken(id, token);
    }

    /** @return 이번에 폐기했으면 true, 이미 폐기돼 있었으면 false */
    public boolean revoke(MachineKind kind, String id) {
        int rows = jdbc.update("UPDATE " + kind.table() + " SET revoked_at = :now WHERE " + kind.idColumn()
                        + " = :id AND revoked_at IS NULL",
                new MapSqlParameterSource().addValue("id", id).addValue("now", now()));
        if (rows == 0 && !exists(kind, id)) {
            throw new BusinessException(ErrorCode.Machine.MACHINE_NOT_FOUND);
        }
        return rows > 0;
    }

    /** 폐기되지 않은 토큰이면 그 주체 ID */
    public Optional<String> authenticate(MachineKind kind, String token) {
        // token_hash 는 UNIQUE(V5)라 많아야 한 행이다
        List<String> ids = jdbc.queryForList("SELECT " + kind.idColumn() + " FROM " + kind.table()
                        + " WHERE token_hash = :hash AND revoked_at IS NULL",
                new MapSqlParameterSource("hash", TokenHashes.sha256Hex(token)), String.class);
        return ids.stream().findFirst();
    }

    private boolean exists(MachineKind kind, String id) {
        Integer count = jdbc.queryForObject("SELECT count(*) FROM " + kind.table() + " WHERE " + kind.idColumn() + " = :id",
                new MapSqlParameterSource("id", id), Integer.class);
        return count != null && count > 0;
    }

    /** ID 는 소문자·숫자·-(예: laptop-01, worker-01). 계약 source_id 로도 쓴다 */
    private static void checkId(String id) {
        if (!ID.matcher(id).matches()) {
            throw new BusinessException(ErrorCode.Machine.INVALID_MACHINE_ID);
        }
    }

    private OffsetDateTime now() {
        return OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
    }
}
