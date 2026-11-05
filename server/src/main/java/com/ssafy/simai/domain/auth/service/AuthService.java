package com.ssafy.simai.domain.auth.service;

import com.ssafy.simai.domain.auth.dto.LoginRequest;
import com.ssafy.simai.domain.auth.dto.TokenResponse;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.security.PasswordPolicy;
import java.util.Optional;
import java.util.UUID;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AuthService {

    private final AppUserRepository users;
    private final PasswordEncoder passwordEncoder;
    private final AccessTokenIssuer accessTokens;
    private final RefreshTokenService refreshTokens;
    // 없는 아이디도 해시 비교를 한 번 해서 응답 시간으로 아이디 존재를 알 수 없게 한다
    private final String dummyHash;

    AuthService(
            AppUserRepository users,
            PasswordEncoder passwordEncoder,
            AccessTokenIssuer accessTokens,
            RefreshTokenService refreshTokens) {
        this.users = users;
        this.passwordEncoder = passwordEncoder;
        this.accessTokens = accessTokens;
        this.refreshTokens = refreshTokens;
        this.dummyHash = passwordEncoder.encode("simai-dummy-password");
    }

    @Transactional
    public AuthTokens login(LoginRequest request) {
        // BCrypt 가 받을 수 없는 길이는 어떤 계정의 비밀번호도 아니다(500 대신 같은 실패로 답한다)
        if (PasswordPolicy.tooLong(request.password())) {
            throw new BusinessException(ErrorCode.Auth.LOGIN_FAILED);
        }
        Optional<AppUser> found = users.findByLoginId(request.username());
        String hash = found.map(AppUser::getPasswordHash).orElse(dummyHash);
        boolean matches = passwordEncoder.matches(request.password(), hash);
        AppUser user = found.filter(u -> matches && !u.isDisabled())
                .orElseThrow(() -> new BusinessException(ErrorCode.Auth.LOGIN_FAILED));
        return issueTokens(user);
    }

    /** 갱신 토큰을 새 access·갱신 토큰으로 바꾼다. 막힌 계정이면 쓴 토큰만 폐기하고 거부한다. */
    @Transactional(noRollbackFor = BusinessException.class)
    public AuthTokens refresh(String refreshToken) {
        RefreshTokenService.ConsumedRefreshToken consumed = refreshTokens.consume(refreshToken);
        AppUser user = users.findById(consumed.userId())
                .filter(u -> !u.isDisabled())
                .orElseThrow(() -> new BusinessException(ErrorCode.Auth.INVALID_REFRESH_TOKEN));
        return issueTokens(user, consumed.familyId());
    }

    @Transactional
    public void logout(String refreshToken) {
        refreshTokens.revoke(refreshToken);
    }

    private AuthTokens issueTokens(AppUser user) {
        return issueTokens(user, null);
    }

    private AuthTokens issueTokens(AppUser user, UUID familyId) {
        AccessTokenIssuer.IssuedToken access = accessTokens.issue(user);
        RefreshTokenService.IssuedRefreshToken refresh = familyId == null
                ? refreshTokens.issue(user.getId()) : refreshTokens.issue(user.getId(), familyId);
        TokenResponse body = new TokenResponse(
                access.value(), "Bearer", access.ttl().toSeconds(), TokenResponse.User.from(user));
        return new AuthTokens(body, refresh.value(), refresh.ttl());
    }
}
