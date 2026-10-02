package com.pepito.servidor.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.pepito.servidor.network.AgentRequest
import com.pepito.servidor.network.CollaborateRequest
import com.pepito.servidor.network.RetrofitClient
import kotlinx.coroutines.launch

data class ChatMessage(val text: String, val isUser: Boolean, val senderName: String = "Du")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(onNavigateBack: () -> Unit) {
    var selectedAI by remember { mutableStateOf("Gemini") }
    // true = begge jobber sammen: den valgte svarer, den andre kontrollerer og retter
    var collaborateMode by remember { mutableStateOf(false) }
    var messages by remember { mutableStateOf(listOf(
        ChatMessage("Hei! Hvilken AI vil du prate med i dag?", false, "System")
    )) }
    var inputText by remember { mutableStateOf("") }
    var isTyping by remember { mutableStateOf(false) }
    var conversationId by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    fun sendMessage(text: String) {
        if (text.isBlank() || isTyping) return
        val userText = text
        messages = messages + ChatMessage(userText, true)
        inputText = ""
        isTyping = true
        scope.launch {
            try {
                // Sjekk om det er spørsmål om nylige endringer
                var finalPrompt = userText
                if (userText.contains("Hva er nytt") || userText.contains("sist endrede filen")) {
                    try {
                        val changes = RetrofitClient.api.getRecentChanges()
                        val filesList = changes.recent.joinToString(", ") { it.path }
                        finalPrompt = "$userText\n\nHer er filene som nylig er endret: $filesList"
                    } catch (e: Exception) {
                        // Ignorer feil her, send original prompt
                    }
                }

                if (collaborateMode) {
                    // Begge agentene: den valgte svarer, den andre kontrollerer og retter
                    val result = RetrofitClient.api.collaborate(
                        CollaborateRequest(
                            message = finalPrompt,
                            primary = selectedAI.lowercase()
                        )
                    )

                    messages = messages + ChatMessage(
                        result.primary.reply, false, result.primary.agent
                    )

                    val badge = when (result.review.verdict.uppercase()) {
                        "FEIL" -> "⚠️ Fant feil"
                        "AVBRUTT" -> "⏹️ Avbrutt"
                        else -> "✅ Godkjent"
                    }
                    messages = messages + ChatMessage(
                        "$badge\n\n${result.review.reply}",
                        false,
                        "${result.review.agent} (kontroll)"
                    )

                    result.finalAnswer?.let { fix ->
                        messages = messages + ChatMessage(
                            fix.reply, false, "${fix.agent} (rettet)"
                        )
                    }
                } else {
                    // Agenten: AI-en velger verktøy, utfører og svarer med hva den gjorde
                    val result = RetrofitClient.api.agentChat(
                        AgentRequest(
                            message = finalPrompt,
                            provider = selectedAI.lowercase(),
                            conversation_id = conversationId
                        )
                    )
                    conversationId = result.conversation_id
                    if (result.tool_calls.isNotEmpty()) {
                        val toolText = result.tool_calls.joinToString("\n") { "🔧 ${it.tool}" }
                        messages = messages + ChatMessage(toolText, false, "Verktøy")
                    }
                    messages = messages + ChatMessage(result.reply, false, selectedAI)
                }
            } catch (e: Exception) {
                messages = messages + ChatMessage("Feil: Kunne ikke nå $selectedAI (${e.message})", false, "System")
            } finally {
                isTyping = false
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Super-Server AI", fontWeight = FontWeight.Bold) },
                navigationIcon = {
                    IconButton(onClick = onNavigateBack) {
                        Icon(Icons.Default.ArrowBack, contentDescription = "Tilbake")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.primaryContainer
                )
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            // AI Velger
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(8.dp),
                horizontalArrangement = Arrangement.Center
            ) {
                FilterChip(
                    selected = selectedAI == "Gemini" && !collaborateMode,
                    onClick = {
                        selectedAI = "Gemini"
                        collaborateMode = false
                    },
                    label = { Text("Gemini") }
                )
                Spacer(modifier = Modifier.width(8.dp))
                FilterChip(
                    selected = selectedAI == "DeepSeek" && !collaborateMode,
                    onClick = {
                        selectedAI = "DeepSeek"
                        collaborateMode = false
                    },
                    label = { Text("DeepSeek") }
                )
                Spacer(modifier = Modifier.width(8.dp))
                FilterChip(
                    selected = collaborateMode,
                    onClick = { collaborateMode = true },
                    label = { Text("🤝 Begge") }
                )
            }
            
            // Hurtigknapper for Samarbeid
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 8.dp, vertical = 4.dp),
                horizontalArrangement = Arrangement.Center
            ) {
                Button(
                    onClick = {
                        sendMessage("Hva er nytt på serveren? Sjekk nylige filer og gi en kort oppsummering.")
                    },
                    modifier = Modifier.weight(1f)
                ) {
                    Text("Hva er nytt?")
                }
                Spacer(modifier = Modifier.width(8.dp))
                Button(
                    onClick = {
                        collaborateMode = true
                        sendMessage("Den andre AI-en har nettopp gjort endringer i koden vår. Sjekk den sist endrede filen for feil og rett det opp.")
                    },
                    modifier = Modifier.weight(1f)
                ) {
                    Text("Sjekk kode")
                }
            }

            LazyColumn(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp),
                contentPadding = PaddingValues(vertical = 16.dp)
            ) {
                items(messages) { msg ->
                    ChatBubble(msg)
                    Spacer(modifier = Modifier.height(8.dp))
                }
                if (isTyping) {
                    item {
                        val whoTalks = if (collaborateMode) "Gemini og DeepSeek samarbeider" else selectedAI
                        Text("$whoTalks skriver...", color = Color.Gray, modifier = Modifier.padding(8.dp))
                    }
                }
            }

            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(16.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                TextField(
                    value = inputText,
                    onValueChange = { inputText = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("Spør $selectedAI...") },
                    colors = TextFieldDefaults.colors(
                        focusedIndicatorColor = Color.Transparent,
                        unfocusedIndicatorColor = Color.Transparent
                    ),
                    shape = RoundedCornerShape(24.dp)
                )
                Spacer(modifier = Modifier.width(8.dp))
                FloatingActionButton(
                    onClick = { sendMessage(inputText) },
                    containerColor = MaterialTheme.colorScheme.primary
                ) {
                    Icon(Icons.Default.Send, contentDescription = "Send", tint = Color.White)
                }
            }
        }
    }
}

@Composable
fun ChatBubble(message: ChatMessage) {
    val alignment = if (message.isUser) Alignment.CenterEnd else Alignment.CenterStart
    val color = if (message.isUser) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.secondaryContainer
    val textColor = if (message.isUser) Color.White else MaterialTheme.colorScheme.onSecondaryContainer

    Box(modifier = Modifier.fillMaxWidth(), contentAlignment = alignment) {
        Column(horizontalAlignment = if (message.isUser) Alignment.End else Alignment.Start) {
            if (!message.isUser) {
                Text(
                    text = message.senderName,
                    style = MaterialTheme.typography.labelSmall,
                    color = Color.Gray,
                    modifier = Modifier.padding(start = 4.dp, bottom = 2.dp)
                )
            }
            Box(
                modifier = Modifier
                    .background(color, RoundedCornerShape(16.dp))
                    .padding(12.dp)
                    .widthIn(max = 280.dp)
            ) {
                Text(text = message.text, color = textColor)
            }
        }
    }
}
