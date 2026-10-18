package com.pepito.servidor

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.pepito.servidor.network.RetrofitClient
import com.pepito.servidor.ui.DashboardScreen
import com.pepito.servidor.ui.EditorScreen
import com.pepito.servidor.ui.LoginScreen
import com.pepito.servidor.ui.theme.ServidorTheme
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences("server_prefs", MODE_PRIVATE)
        prefs.getString("server_ip", null)?.let { RetrofitClient.BASE_URL = it }
        prefs.getString("api_token", null)?.let { RetrofitClient.API_TOKEN = it }
        prefs.getString("auth_token", null)?.let { RetrofitClient.JWT_TOKEN = it }

        enableEdgeToEdge()
        setContent {
            ServidorTheme {
                ServidorApp()
            }
        }
    }
}

@Composable
fun ServidorApp() {
    val navController = rememberNavController()
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("server_prefs", Context.MODE_PRIVATE) }
    val startDestination = if (prefs.getString("auth_token", null) != null) "dashboard" else "login"

    NavHost(navController = navController, startDestination = startDestination) {
        composable("login") {
            LoginScreen(
                onLoggedIn = {
                    navController.navigate("dashboard") {
                        popUpTo("login") { inclusive = true }
                    }
                }
            )
        }

        composable("dashboard") {
            DashboardScreen(
                onFileClick = { path ->
                    if (path == "chat_ai_special_route") {
                        navController.navigate("chat")
                    } else {
                        val encodedPath = URLEncoder.encode(path, StandardCharsets.UTF_8.toString())
                        navController.navigate("editor/$encodedPath")
                    }
                },
                onLogout = {
                    prefs.edit().remove("auth_token").remove("username").remove("user_role").apply()
                    RetrofitClient.JWT_TOKEN = ""
                    navController.navigate("login") {
                        popUpTo(0) { inclusive = true }
                    }
                }
            )
        }

        composable("chat") {
            com.pepito.servidor.ui.ChatScreen(
                onNavigateBack = { navController.popBackStack() }
            )
        }

        composable(
            route = "editor/{path}",
            arguments = listOf(navArgument("path") { type = NavType.StringType })
        ) { backStackEntry ->
            val path = backStackEntry.arguments?.getString("path") ?: ""
            EditorScreen(
                encodedPath = path,
                onNavigateBack = { navController.popBackStack() }
            )
        }
    }
}