package com.ssafy.simai.global.command;

import java.io.BufferedReader;
import java.io.Console;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;

/**
 * 관리 명령의 입출력. 비밀번호는 명령 인자로 받지 않고(셸 기록·프로세스 목록에 남는다) 여기서 읽는다.
 */
public record CommandIo(PrintStream out, PrintStream err, SecretReader secrets) {

    @FunctionalInterface
    public interface SecretReader {
        /** 한 줄을 읽는다. 입력이 끝났으면 null. */
        String read(String prompt);
    }

    /** 터미널이면 화면에 보이지 않게, 파이프면 표준 입력 한 줄씩 읽는다. */
    public static CommandIo system() {
        Console console = System.console();
        if (console != null) {
            return new CommandIo(System.out, System.err, prompt -> {
                char[] value = console.readPassword("%s", prompt);
                return value == null ? null : new String(value);
            });
        }
        BufferedReader stdin = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        return new CommandIo(System.out, System.err, prompt -> {
            System.err.print(prompt);
            try {
                return stdin.readLine();
            } catch (IOException e) {
                throw new UncheckedIOException(e);
            }
        });
    }
}
