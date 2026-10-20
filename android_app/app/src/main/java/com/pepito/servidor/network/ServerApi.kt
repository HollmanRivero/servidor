package com.pepito.servidor.network

import com.google.gson.annotations.SerializedName
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Query
import java.util.concurrent.TimeUnit

data class ServerStats(
    val cpu_percent: Float,
    val ram_percent: Float,
    val ram_used_mb: Long,
    val ram_total_mb: Long,
    val disk_used_gb: Long = 0,
    val disk_total_gb: Long = 0,
    val disk_percent: Float = 0f,
    val temp_c: Float = 0f
)

data class ProjectItem(
    val name: String,
    val is_dir: Boolean,
    val path: String
)

data class ProjectListResponse(
    val items: List<ProjectItem>
)

data class FileContentResponse(
    val content: String
)

data class SaveFileRequest(
    val content: String
)

data class SaveFileResponse(
    val message: String
)

data class ChatRequest(
    val message: String
)

data class ChatResponse(
    val reply: String
)

data class RecentFile(
    val path: String,
    val mtime: Double
)

data class RecentChangesResponse(
    val recent: List<RecentFile>
)

// --- Samarbeid mellom agentene ---

data class CollaborateRequest(
    val message: String,
    val primary: String
)

data class AgentAnswer(
    val agent: String,
    val reply: String,
    val model: String? = null
)

data class ReviewResult(
    val agent: String,
    val reply: String,
    val verdict: String = "",
    val model: String? = null
)

data class CollaborateResponse(
    val question: String = "",
    val primary: AgentAnswer,
    val review: ReviewResult,
    @SerializedName("final") val finalAnswer: AgentAnswer? = null
)

// --- Autonom kodeagent (/chat/agent) ---

data class AgentTaskRequest(
    val goal: String,
    val project: String = "",
    val primary: String = "gemini",
    val review_with_other: Boolean = true,
    val max_steps: Int = 30
)

data class AgentStepLog(
    val step: Int = 0,
    val action: String = "",
    val thought: String = "",
    val target: String = "",
    val result: String = "",
    val ok: Boolean = true
)

data class AgentTaskResponse(
    val goal: String = "",
    val project: String = "",
    val primary: String = "",
    val model: String? = null,
    val done: Boolean = false,
    val summary: String = "",
    val steps: List<AgentStepLog> = emptyList(),
    val review: ReviewResult? = null
)

// --- Autentisering mot lokal SQL-database (/auth/login & /auth/register) ---

data class UserInfo(
    val id: String = "",
    val username: String = "",
    val role: String = "",
    val is_vip: Boolean = false
)

data class LoginRequest(
    val username: String,
    val password: String
)

data class LoginResponse(
    val token: String = "",
    val user: UserInfo? = null,
    val access: String = "",
    val reason: String = ""
)

data class RegisterRequest(
    val username: String,
    val password: String,
    val email: String? = null
)

data class RegisterResponse(
    val id: String = "",
    val username: String = "",
    val role: String = ""
)

// --- Automatisk SQL-feillogging (/errors/*) ---

data class ErrorReportRequest(
    val error_message: String,
    val source: String = "android_app_v2.0",
    val endpoint: String = "",
    val status_code: Int = 0,
    val details: String = "",
    val username: String = ""
)

data class ErrorLogItem(
    val id: String = "",
    val username: String? = null,
    val source: String = "",
    val endpoint: String = "",
    val status_code: Int = 0,
    val error_message: String = "",
    val details: String = "",
    val resolved: Int = 0,
    val created_at: String = ""
)

data class ErrorLogsResponse(
    val unresolved_count: Int = 0,
    val total_returned: Int = 0,
    val errors: List<ErrorLogItem> = emptyList()
)

data class ResolveErrorRequest(
    val error_id: String = "all"
)

data class ShellRequest(
    val command: String,
    val cwd: String = "",
    val timeout: Int = 120
)

data class ShellResponse(
    val command: String = "",
    val exit_code: Int = 0,
    val stdout: String = "",
    val stderr: String = "",
    val cwd: String = "",
    val os: String = ""
)

interface ServerApiService {
    @GET("stats")
    suspend fun getStats(): ServerStats

    @GET("projects")
    suspend fun listProjects(@Query("path") path: String = ""): ProjectListResponse

    @GET("file")
    suspend fun readFile(@Query("path") path: String): FileContentResponse

    @POST("file")
    suspend fun saveFile(@Query("path") path: String, @Body data: SaveFileRequest): SaveFileResponse

    @POST("chat/gemini")
    suspend fun chatWithGemini(@Body request: ChatRequest): ChatResponse

    @POST("chat/deepseek")
    suspend fun chatWithDeepseek(@Body request: ChatRequest): ChatResponse

    @POST("chat/techai")
    suspend fun chatWithTechAi(@Body request: ChatRequest): ChatResponse

