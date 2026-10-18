package com.pepito.servidor.ui

import android.content.Context
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.InsertDriveFile
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.pepito.servidor.network.ProjectItem
import com.pepito.servidor.network.RetrofitClient
import com.pepito.servidor.network.ServerStats
import com.pepito.servidor.ui.theme.*
import kotlinx.coroutines.launch
import retrofit2.HttpException
import kotlin.math.cos
import kotlin.math.sin

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(
    onFileClick: (String) -> Unit,
    onLogout: () -> Unit
) {
    var stats by remember { mutableStateOf<ServerStats?>(null) }
    var currentPath by remember { mutableStateOf("") }
    var projects by remember { mutableStateOf<List<ProjectItem>>(emptyList()) }
    var isLoading by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var showIpDialog by remember { mutableStateOf(false) }
    var tempIp by remember { mutableStateOf(RetrofitClient.BASE_URL) }
    var tempToken by remember { mutableStateOf(RetrofitClient.API_TOKEN) }
    
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("server_prefs", Context.MODE_PRIVATE) }
    val scope = rememberCoroutineScope()

    fun loadData() {
        RetrofitClient.BASE_URL = prefs.getString("server_ip", RetrofitClient.BASE_URL) ?: RetrofitClient.BASE_URL
        RetrofitClient.API_TOKEN = prefs.getString("api_token", RetrofitClient.API_TOKEN) ?: RetrofitClient.API_TOKEN
        scope.launch {
            isLoading = true
            errorMessage = null
            try {
                stats = RetrofitClient.api.getStats()
                val response = RetrofitClient.api.listProjects(currentPath)
                projects = response.items.sortedBy { !it.is_dir }
            } catch (e: HttpException) {
                errorMessage = when (e.code()) {
                    401 -> "Feil X-API-Token (401) – trykk på ⚙️ for å sjekke X-API-Token (f.eks. servidor123)"
                    else -> "Serveren svarte med feil ${e.code()}"
                }
            } catch (e: Exception) {
                errorMessage = "Kunne ikke koble til (${e.javaClass.simpleName}): ${e.message ?: "sjekk Tailscale IP"}"
            } finally {
                isLoading = false
            }
        }
    }

    LaunchedEffect(currentPath) {
        loadData()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Super-Server Ubuntu", color = LightText)
                        val user = prefs.getString("username", null)
                        val role = prefs.getString("user_role", "user")
                        if (user != null) {
                            val roleLabel = if (role == "admin") "Administrator" else "Bruker"
                            Text("$user ($roleLabel)", color = NeonBlue, style = MaterialTheme.typography.labelSmall)
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = DarkBackground
                ),
                actions = {
                    TextButton(onClick = onLogout) {
                        Text("Logg ut", color = Color(0xFFFF5252))
                    }
                    IconButton(onClick = {
                        tempIp = RetrofitClient.BASE_URL
                        tempToken = RetrofitClient.API_TOKEN
                        showIpDialog = true
                    }) {
                        Icon(Icons.Default.Settings, contentDescription = "Konfigurer server", tint = LightText)
                    }
                    IconButton(onClick = { loadData() }) {
                        Icon(Icons.Default.Refresh, contentDescription = "Oppdater", tint = LightText)
                    }
                }
            )
        },
        floatingActionButton = {
            FloatingActionButton(
                onClick = { onFileClick("chat_ai_special_route") },
                containerColor = NeonBlue
            ) {
                Text("💬 AI", modifier = Modifier.padding(16.dp), fontWeight = FontWeight.Bold, color = DarkBackground)
            }
        },
        containerColor = DarkBackground
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .border(
                    width = 4.dp,
                    brush = Brush.verticalGradient(listOf(NeonBlue, NeonViolet)),
                    shape = RoundedCornerShape(16.dp)
                )
                .padding(16.dp)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
            ) {
                // Dashboard Cards
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = DarkSurface)
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text("Estado del Servidor", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold, color = LightText)
                        Spacer(modifier = Modifier.height(16.dp))
                        if (stats != null) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceEvenly
                            ) {
                                CircularGauge(
                                    title = "CPU",
                                    value = stats!!.cpu_percent,
                                    maxValue = 100f,
                                    unit = "%",
                                    color = NeonBlue
                                )
                                CircularGauge(
                                    title = "RAM",
                                    value = stats!!.ram_percent,
                                    maxValue = 100f,
                                    unit = "%",
                                    color = NeonViolet
                                )
                                CircularGauge(
                                    title = "TEMP",
                                    value = stats!!.temp_c,
                                    maxValue = 100f,
                                    unit = "°C",
                                    color = Color(0xFFFF5252)
                                )
                            }
                            
                            Spacer(modifier = Modifier.height(16.dp))
                            
                            Text("Disk Space: ${stats!!.disk_used_gb} GB / ${stats!!.disk_total_gb} GB", color = LightText)
                            LinearProgressIndicator(
                                progress = { stats!!.disk_percent / 100f },
                                modifier = Modifier.fillMaxWidth().height(8.dp),
                                color = NeonBlue,
                                trackColor = DarkBorder
                            )
                        } else if (isLoading) {
                            CircularProgressIndicator(modifier = Modifier.align(Alignment.CenterHorizontally), color = NeonBlue)
                        } else {
                            Text("No hay datos", color = LightText)
                        }
                    }
                }
                
                Spacer(modifier = Modifier.height(16.dp))
                
                if (errorMessage != null) {
                    Text(errorMessage!!, color = Color(0xFFFF5252))
                }

                if (showIpDialog) {
                    AlertDialog(
                        onDismissRequest = { showIpDialog = false },
                        title = { Text("Configurar Servidor") },
                        text = {
                            Column {
                                Text("Introduce la IP o dominio del servidor (ej: http://100.68.2.32:8000)")
                                Spacer(modifier = Modifier.height(8.dp))
                                TextField(
                                    value = tempIp,
                                    onValueChange = { tempIp = it },
                                    label = { Text("URL del Servidor") },
                                    singleLine = true
                                )
                                Spacer(modifier = Modifier.height(12.dp))
                                Text("Token del servidor (API_TOKEN de backend/.env)")
                                Spacer(modifier = Modifier.height(8.dp))
                                TextField(
                                    value = tempToken,
                                    onValueChange = { tempToken = it },
                                    label = { Text("X-API-Token") },
                                    singleLine = true
                                )
                            }
                        },
                        confirmButton = {
                            Button(onClick = {
                                RetrofitClient.BASE_URL = tempIp
                                RetrofitClient.API_TOKEN = tempToken
                                prefs.edit()
                                    .putString("server_ip", tempIp)
                                    .putString("api_token", tempToken.trim())
                                    .apply()
                                showIpDialog = false
                                loadData()
                            }) {
                                Text("Guardar")
                            }
                        },
                        dismissButton = {
                            TextButton(onClick = { showIpDialog = false }) {
                                Text("Cancelar")
                            }
                        }
                    )
                }

                Text("Proyectos", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold, color = NeonBlue)
                Spacer(modifier = Modifier.height(8.dp))
                
                if (currentPath.isNotEmpty()) {
                    TextButton(onClick = { 
                        val parent = currentPath.substringBeforeLast("/", "")
                        currentPath = if (currentPath.contains("/")) parent else ""
                    }) {
                        Text("⬅ Volver atrás", color = NeonViolet)
                    }
                }

                LazyColumn(modifier = Modifier.fillMaxSize()) {
                    items(projects) { item ->
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable {
                                    if (item.is_dir) {
                                        currentPath = item.path
                                    } else {
                                        onFileClick(item.path)
                                    }
                                }
                                .padding(16.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            if (item.is_dir) {
                                Icon(Icons.Default.Folder, contentDescription = "Carpeta", tint = NeonBlue)
                            } else {
                                Icon(Icons.Default.InsertDriveFile, contentDescription = "Archivo", tint = LightText)
                            }
                            Spacer(modifier = Modifier.width(16.dp))
                            Text(item.name, color = LightText)
                        }
                        HorizontalDivider(color = DarkBorder)
                    }
                }
            }
        }
    }
}

