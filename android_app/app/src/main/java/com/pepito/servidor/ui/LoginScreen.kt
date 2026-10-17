package com.pepito.servidor.ui

import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.pepito.servidor.network.RetrofitClient
import com.pepito.servidor.ui.theme.DarkBackground
import com.pepito.servidor.ui.theme.LightText
import com.pepito.servidor.ui.theme.NeonBlue
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LoginScreen(onLoggedIn: () -> Unit) {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("server_prefs", Context.MODE_PRIVATE) }
    val scope = rememberCoroutineScope()

    var serverUrl by remember {
        mutableStateOf(prefs.getString("server_ip", RetrofitClient.BASE_URL) ?: "http://100.115.248.101:8000")
    }
    var apiToken by remember {
        mutableStateOf(prefs.getString("api_token", RetrofitClient.API_TOKEN) ?: "servidor123")
    }
    var isLoading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    fun connectToServer() {
        if (serverUrl.isBlank() || apiToken.isBlank()) {
            error = "Skriv inn Tailscale Server IP og X-API-Token"
            return
        }
        isLoading = true
        error = null
        RetrofitClient.BASE_URL = serverUrl.trim()
        RetrofitClient.API_TOKEN = apiToken.trim()
        prefs.edit()
            .putString("server_ip", RetrofitClient.BASE_URL)
            .putString("api_token", RetrofitClient.API_TOKEN)
            .apply()

        scope.launch {
            try {
                RetrofitClient.api.getStats()
                onLoggedIn()
            } catch (e: Exception) {
                error = "Kunne ikke koble til (${e.message}). Sjekk Tailscale IP og X-API-Token."
            } finally {
                isLoading = false
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Super-Server Tilkobling", fontWeight = FontWeight.Bold, color = LightText) },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = DarkBackground)
            )
        },
        containerColor = DarkBackground
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Text(
                text = "Koble til Tailscale-server",
                color = LightText,
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = "Skriv inn serverens Tailscale IP-adresse og X-API-Token",
                color = Color.Gray,
                style = MaterialTheme.typography.bodyMedium
            )
            Spacer(modifier = Modifier.height(24.dp))

            OutlinedTextField(
                value = serverUrl,
                onValueChange = { serverUrl = it },
                label = { Text("Tailscale Server URL (f.eks. http://100.115.248.101:8000)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(modifier = Modifier.height(12.dp))
            OutlinedTextField(
                value = apiToken,
                onValueChange = { apiToken = it },
                label = { Text("X-API-Token (f.eks. servidor123)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )

            if (error != null) {
                Spacer(modifier = Modifier.height(12.dp))
                Text(error!!, color = Color(0xFFFF5252))
            }

            Spacer(modifier = Modifier.height(24.dp))
            Button(
                onClick = { connectToServer() },
                enabled = !isLoading,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = NeonBlue)
            ) {
                if (isLoading) {
                    CircularProgressIndicator(modifier = Modifier.size(20.dp), color = DarkBackground)
                } else {
                    Text("Lagre & Koble til", color = DarkBackground, fontWeight = FontWeight.Bold)
                }
            }
        }
    }
}