    @GET("recent_changes")
    suspend fun getRecentChanges(): RecentChangesResponse

    @POST("chat/collaborate")
    suspend fun collaborate(@Body request: CollaborateRequest): CollaborateResponse

    @POST("chat/agent")
    suspend fun runAgent(@Body request: AgentTaskRequest): AgentTaskResponse

    @POST("shell")
    suspend fun runShell(@Body request: ShellRequest): ShellResponse

    @POST("auth/login")
    suspend fun login(@Body request: LoginRequest): LoginResponse

    @POST("auth/register")
    suspend fun register(@Body request: RegisterRequest): RegisterResponse

    @POST("errors/report")
    suspend fun reportError(@Body request: ErrorReportRequest)

    @GET("errors")
    suspend fun getErrors(@Query("only_unresolved") onlyUnresolved: Boolean = false): ErrorLogsResponse

    @POST("errors/resolve")
    suspend fun resolveErrors(@Body request: ResolveErrorRequest)
}

object RetrofitClient {
    private fun normalizeBaseUrl(raw: String): String {
        var u = raw.trim()
        if (u.isEmpty()) return ""
        if (!u.startsWith("http://") && !u.startsWith("https://")) {
            u = "http://$u"
        }
        if (!u.endsWith("/")) {
            u = "$u/"
        }
        return u
    }

    /** Returnerer null hvis adressen er brukbar, ellers en feilmelding til brukeren.
     * Uten denne godtas en skrivefeil som "100.1115.248.101" og appen venter
     * 30 sekunder på en vert som ikke kan finnes. */
    fun validateBaseUrl(raw: String): String? {
        val u = raw.trim()
        if (u.isEmpty()) return "Skriv inn serveradressen, f.eks. http://100.x.y.z:8000"

        val withoutScheme = u.removePrefix("https://").removePrefix("http://")
        val authority = withoutScheme.substringBefore('/').substringBefore('?')
        if (authority.isEmpty()) return "Mangler vertsnavn"

        val host = authority.substringBeforeLast(':', authority)
        val portText = if (authority.contains(':')) authority.substringAfterLast(':') else ""
        if (portText.isNotEmpty()) {
            val port = portText.toIntOrNull() ?: return "Ugyldig port: $portText"
            if (port !in 1..65535) return "Ugyldig port: $port"
        }
        if (host.isEmpty()) return "Mangler vertsnavn"

        if (host.all { it.isDigit() || it == '.' }) {
            val parts = host.split(".")
            if (parts.size != 4) return "Ugyldig IP-adresse: $host"
            for (part in parts) {
                val n = part.toIntOrNull() ?: return "Ugyldig IP-adresse: $host"
                if (n !in 0..255) return "Ugyldig IP-adresse: $host (hver del må være 0-255)"
            }
        } else if (!host.matches(Regex("[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*"))) {
            return "Ugyldig vertsnavn: $host"
        }
        return null
    }

    private var _baseUrl = "http://100.115.248.101:8000/"
    var BASE_URL: String
        get() = _baseUrl
        set(value) {
            val normalized = normalizeBaseUrl(value)
            if (normalized.isNotEmpty()) {
                _baseUrl = normalized
                _api = null
            }
        }

    private var _apiToken: String = "servidor123"
    var API_TOKEN: String
        get() = _apiToken
        set(value) {
            _apiToken = value.trim()
            _api = null
        }

    private var _jwtToken: String = ""
    var JWT_TOKEN: String
        get() = _jwtToken
        set(value) {
            _jwtToken = value.trim()
            _api = null
        }

    var CURRENT_USER: String = ""

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(600, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                val builder = chain.request().newBuilder()
                if (_jwtToken.isNotEmpty()) {
                    builder.header("Authorization", "Bearer $_jwtToken")
                }
                if (_apiToken.isNotEmpty()) {
                    builder.header("X-API-Token", _apiToken)
                }
                chain.proceed(builder.build())
            }
            .build()
    }

    private var _api: ServerApiService? = null
    val api: ServerApiService
        get() {
            if (_api == null) {
                _api = Retrofit.Builder()
                    .baseUrl(_baseUrl)
                    .client(httpClient)
                    .addConverterFactory(GsonConverterFactory.create())
                    .build()
                    .create(ServerApiService::class.java)
            }
            return _api!!
        }

    suspend fun reportAppError(
        message: String,
        endpoint: String = "",
        statusCode: Int = 0,
        details: String = ""
    ) {
        try {
            api.reportError(
                ErrorReportRequest(
                    error_message = message,
                    source = "android_app_v2.0",
                    endpoint = endpoint,
                    status_code = statusCode,
                    details = details,
                    username = CURRENT_USER
                )
            )
        } catch (_: Exception) {
            // Unngå krasj dersom serveren ikke er nåbar akkurat da
        }
    }
}
