package com.ssafy.simai.domain.work;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.work.IncidentCounter.Alert;
import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.Test;

class IncidentCounterTest {

    private static final Instant T0 = Instant.parse("2026-11-15T03:00:00Z");

    private static Alert alert(String rule, String zone, String person, String severity, long startSec, long lastSec) {
        return new Alert(rule, zone, person, severity, T0.plusSeconds(startSec), T0.plusSeconds(lastSec));
    }

    @Test
    void 트랙_번호가_바뀌며_이어진_같은_위험은_한_사건이다() {
        var counts = IncidentCounter.count(List.of(
                alert("R2", "z1", "w-1", "CRITICAL", 0, 20),
                alert("R2", "z1", "w-2", "CRITICAL", 30, 50),
                alert("R2", "z1", "w-3", "CRITICAL", 100, 110)));
        // w-2 는 w-1 끝에서 10초 뒤, w-3 은 w-2 끝에서 50초 뒤라 모두 이어진다
        assertThat(counts.critical()).isEqualTo(1);
        assertThat(counts.high()).isZero();
    }

    @Test
    void 간격이_넘으면_따로_센다() {
        var counts = IncidentCounter.count(List.of(
                alert("R2", "z1", "w-1", "CRITICAL", 0, 10),
                alert("R2", "z1", "w-2", "CRITICAL", 200, 210)));
        assertThat(counts.critical()).isEqualTo(2);
    }

    @Test
    void 같은_사람은_시각과_상관없이_한_사건이고_규칙과_구역이_다르면_따로다() {
        var counts = IncidentCounter.count(List.of(
                alert("R2", "z1", "w-1", "HIGH", 0, 10),
                alert("R2", "z1", "w-1", "HIGH", 1000, 1010),
                alert("R3", "z1", "w-1", "HIGH", 0, 10),
                alert("R2", "z2", "w-1", "CRITICAL", 0, 10)));
        assertThat(counts.high()).isEqualTo(2);
        assertThat(counts.critical()).isEqualTo(1);
    }

    @Test
    void 사건_등급은_든_경보_중_가장_높은_등급이다() {
        var counts = IncidentCounter.count(List.of(
                alert("R2", "z1", "w-1", "HIGH", 0, 10),
                alert("R2", "z1", "w-2", "CRITICAL", 20, 30)));
        assertThat(counts.critical()).isEqualTo(1);
        assertThat(counts.high()).isZero();
    }
}
