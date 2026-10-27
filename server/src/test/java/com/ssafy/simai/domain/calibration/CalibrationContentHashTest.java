package com.ssafy.simai.domain.calibration;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * Java 가 잰 content_hash 가 ai calibration/document.py 의 content_hash 와 같은지 본다. 기대값은 같은 문서를 Python
 * (json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) → sha256)으로 잰 값이다.
 */
class CalibrationContentHashTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @ParameterizedTest
    @CsvSource({
        "examples/calibration.json, sha256:75ffde54159aff5cf3cc892429f920c2b3d12ce45c3b35af89eb6f54393ae846",
        "examples/calibration.g1_auto.json, sha256:0f1bf9c84d67066566608cff792bf67d6e2316866f1a239926e0c2230a7c2ec5",
        "examples/calibration.world_frame_manual.json, sha256:273468488ca345c664578a292859b76f353c0ac5ccf80c86d8cd3b50557239c2",
        "examples/calibration.world_frame_wall_and_floor.json, sha256:5c1b6f1ffd73a114db72dde88052c78b31ad3b3d168e44dc952c601ab373cb20"
    })
    void 계약_예시_보정_문서의_해시가_Python_과_같다(String file, String expected) throws Exception {
        assertThat(CalibrationContentHash.of(MAPPER.readTree(Files.readString(CONTRACTS.resolve(file))))).isEqualTo(expected);
    }

    @Test
    void 지수형_실수와_한글_제어_문자_키_순서도_Python_과_같다() throws Exception {
        String doc = Files.readString(Path.of("src/test/resources/calibration/content-hash-edge.json"));

        assertThat(CalibrationContentHash.of(MAPPER.readTree(doc)))
                .isEqualTo("sha256:9e2936c6533f3bf29a027e9d906c4cecdd76d2dcbd4eb7336a0c340796308903");
    }

    @ParameterizedTest
    @CsvSource({
        "1e-05, 1e-05", "1e16, 1e+16", "1e15, 1000000000000000.0", "100.0, 100.0", "0.0001, 0.0001",
        "1.5e-7, 1.5e-07", "-0.0, -0.0", "2.5E+20, 2.5e+20", "12345678.9, 12345678.9", "-3.25, -3.25"
    })
    void 실수는_Python_repr_모양으로_쓴다(double value, String expected) {
        assertThat(CalibrationContentHash.pythonFloat(value)).isEqualTo(expected);
    }

    /** 2의 거듭제곱 전부(가장 가까운 후보만 보면 46개가 틀린다), 무작위 double 3000개, 경계값의 Python repr */
    @Test
    void 실수_5천여_개가_Python_repr_과_같다() throws Exception {
        List<String> lines = Files.readAllLines(Path.of("src/test/resources/calibration/python-float-repr.txt"));
        int checked = 0;
        for (String line : lines) {
            if (line.startsWith("#")) {
                continue;
            }
            String[] cols = line.split(" ");
            double value = Double.longBitsToDouble(Long.parseUnsignedLong(cols[0], 16));
            assertThat(CalibrationContentHash.pythonFloat(value)).as(cols[0]).isEqualTo(cols[1]);
            checked++;
        }
        assertThat(checked).isGreaterThan(5000);
    }

    @Test
    void 버전_시각_상태가_달라도_해시는_같다() throws Exception {
        ObjectNode doc = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.json")));
        String before = CalibrationContentHash.of(doc);
        doc.put("calibration_version", "cal-other").put("created_at", 1).put("state", "DRIFTED").put("content_hash", "sha256:0");

        assertThat(CalibrationContentHash.of(doc)).isEqualTo(before);
    }
}
