package com.ssafy.simai.watch.config

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

// 노트북 게이트웨이 시험(gateway/site_alert/tests/test_heartbeat.py)과 같은 경우를 막는다
class WatchConfigTest {
    private val token = "dev-tok-S3cr3t+/=%41"
    private val deviceId = "watch-01"

    @Test
    fun acceptsHttpsBaseUrlAndTrimsTrailingSlash() {
        val config = WatchConfig.of(" https://ec2.example/ ", " $deviceId ", " $token ")
        assertEquals("https://ec2.example", config.ec2BaseUrl)
        assertEquals("ec2.example", config.ec2Host)
        assertEquals(deviceId, config.deviceId)
        assertEquals(token, config.deviceToken)
        assertFalse(config.plainHttp)
        assertEquals("wss://ec2.example/ws/watch", config.wsUrl)
    }

    @Test
    fun plainHttpOnlyWhenAllowed() {
        assertThrows(IllegalArgumentException::class.java) { WatchConfig.of("http://192.168.0.10:8765", deviceId, token) }
        val config = WatchConfig.of("http://192.168.0.10:8765/", deviceId, token, allowPlainHttp = true)
        assertTrue(config.plainHttp)
        assertEquals("ws://192.168.0.10:8765/ws/watch", config.wsUrl)
        // 허용해도 다른 scheme 은 안 된다
        assertThrows(IllegalArgumentException::class.java) { WatchConfig.of("ftp://ec2.example", deviceId, token, allowPlainHttp = true) }
    }

    @Test
    fun rejectsUnsafeUrlsWithoutEchoingInput() {
        val unsafe = listOf(
            "http://127.0.0.1:8443", // 평문 http 는 루프백도 안 된다(release)
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
            val e = assertThrows(url, IllegalArgumentException::class.java) { WatchConfig.of(url, deviceId, token) }
            assertFalse(url, e.message.orEmpty().contains(token))
            assertFalse(url, e.message.orEmpty().contains(url))
        }
    }

    @Test
    fun rejectsTokenWithPlusHiddenByUnquote() {
        // URLDecoder 처럼 '+' 를 공백으로만 풀면 놓친다. unquote 형태(+ 그대로)로도 찾는다
        assertThrows(IllegalArgumentException::class.java) { WatchConfig.of("https://ec2.example/%61b+cd", deviceId, "ab+cd") }
    }

    @Test
    fun rejectsBlankOrSpacedToken() {
        for (bad in listOf("", "   ", "a b")) {
            assertThrows(IllegalArgumentException::class.java) { WatchConfig.of("https://ec2.example", deviceId, bad) }
        }
    }

    @Test
    fun deviceIdFollowsTheInstanceIdPattern() {
        for (ok in listOf("watch-01", "w1", "galaxy-watch6-2")) WatchConfig.of("https://ec2.example", ok, token)
        for (bad in listOf("", "Watch-01", "-watch", "watch_01", "watch 01", "ec2 ", "a".repeat(65))) {
            assertThrows(bad, IllegalArgumentException::class.java) { WatchConfig.of("https://ec2.example", bad, token) }
        }
    }

    @Test
    fun toStringHidesToken() {
        val text = WatchConfig.of("https://ec2.example", deviceId, token).toString()
        assertFalse(text.contains(token))
        assertTrue(text.contains(deviceId))
    }
}
