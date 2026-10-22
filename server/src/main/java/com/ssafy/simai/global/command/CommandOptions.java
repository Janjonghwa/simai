package com.ssafy.simai.global.command;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/** --key value 또는 --key=value 옵션. 정해 둔 키 밖의 값이나 위치 인자는 받지 않는다. */
final class CommandOptions {

    private final Map<String, String> values;

    private CommandOptions(Map<String, String> values) {
        this.values = values;
    }

    static CommandOptions parse(List<String> args, Set<String> allowed) {
        Map<String, String> values = new HashMap<>();
        for (int i = 0; i < args.size(); i++) {
            String token = args.get(i);
            if (!token.startsWith("--")) {
                throw new CommandUsageException("알 수 없는 인자: " + token);
            }
            String key;
            String value;
            int eq = token.indexOf('=');
            if (eq >= 0) {
                key = token.substring(2, eq);
                value = token.substring(eq + 1);
            } else {
                key = token.substring(2);
                if (i + 1 >= args.size()) {
                    throw new CommandUsageException("값이 없다: --" + key);
                }
                value = args.get(++i);
            }
            if (!allowed.contains(key)) {
                throw new CommandUsageException("알 수 없는 옵션: --" + key);
            }
            if (values.put(key, value) != null) {
                throw new CommandUsageException("옵션이 두 번 있다: --" + key);
            }
        }
        return new CommandOptions(values);
    }

    Optional<String> get(String key) {
        return Optional.ofNullable(values.get(key)).filter(v -> !v.isBlank());
    }

    String require(String key) {
        return get(key).orElseThrow(() -> new CommandUsageException("필수 옵션: --" + key));
    }
}
