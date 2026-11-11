package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.service.UserService;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Component;

/**
 * admin set-profile --login {아이디} [--label {표시 이름}] [--org {소속}] [--title {직책}]
 * TBM 리더 칸(소속·직책·성명)에 쓰는 계정 정보를 넣는다(S15P31A101-609). 주지 않은 칸은 그대로 둔다.
 */
@Component
class SetProfileCommand implements AdminCommand {

    private final UserService userService;

    SetProfileCommand(UserService userService) {
        this.userService = userService;
    }

    @Override
    public String name() {
        return "set-profile";
    }

    @Override
    public String usage() {
        return "admin set-profile --login <아이디> [--label <표시 이름>] [--org <소속>] [--title <직책>]";
    }

    @Override
    public int run(List<String> args, CommandIo io) {
        CommandOptions options = CommandOptions.parse(args, Set.of("login", "label", "org", "title"));
        String loginId = options.require("login");
        AppUser user = userService.setProfile(loginId,
                options.get("label").orElse(null), options.get("org").orElse(null), options.get("title").orElse(null));
        io.out().printf("계정 정보를 바꿨다: user_id=%d, login=%s, label=%s, org=%s, title=%s%n",
                user.getId(), user.getLoginId(), user.getDisplayLabel(), user.getOrg(), user.getTitle());
        return OK;
    }
}
