package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.auth.service.RefreshTokenService;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.service.UserService;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Component;

/**
 * admin disable-user --login {아이디}
 * 계정을 막고 그 계정의 갱신 토큰을 모두 폐기한다. 이미 나간 access 토큰은 만료(최대 30분)까지 남는다.
 */
@Component
class DisableUserCommand implements AdminCommand {

    private final UserService userService;
    private final RefreshTokenService refreshTokens;

    DisableUserCommand(UserService userService, RefreshTokenService refreshTokens) {
        this.userService = userService;
        this.refreshTokens = refreshTokens;
    }

    @Override
    public String name() {
        return "disable-user";
    }

    @Override
    public String usage() {
        return "admin disable-user --login <아이디>";
    }

    @Override
    public int run(List<String> args, CommandIo io) {
        CommandOptions options = CommandOptions.parse(args, Set.of("login"));
        AppUser user = userService.disable(options.require("login"));
        int revoked = refreshTokens.revokeAll(user.getId());
        io.out().printf("계정을 막았다: user_id=%d, login=%s, 폐기한 갱신 토큰 %d개%n",
                user.getId(), user.getLoginId(), revoked);
        return OK;
    }
}
