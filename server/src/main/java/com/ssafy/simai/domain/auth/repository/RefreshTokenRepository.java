package com.ssafy.simai.domain.auth.repository;

import com.ssafy.simai.domain.auth.entity.RefreshToken;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface RefreshTokenRepository extends JpaRepository<RefreshToken, UUID> {

    Optional<RefreshToken> findByTokenHash(String tokenHash);

    /** 그 시각 이후 같은 계보에 발급된 다른 토큰이 있는가(갱신으로 교체된 토큰인지 가린다). */
    boolean existsByFamilyIdAndIdNotAndIssuedAtGreaterThanEqual(UUID familyId, UUID id, Instant issuedAt);

    /** 아직 폐기되지 않았을 때만 폐기한다. 같은 토큰으로 동시에 갱신하면 하나만 1을 받는다. */
    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("update RefreshToken t set t.revokedAt = :now where t.id = :id and t.revokedAt is null")
    int revokeIfActive(@Param("id") UUID id, @Param("now") Instant now);

    /** 재사용이 감지된 로그인 계보의 토큰만 폐기한다. */
    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("update RefreshToken t set t.revokedAt = :now where t.familyId = :familyId and t.revokedAt is null")
    int revokeFamilyActive(@Param("familyId") UUID familyId, @Param("now") Instant now);

    /** 계정의 살아 있는 토큰을 모두 폐기한다. */
    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("update RefreshToken t set t.revokedAt = :now where t.userId = :userId and t.revokedAt is null")
    int revokeAllActive(@Param("userId") Long userId, @Param("now") Instant now);

    /** 그 시각 전에 만료된 토큰 행을 지운다(RefreshTokenCleaner). */
    @Modifying(flushAutomatically = true, clearAutomatically = true)
    @Query("delete from RefreshToken t where t.expiresAt < :cutoff")
    int purgeExpiredBefore(@Param("cutoff") Instant cutoff);
}
