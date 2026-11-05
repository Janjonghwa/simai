package com.ssafy.simai.domain.user.service;

import com.ssafy.simai.domain.auth.service.RefreshTokenService;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.security.PasswordPolicy;
import java.time.Clock;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 계정 생성은 관리 명령(admin create-user)으로만 한다. 계정을 만드는 API 는 두지 않는다. */
@Service
public class UserService {

    private final AppUserRepository users;
    private final PasswordEncoder passwordEncoder;
    private final Clock clock;
    private final RefreshTokenService refreshTokens;

    UserService(AppUserRepository users, PasswordEncoder passwordEncoder, Clock clock, RefreshTokenService refreshTokens) {
        this.users = users;
        this.passwordEncoder = passwordEncoder;
        this.clock = clock;
        this.refreshTokens = refreshTokens;
    }

    @Transactional
    public AppUser create(String loginId, String rawPassword, Role role, String displayLabel) {
        validatePassword(rawPassword);
        if (users.existsByLoginId(loginId)) {
            throw new BusinessException(ErrorCode.User.DUPLICATE_LOGIN_ID);
        }
        try {
            return users.saveAndFlush(
                    new AppUser(loginId, passwordEncoder.encode(rawPassword), role, displayLabel, clock.instant()));
        } catch (DataIntegrityViolationException e) {
            throw new BusinessException(ErrorCode.User.DUPLICATE_LOGIN_ID);
        }
    }

    @Transactional
    public AppUser setPassword(String loginId, String rawPassword) {
        validatePassword(rawPassword);
        AppUser user = users.findByLoginId(loginId)
                .orElseThrow(() -> new BusinessException(ErrorCode.User.USER_NOT_FOUND));
        user.changePassword(passwordEncoder.encode(rawPassword));
        refreshTokens.revokeAll(user.getId());
        return user;
    }

    private static void validatePassword(String rawPassword) {
        if (PasswordPolicy.tooShort(rawPassword)) {
            throw new BusinessException(ErrorCode.User.WEAK_PASSWORD);
        }
        if (PasswordPolicy.tooLong(rawPassword)) {
            throw new BusinessException(ErrorCode.User.PASSWORD_TOO_LONG);
        }
    }

    /**
     * 계정을 막는다(지우지 않는다). 이미 막혀 있으면 처음 막은 시각을 그대로 둔다.
     * 로그인·갱신은 disabled_at 을 보고 거부한다. 남은 갱신 토큰 폐기는 부르는 쪽이 한다.
     */
    @Transactional
    public AppUser disable(String loginId) {
        AppUser user = users.findByLoginId(loginId)
                .orElseThrow(() -> new BusinessException(ErrorCode.User.USER_NOT_FOUND));
        if (!user.isDisabled()) {
            user.disable(clock.instant());
        }
        return user;
    }
}
