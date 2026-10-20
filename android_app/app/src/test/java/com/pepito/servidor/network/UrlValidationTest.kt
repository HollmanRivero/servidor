package com.pepito.servidor.network

import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class UrlValidationTest {

    @Test
    fun acceptsValidAddresses() {
        assertNull(RetrofitClient.validateBaseUrl("http://100.115.248.101:8000"))
        assertNull(RetrofitClient.validateBaseUrl("100.115.248.101:8000"))
        assertNull(RetrofitClient.validateBaseUrl("100.115.248.101"))
        assertNull(RetrofitClient.validateBaseUrl("https://10.0.0.5:8000/"))
        assertNull(RetrofitClient.validateBaseUrl("http://your-server:8000"))
        assertNull(RetrofitClient.validateBaseUrl("192.168.1.137:8000"))
    }

    @Test
    fun rejectsInvalidOctet() {
        // Skrivefeilen som ga 30 sekunders SocketTimeoutException
        assertNotNull(RetrofitClient.validateBaseUrl("100.1115.248.101"))
        assertNotNull(RetrofitClient.validateBaseUrl("http://100.1115.248.101:8000"))
        assertNotNull(RetrofitClient.validateBaseUrl("256.1.1.1"))
    }

    @Test
    fun rejectsMalformedIp() {
        assertNotNull(RetrofitClient.validateBaseUrl("100.115.248"))
        assertNotNull(RetrofitClient.validateBaseUrl("100.115.248.101.7"))
    }

    @Test
    fun rejectsBadPort() {
        assertNotNull(RetrofitClient.validateBaseUrl("100.115.248.101:99999"))
        assertNotNull(RetrofitClient.validateBaseUrl("100.115.248.101:abc"))
        assertNotNull(RetrofitClient.validateBaseUrl("100.115.248.101:0"))
    }

    @Test
    fun rejectsEmpty() {
        assertNotNull(RetrofitClient.validateBaseUrl(""))
        assertNotNull(RetrofitClient.validateBaseUrl("   "))
    }
}
