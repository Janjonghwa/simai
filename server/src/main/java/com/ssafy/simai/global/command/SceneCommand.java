package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.scene.SceneActivationService;
import java.util.List;
import java.util.Set;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;

/** 활성화·되돌리기는 같은 명령으로 대상을 지정한다. list 로 결과와 이력을 확인한다. */
@Component
class SceneCommand implements AdminCommand {

    private final SceneActivationService scenes;
    private final NamedParameterJdbcTemplate jdbc;

    SceneCommand(SceneActivationService scenes, NamedParameterJdbcTemplate jdbc) {
        this.scenes = scenes;
        this.jdbc = jdbc;
    }

    @Override
    public String name() {
        return "scene";
    }

    @Override
    public String usage() {
        return "admin scene list --site <site_id> | activate --site <site_id> --version <scene_version> --actor <관리자> --reason <사유>";
    }

    @Override
    public int run(List<String> args, CommandIo io) {
        if (args.isEmpty()) {
            throw new CommandUsageException(usage());
        }
        List<String> rest = args.subList(1, args.size());
        switch (args.getFirst()) {
            case "activate" -> {
                CommandOptions options = CommandOptions.parse(rest, Set.of("site", "version", "actor", "reason"));
                scenes.activate(options.require("site"), options.require("version"), options.require("actor"), options.require("reason"));
                io.out().println("Scene 을 활성화했다: " + options.require("version"));
            }
            case "list" -> {
                String site = CommandOptions.parse(rest, Set.of("site")).require("site");
                var params = java.util.Map.of("site", site);
                jdbc.queryForList("""
                        SELECT scene_version, state, calibration_version, job_id, quality::text AS quality
                        FROM scene_version WHERE site_id = :site ORDER BY scene_version
                        """, params).forEach(io.out()::println);
                jdbc.queryForList("""
                        SELECT activation_id, scene_version, previous_scene_version, activated_at, actor, reason
                        FROM scene_activation WHERE site_id = :site ORDER BY activation_id DESC LIMIT 20
                        """, params).forEach(io.out()::println);
            }
            default -> throw new CommandUsageException(usage());
        }
        return OK;
    }
}
