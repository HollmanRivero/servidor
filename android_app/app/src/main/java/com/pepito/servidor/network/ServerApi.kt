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

    @POST("auth/login")
    suspend fun login(@Body request: LoginRequest): LoginResponse

    @POST("auth/register")
    suspend fun register(@Body request: RegisterRequest): RegisterResponse
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
}
