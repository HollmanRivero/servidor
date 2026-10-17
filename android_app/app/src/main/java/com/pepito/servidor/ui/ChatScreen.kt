package com.pepito.servidor.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
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
import com.pepito.servidor.network.AgentTaskRequest
import com.pepito.servidor.network.ChatRequest
import com.pepito.servidor.network.CollaborateRequest
import com.pepito.servidor.network.RetrofitClient
import kotlinx.coroutines.launch

data class ChatMessage(val text: String, val isUser: Boolean, val senderName: String = "Du")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(onNavigateBack: () -> Unit) {
    var selectedAI by remember { mutableStateOf("Gemini") }
    var collaborateMode by remember { mutableStateOf(false) }
    var agentMode by remember { mutableStateOf(false) }
    var projectName by remember { mutableStateOf("") }
    var messages by remember {
        mutableStateOf(
            listOf(
                ChatMessage("Hei! Hvilken AI vil du prate med i dag?", false, "System")
            )
        )
    }
    var inputText by remember { mutableStateOf("") }
    var isTyping by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun sendMessage(text: String) {
        if (text.isBlank() || isTyping) return
        val userText = text
        messages = messages + ChatMessage(userText, true)
        inputText = ""
        isTyping = true
        scope.launch {
            try {
                var finalPrompt = userText
                if (userText.contains("Hva er nytt") || userText.contains("sist endrede filen")) {
                    try {
                        val changes = RetrofitClient.api.getRecentChanges()
                        val filesList = changes.recent.joinToString(", ") { it.path }
                        finalPrompt = "$userText\n\nHer er filene som nylig er endret: $filesList"
                    } catch (_: Exception) {
                    }
                }

                when {
                    agentMode -> {
                        var currentGoal = finalPrompt
                        var currentProject = projectName.trim()
                        var round = 1
                        val maxRounds = 3
                        while (round <= maxRounds) {
                            val result = RetrofitClient.api.runAgent(
                                AgentTaskRequest(
                                    goal = currentGoal,
                                    project = currentProject,
                                    primary = selectedAI.lowercase(),
                                    review_with_other = true,
                                    max_steps = 30
                                )
                            )
                            if (result.project.isNotBlank()) {
                                currentProject = result.project
                                projectName = result.project
                            }
                            val stepsText = if (result.steps.isEmpty()) {
                                "(ingen steg ble kjørt)"
                            } else {
                                result.steps.joinToString("\n\n") { step ->
                                    val icon = if (step.ok) "✅" else "❌"
                                    val header = "$icon Steg ${step.step} · ${step.action}" +
                                        if (step.target.isNotBlank()) " · ${step.target}" else ""
                                    val thought = if (step.thought.isNotBlank()) "\n💭 ${step.thought}" else ""
                                    val out = if (step.result.isNotBlank()) "\n${step.result.take(900)}" else ""
                                    header + thought + out
                                }
                            }
                            messages = messages + ChatMessage(
                                "📁 Prosjekt: ${result.project} (runde $round)\n\n$stepsText",
                                false,
                                "${result.primary.ifBlank { selectedAI }} (agent)"
                            )
                            val statusLine = if (result.done) "✅ Oppgave fullført" else "⏳ Fortsetter automatisk..."
                            messages = messages + ChatMessage(
                                "$statusLine\n\n${result.summary}",
                                false,
                                result.primary.ifBlank { selectedAI }
                            )
                            result.review?.let { rev ->
                                val badge = when (rev.verdict.uppercase()) {
                                    "FEIL" -> "⚠️ Fant feil"
                                    "AVBRUTT" -> "⏹️ Avbrutt"
                                    else -> "✅ Godkjent"
                                }
                                messages = messages + ChatMessage(
                                    "$badge\n\n${rev.reply}",
                                    false,
                                    "${rev.agent} (kontroll)"
                                )
                            }
                            if (result.done) break
                            round++
                            if (round <= maxRounds) {
                                currentGoal = "Fortsett og fullfør oppgaven helt ferdig i prosjektet '$currentProject'. Opprinnelig mål: $finalPrompt"
                            }
                        }
                    }

                    collaborateMode -> {
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
                    }

                    selectedAI == "Gemini" -> {
                        val response = RetrofitClient.api.chatWithGemini(ChatRequest(finalPrompt))
                        messages = messages + ChatMessage(response.reply, false, "Gemini")
                    }

                    selectedAI == "Tech-AI" -> {
                        val response = RetrofitClient.api.chatWithTechAi(ChatRequest(finalPrompt))
                        messages = messages + ChatMessage(response.reply, false, "Tech-AI")
                    }

                    else -> {
                        val response = RetrofitClient.api.chatWithDeepseek(ChatRequest(finalPrompt))
                        messages = messages + ChatMessage(response.reply, false, "DeepSeek")
                    }
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
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState())
                    .padding(8.dp),
                horizontalArrangement = Arrangement.Center
            ) {
                FilterChip(
                    selected = selectedAI == "Gemini" && !collaborateMode && !agentMode,
                    onClick = {
                        selectedAI = "Gemini"
                        collaborateMode = false
                        agentMode = false
                    },
                    label = { Text("Gemini") }
                )
                Spacer(modifier = Modifier.width(8.dp))
                FilterChip(
                    selected = selectedAI == "DeepSeek" && !collaborateMode && !agentMode,
                    onClick = {
                        selectedAI = "DeepSeek"
                        collaborateMode = false
                        agentMode = false
                    },
                    label = { Text("DeepSeek") }
                )
                Spacer(modifier = Modifier.width(8.dp))
                FilterChip(
                    selected = selectedAI == "Tech-AI" && !collaborateMode && !agentMode,
                    onClick = {
                        selectedAI = "Tech-AI"
                        collaborateMode = false
                        agentMode = false
                    },
                    label = { Text("Tech-AI") }
                )
                Spacer(modifier = Modifier.width(8.dp))
                FilterChip(
                    selected = collaborateMode,
                    onClick = {
                        collaborateMode = true
                        agentMode = false
                    },
                    label = { Text("🤝 Begge") }
                )
                Spacer(modifier = Modifier.width(8.dp))
                FilterChip(
                    selected = agentMode,
                    onClick = {
                        agentMode = true
                        collaborateMode = false
                    },
                    label = { Text("🛠 Agent") }
                )
            }

            if (agentMode) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 12.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    TextField(
                        value = projectName,
                        onValueChange = { projectName = it },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        placeholder = { Text("Prosjektmappe (valgfri, f.eks. min-nettside)") }
                    )
                }
            }

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
                        val whoTalks = when {
                            agentMode -> "${selectedAI}-agenten jobber på serveren"
                            collaborateMode -> "AI-agentene samarbeider"
                            else -> selectedAI
                        }
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
                    placeholder = { Text(if (agentMode) "Beskriv prosjektet agenten skal bygge..." else "Spør $selectedAI...") },
                    colors = TextFieldDefaults.colors(
                        focusedIndicatorColor = Color.Transparent,
                        unfocusedIndicatorColor = Color.Transparent
                    ),
                    shape = RoundedCornerShape(24.dp)
                )
                Spacer(modifier = Modifier.width(8.dp))
                IconButton(
                    onClick = { sendMessage(inputText) },
                    modifier = Modifier
                        .background(MaterialTheme.colorScheme.primary, RoundedCornerShape(50))
                ) {
                    Icon(Icons.Default.Send, contentDescription = "Send", tint = Color.White)
                }
            }
        }
    }
}

@Composable
fun ChatBubble(message: ChatMessage) {
    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (message.isUser) Alignment.End else Alignment.Start
    ) {
        Text(
            text = message.senderName,
            style = MaterialTheme.typography.labelSmall,
            color = Color.Gray,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
        )
        Surface(
            color = if (message.isUser) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.secondaryContainer,
            shape = RoundedCornerShape(
                topStart = 16.dp,
                topEnd = 16.dp,
                bottomStart = if (message.isUser) 16.dp else 0.dp,
                bottomEnd = if (message.isUser) 0.dp else 16.dp
            )
        ) {
            Text(
                text = message.text,
                modifier = Modifier.padding(12.dp),
                color = if (message.isUser) Color.White else MaterialTheme.colorScheme.onSecondaryContainer
            )
        }
    }
}
