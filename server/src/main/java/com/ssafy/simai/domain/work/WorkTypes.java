package com.ssafy.simai.domain.work;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkTypes {
    public record WorkType(String code, String label, @JsonProperty("rule_tags") List<String> ruleTags) { }
    private static final List<WorkType> TYPES = List.of(
            new WorkType("SCAFFOLD_INSTALL", "비계 조립·해체 및 고소 작업발판", List.of("R2", "R3")),
            new WorkType("OPENING_WORK", "개구부 주변·단부 작업", List.of("R1", "R2")),
            new WorkType("HIGH_PLACE", "고소 작업(단상·사다리·작업면 상부)", List.of("R2", "R3", "R4")),
            new WorkType("MATERIAL_HANDLING", "자재 반입·운반·정리", List.of("R1", "R5")),
            new WorkType("EQUIPMENT_OP", "중장비·지게차·양중기 운행", List.of("R1", "R5")),
            new WorkType("FLOOR_FINISHING", "바닥·내장 마감 및 일반 통행", List.of("R1", "R4")),
            new WorkType("GENERAL", "일반 작업(기타)", List.of("R1", "R2", "R3", "R4", "R5", "R6", "R7")));
    public static boolean contains(String code) { return find(code).isPresent(); }
    public static Optional<WorkType> find(String code) {
        return code == null ? Optional.empty() : TYPES.stream().filter(t -> t.code().equals(code)).findFirst();
    }
    @GetMapping("/api/work-types")
    List<WorkType> list() { return TYPES; }
}
