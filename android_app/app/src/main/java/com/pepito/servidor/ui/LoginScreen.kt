package com.pepito.servidor.ui

import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.pepito.servidor.network.ForgotPasswordRequest
import com.pepito.servidor.network.LoginRequest
import com.pepito.servidor.network.RegisterRequest
import com.pepito.servidor.network.ResetPasswordRequest
import com.pepito.servidor.network.RetrofitClient
import com.pepito.servidor.ui.theme.DarkBackground
import com.pepito.servidor.ui.theme.LightText
import com.pepito.servidor.ui.theme.NeonBlue
import kotlinx.coroutines.launch
import retrofit2.HttpException

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LoginScreen(onLoggedIn: () -> Unit) {
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var isRegister by remember { mutableStateOf(false) }
    var isLoading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var showSettings by remember { mutableStateOf(false) }
    var tempIp by remember { mutableStateOf(RetrofitClient.BASE_URL) }
    var tempToken by remember { mutableStateOf(RetrofitClient.API_TOKEN) }
    var showReset by remember { mutableStateOf(false) }
    var resetUser by remember { mutableStateOf("") }
    var resetCode by remember { mutableStateOf("") }
    var resetNewPassword by remember { mutableStateOf("") }
    var resetError by remember { mutableStateOf<String?>(null) }
    var resetInfo by remember { mutableStateOf<String?>(null) }

    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("server_prefs", Context.MODE_PRIVATE) }
    val scope = rememberCoroutineScope()

    fun doAuth() {
        if (username.isBlank() || password.isBlank()) {
            error = "Fyll inn brukernavn og passord"
            return
        }
        isLoading = true
        error = null
        scope.launch {
            try {
                if (isRegister) {
                    RetrofitClient.api.register(RegisterRequest(username.trim(), password))
                }
                val resp = RetrofitClient.api.login(LoginRequest(username.trim(), password))
                val token = resp.token
                val name = resp.user?.username ?: username.trim()
                val role = resp.user?.role ?: "user"
                RetrofitClient.JWT_TOKEN = token
                RetrofitClient.CURRENT_USER = name
                prefs.edit()
                    .putString("auth_token", token)
                    .putString("username", name)
                    .putString("user_role", role)
                    .apply()
                onLoggedIn()
            } catch (e: HttpException) {
                error = when (e.code()) {
                    401 -> "Feil brukernavn eller passord"
                    403 -> "Kontoen er bannet"
                    409 -> "Brukernavnet er allerede i bruk"
                    else -> "Serveren svarte med feil ${e.code()}"
                }
                RetrofitClient.reportAppError(
                    message = error ?: "HTTP ${e.code()}",
                    endpoint = if (isRegister) "POST /auth/register" else "POST /auth/login",
                    statusCode = e.code()
                )
            } catch (e: Exception) {
                error = "Kunne ikke nå serveren (${e.message}) – sjekk ⚙️ Innstillinger oppe til høyre"
                RetrofitClient.reportAppError(
                    message = error ?: "Nettverksfeil",
                    endpoint = if (isRegister) "POST /auth/register" else "POST /auth/login",
                    details = e.toString()
                )
            } finally {
                isLoading = false
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Super-Server v2.0", fontWeight = FontWeight.Bold, color = LightText) },
                actions = {
                    IconButton(onClick = {
                        tempIp = RetrofitClient.BASE_URL
                        tempToken = RetrofitClient.API_TOKEN
                        showSettings = true
                    }) {
                        Icon(Icons.Default.Settings, contentDescription = "Innstillinger", tint = LightText)
                    }
                },
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
                text = if (isRegister) "Lag ny bruker" else "Logg inn",
                color = LightText,
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            Spacer(modifier = Modifier.height(24.dp))

            OutlinedTextField(
                value = username,
                onValueChange = { username = it },
                label = { Text("Brukernavn") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(modifier = Modifier.height(12.dp))
            OutlinedTextField(
                value = password,
                onValueChange = { password = it },
                label = { Text("Passord") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier.fillMaxWidth()
            )

            if (error != null) {
                Spacer(modifier = Modifier.height(12.dp))
                Text(error!!, color = Color(0xFFFF5252))
            }

            Spacer(modifier = Modifier.height(24.dp))
            Button(
                onClick = { doAuth() },
                enabled = !isLoading,
                modifier = Modifier.fillMaxWidth()
            ) {
                if (isLoading) {
                    CircularProgressIndicator(modifier = Modifier.size(20.dp))
                } else {
                    Text(if (isRegister) "Registrer" else "Logg inn")
                }
            }

            TextButton(onClick = { isRegister = !isRegister; error = null }) {
                Text(
                    if (isRegister) "Har du konto? Logg inn" else "Ingen konto? Registrer deg",
                    color = NeonBlue
                )
            }
            TextButton(onClick = {
                resetUser = username.trim()
                resetInfo = null
                resetError = null
                resetCode = ""
                resetNewPassword = ""
                showReset = true
            }) {
                Text("Glemt passord?", color = NeonBlue)
            }
        }
    }

    if (showReset) {
        AlertDialog(
            onDismissRequest = { showReset = false },
            title = { Text("Glemt passord") },
            text = {
                Column {
                    Text(
                        "Be administratoren om en engangskode. " +
                            "Han lager den på serveren med: python reset_password.py --user <navn>"
                    )
                    Spacer(modifier = Modifier.height(12.dp))
                    OutlinedTextField(
                        value = resetUser,
                        onValueChange = { resetUser = it },
                        label = { Text("Brukernavn") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = resetCode,
                        onValueChange = { resetCode = it },
                        label = { Text("Engangskode (6 siffer)") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = resetNewPassword,
                        onValueChange = { resetNewPassword = it },
                        label = { Text("Nytt passord") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                        modifier = Modifier.fillMaxWidth()
                    )
                    resetError?.let { msg ->
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(msg, color = Color(0xFFFF5252))
                    }
                    resetInfo?.let { msg ->
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(msg, color = NeonBlue)
                    }
                    Spacer(modifier = Modifier.height(8.dp))
                    TextButton(
                        onClick = {
                            if (resetUser.isBlank()) {
                                resetError = "Skriv inn brukernavnet ditt"
                                return@TextButton
                            }
                            resetError = null
                            scope.launch {
                                try {
                                    val resp = RetrofitClient.api.forgotPassword(
                                        ForgotPasswordRequest(resetUser.trim())
                                    )
                                    resetInfo = resp.message
                                } catch (e: Exception) {
                                    resetError = "Kunne ikke kontakte serveren (${e.message})"
                                }
                            }
                        }
                    ) {
                        Text("Jeg har ikke fått noen kode ennå", color = NeonBlue)
                    }
                }
            },
            confirmButton = {
                Button(onClick = {
                    if (resetUser.isBlank() || resetCode.isBlank()) {
                        resetError = "Fyll inn brukernavn og engangskode"
                        return@Button
                    }
                    if (resetNewPassword.length < 4) {
                        resetError = "Nytt passord må ha minst 4 tegn"
                        return@Button
                    }
                    resetError = null
                    resetInfo = null
                    scope.launch {
                        try {
                            val resp = RetrofitClient.api.resetPassword(
                                ResetPasswordRequest(
                                    resetUser.trim(),
                                    resetCode.trim(),
                                    resetNewPassword
                                )
                            )
                            username = resetUser.trim()
                            password = ""
                            error = resp.message.ifBlank { "Passordet er endret. Logg inn." }
                            showReset = false
                        } catch (e: HttpException) {
                            resetError = when (e.code()) {
                                400 -> "Feil eller utløpt kode. Be om en ny."
                                else -> "Serveren svarte med feil ${e.code()}"
                            }
                        } catch (e: Exception) {
                            resetError = "Kunne ikke kontakte serveren (${e.message})"
                        }
                    }
                }) {
                    Text("Bytt passord")
                }
            },
            dismissButton = {
                TextButton(onClick = { showReset = false }) {
                    Text("Avbryt")
                }
            }
        )
    }

    if (showSettings) {
        AlertDialog(
            onDismissRequest = { showSettings = false },
            title = { Text("Konfigurer server") },
            text = {
                Column {
                    Text("URL til serveren (f.eks. http://100.x.y.z:8000)")
                    Spacer(modifier = Modifier.height(8.dp))
                    TextField(
                        value = tempIp,
                        onValueChange = { tempIp = it },
                        label = { Text("Tailscale Server URL") },
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(12.dp))
                    Text("X-API-Token (f.eks. servidor123)")
                    Spacer(modifier = Modifier.height(8.dp))
                    TextField(
                        value = tempToken,
                        onValueChange = { tempToken = it },
                        label = { Text("X-API-Token") },
                        singleLine = true
                    )
                    error?.let { msg ->
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(msg, color = Color(0xFFFF5252))
                    }
                }
            },
            confirmButton = {
                Button(onClick = {
                    val urlError = RetrofitClient.validateBaseUrl(tempIp)
                    if (urlError != null) {
                        error = urlError
                        return@Button
                    }
                    error = null
                    RetrofitClient.BASE_URL = tempIp
                    RetrofitClient.API_TOKEN = tempToken
                    prefs.edit()
                        .putString("server_ip", RetrofitClient.BASE_URL)
                        .putString("api_token", RetrofitClient.API_TOKEN)
                        .apply()
                    showSettings = false
                }) {
                    Text("Lagre")
                }
            },
            dismissButton = {
                TextButton(onClick = { showSettings = false }) {
                    Text("Avbryt")
                }
            }
        )
    }
}
