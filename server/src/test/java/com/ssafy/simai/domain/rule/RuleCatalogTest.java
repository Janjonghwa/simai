package com.ssafy.simai.domain.rule;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.Arrays;
import org.junit.jupiter.api.Test;

class RuleCatalogTest {
    @Test
    void 미구현_R8_R10은_제외하고_표시_번호만_R1부터_R9로_잇는다() {
        assertThat(Arrays.stream(RuleCatalog.values()).map(Enum::name))
                .containsExactly("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R9", "R11");
        assertThat(Arrays.stream(RuleCatalog.values()).map(RuleCatalog::displayId))
                .containsExactly("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9");
        assertThat(RuleCatalog.displayIdOf("R1")).isEqualTo("R1");
        assertThat(RuleCatalog.displayIdOf("R7")).isEqualTo("R7");
        assertThat(RuleCatalog.displayIdOf("R9")).isEqualTo("R8");
        assertThat(RuleCatalog.displayIdOf("R11")).isEqualTo("R9");
        assertThat(RuleCatalog.displayIdOf("R8")).isEqualTo("R8");
        assertThat(RuleCatalog.displayIdOf("R10")).isEqualTo("R10");
        assertThat(RuleCatalog.displayIdOf("R99")).isEqualTo("R99");
        assertThat(RuleCatalog.displayIdOf(null)).isEmpty();
    }

    @Test
    void 워치_문구는_모든_규칙에서_I4의_8자_상한을_지킨다() {
        for (RuleCatalog rule : RuleCatalog.values()) {
            String word = RuleCatalog.watchWord(rule.name());
            assertThat(word).isNotBlank();
            assertThat(word.codePointCount(0, word.length())).isLessThanOrEqualTo(8);
        }
        assertThat(RuleCatalog.watchWord("R2")).isEqualTo("추락 위험");
        assertThat(RuleCatalog.watchWord("R6")).isEqualTo("시야 끊김");
        assertThat(RuleCatalog.watchWord("R9")).isEqualTo("안전모 미착용");
        assertThat(RuleCatalog.watchWord("R11")).isEqualTo("조끼 미착용");
    }

    @Test
    void 모르는_규칙과_제외된_R8_R10은_보고서에_ID를_남기고_워치에는_위험으로_표시한다() {
        for (String id : new String[] {"R8", "R10", "R12"}) {
            assertThat(RuleCatalog.titleOf(id)).isEqualTo(id);
            assertThat(RuleCatalog.watchWord(id)).isEqualTo("위험");
        }
    }
}
