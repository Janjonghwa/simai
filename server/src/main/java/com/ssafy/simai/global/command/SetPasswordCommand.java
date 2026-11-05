package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.service.UserService;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Component;

@Component
class SetPasswordCommand implements AdminCommand {

    private final UserService userService;

    SetPasswordCommand(UserService userService) {
        this.userService = userService;
    }

    @Override
    public String name() {
        return "set-password";
    }

    @Override
    public String usage() {
        return "admin set-password --login <아이디>";
    }

    @Override
    public int run(List<String> args, CommandIo io) {
        String loginId = CommandOptions.parse(args, Set.of("login")).require("login");
        String password = io.secrets().read("비밀번호: ");
        String confirm = io.secrets().read("비밀번호 확인: ");
        if (password == null || !password.equals(confirm)) {
            io.err().println("비밀번호가 서로 다르다");
            return FAILED;
        }
        AppUser user = userService.setPassword(loginId, password);
        io.out().printf("비밀번호를 변경했다: user_id=%d, login=%s%n", user.getId(), user.getLoginId());
        return OK;
    }
}
