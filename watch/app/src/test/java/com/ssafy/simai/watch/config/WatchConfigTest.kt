package com.ssafy.simai.watch.config

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Test

// 노트북 게이트웨이 시험(gateway/site_alert/tests/test_heartbeat.py)과 같은 경우를 막는다
class WatchConfigTest {
    private val token = "dev-tok-S3cr3t+/=%41"

    @Test
    fun acceptsHttpsBaseUrlAndTrimsTrailingSlash() {
        val config = WatchConfig.of(" https://ec2.example/ ", " $token ")
        assertEquals("https://ec2.example", config.ec2BaseUrl)
        assertEquals("ec2.example", config.ec2Host)
        assertEquals(token, config.deviceToken)
    }

    @Test
    fun rejectsUnsafeUrlsWithoutEchoingInput() {
        val unsafe = listOf(
            "http://127.0.0.1:8443", // 평문 http 는 루프백도 안 된다
            "ftp://ec2.example",
            "https://user:pw@ec2.example",
            "https://ec2.example/?token=x",
            "https://ec2.example/#frag",
            "https://ec2.example/$token",
            "https://ec2.example/dev-tok-S3cr3t%2B%2F%3D%2541",
            "https:///nohost",
            "not a url",
            "https://ec2.example/%zz/dev-tok-S3cr3t%2B%2F%3D%2541", // 깨진 이스케이프가 있어도 나머지는 푼다
            "https://ec2.example/dev-tok-S3cr3t%2B%2F%3D%25%34%31", // 토큰의 quote 형태
        )
        for (url in unsafe) {
            val e = assertThrows(url, IllegalArgumentException::class.java) { WatchConfig.of(url, token) }
            assertFalse(url, e.message.orEmpty().contains(token))
            assertFalse(url, e.message.orEmpty().contains(url))
        }
    }

    @Test
    fun rejectsTokenWithPlusHiddenByUnquote() {
        // URLDecoder 처럼 '+' 를 공백으로만 풀면 놓친다. unquote 형태(+ 그대로)로도 찾는다
        assertThrows(IllegalArgumentException::class.java) { WatchConfig.of("https://ec2.example/%61b+cd", "ab+cd") }
    }

    @Test
    fun rejectsBlankOrSpacedToken() {
        for (bad in listOf("", "   ", "a b")) {
            assertThrows(IllegalArgumentException::class.java) { WatchConfig.of("https://ec2.example", bad) }
        }
    }

    @Test
    fun toStringHidesToken() {
        val text = WatchConfig.of("https://ec2.example", token).toString()
        assertFalse(text.contains(token))
    }
}
