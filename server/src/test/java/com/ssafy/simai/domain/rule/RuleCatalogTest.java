package com.ssafy.simai.domain.rule;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

class RuleCatalogTest {
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
    }

    @Test
    void 모르는_규칙은_보고서에_ID를_남기고_워치에는_위험으로_표시한다() {
        assertThat(RuleCatalog.titleOf("R12")).isEqualTo("R12");
        assertThat(RuleCatalog.watchWord("R12")).isEqualTo("위험");
    }
}
