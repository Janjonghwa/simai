package com.ssafy.simai.global.command;

import java.util.List;

/**
 * EC2 에서 부르는 관리 명령 하나. java -jar simai.jar admin {name} --옵션 값 ...
 * 계정·기기 토큰·워커 토큰 발급·회전·폐기를 같은 틀에 붙인다(API 로 열지 않는다).
 */
public interface AdminCommand {

    /** 성공. */
    int OK = 0;
    /** 명령은 맞게 불렀지만 처리하지 못했다(중복 아이디 등). */
    int FAILED = 1;
    /** 옵션이 틀렸다. */
    int USAGE = 2;

    String name();

    /** 사용법 한 줄. 옵션이 틀렸을 때 보여 준다. */
    String usage();

    int run(List<String> args, CommandIo io);
}