@Composable
fun CircularGauge(
    title: String,
    value: Float,
    maxValue: Float,
    unit: String,
    color: Color,
    modifier: Modifier = Modifier
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = modifier.padding(8.dp)
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.size(80.dp)) {
            Canvas(modifier = Modifier.size(80.dp)) {
                val strokeWidth = 8.dp.toPx()
                val radius = (size.minDimension - strokeWidth) / 2
                val center = Offset(size.width / 2, size.height / 2)
                
                // Draw background arc
                drawArc(
                    color = DarkBorder,
                    startAngle = 135f,
                    sweepAngle = 270f,
                    useCenter = false,
                    style = Stroke(strokeWidth, cap = StrokeCap.Round)
                )
                
                // Draw value arc
                val sweep = (value / maxValue).coerceIn(0f, 1f) * 270f
                drawArc(
                    color = color,
                    startAngle = 135f,
                    sweepAngle = sweep,
                    useCenter = false,
                    style = Stroke(strokeWidth, cap = StrokeCap.Round)
                )
                
                // Draw needle
                val angleInDegrees = 135f + sweep
                val angleInRadians = angleInDegrees * (Math.PI / 180f).toFloat()
                val needleLength = radius - 8.dp.toPx()
                val needleEndX = center.x + needleLength * cos(angleInRadians)
                val needleEndY = center.y + needleLength * sin(angleInRadians)
                
                drawLine(
                    color = color,
                    start = center,
                    end = Offset(needleEndX, needleEndY),
                    strokeWidth = 2.dp.toPx(),
                    cap = StrokeCap.Round
                )
                
                // Draw center dot
                drawCircle(
                    color = color,
                    radius = 4.dp.toPx(),
                    center = center
                )
            }
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Spacer(modifier = Modifier.height(32.dp))
                Text("${value.toInt()}$unit", style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.Bold, color = LightText)
            }
        }
        Spacer(modifier = Modifier.height(8.dp))
        Text(title, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Bold, color = LightText)
    }
}
