package com.ssafy.simai;

import com.ssafy.simai.global.command.AdminCommandLine;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class SimaiApplication {

    public static void main(String[] args) {
        // java -jar simai.jar admin ... 는 관리 명령만 실행하고 끝낸다(global/command)
        if (AdminCommandLine.matches(args)) {
            System.exit(AdminCommandLine.run(SimaiApplication.class, args));
        }
        SpringApplication.run(SimaiApplication.class, args);
    }

}
