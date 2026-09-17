package com.cosmos.app

import android.app.Activity
import android.app.AlertDialog
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.webkit.WebChromeClient
import android.webkit.WebHistoryItem
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Intent
import android.database.Cursor
import android.net.ParseException
import android.os.ParcelFileDescriptor
import android.widget.Toast
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt
import org.json.JSONArray
import org.json.JSONObject

private const val UPDATE_ARTIFACTS_URL = "https://api.github.com/repos/Plrlr/cosmos/actions/artifacts?per_page=5"
private const val UPDATE_BASE_URL = "https://api.github.com/repos/Plrlr/cosmos/actions"
private const val UPDATE_ARTIFACTS_PATH = "artifacts"
private const val UPDATE_ARTIFACT_NAME = "cosmos-debug-apk"
private const val UPDATE_AUTHORITY = "com.cosmos.app.update"
private const val UPDATE_ZIP_NAME = "cosmos-update.zip"
private const val UPDATE_APK_DIR = "update"
private const val UPDATE_APK_PATH = "update/app-debug.apk"
private const val PREF_LAST_CHECK_MS = "last_update_check_ms"
private const val PREF_LAST_SEEN_ARTIFACT = "last_seen_artifact_id"
private const val PREF_SKIPPED_ARTIFACT = "skipped_artifact_id"
private const val UPDATE_CHECK_INTERVAL_MS = 30L * 60L * 1000L
private const val CHAT_CODE_RATE_LIMIT = 1302
private const val CHAT_CODE_OVERLOAD = 1305
private const val CHAT_CODE_USAGE = 1308


/**
 * Single-activity app: show a loading screen, run the news_hub pipeline in
 * Python on a background thread, then display the generated HTML report in a
 * WebView -- or swap the loading screen's text for a readable error.
 *
 * The report view is read-only chrome around the brief, plus one way to get
 * more: tapping an article link opens it in a full-screen in-app browser
 * (never an external app or browser). Older issues are reached through the
 * report's own "Back issues" footer link, which loads history.html -- a small
 * index the app regenerates after every run -- in the same WebView.
 *
 * A same-day report already on disk is reused without re-running Python, so
 * reopening the app never re-fetches the day's feeds.
 *
 * The history page also offers a "Search" link: a full-screen, standalone
 * chat with a GLM model that can search the web. It never touches the
 * articles; its API key and model name live in SharedPreferences and are
 * edited through the chat toolbar's "Key" button.
 *
 * Framework widgets only (no AndroidX): the theme, WebView and views all come
 * from android.*, so the build has zero runtime dependencies beyond Chaquopy
 * and the Kotlin stdlib.
 */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private lateinit var reportRoot: View
    private lateinit var loadingPanel: View
    private lateinit var progressBar: ProgressBar
    private lateinit var statusTitle: TextView
    private lateinit var statusMessage: TextView

    private lateinit var articleOverlay: View
    private lateinit var articleWebView: WebView
    private lateinit var articleUrlView: TextView
    private lateinit var articleProgress: ProgressBar

    private lateinit var chatOverlay: View
    private lateinit var chatScroll: ScrollView
    private lateinit var chatList: LinearLayout
    private lateinit var chatInput: EditText
    private lateinit var chatSend: TextView
    private lateinit var updateBanner: View
    private lateinit var updateBannerText: TextView
    private lateinit var updateSkip: TextView

    /** The session's conversation turns, in order: "user" and "assistant". */
    private val chatHistory = ArrayList<ChatTurn>()

    /** True while a chat request is in flight; guards the Send button. */
    private var chatRequestInFlight = false

    /** The transient "Thinkingâ€¦" bubble, removed when the reply lands. */
    private var chatThinkingView: TextView? = null

    /**
     * True from the moment a fresh article session starts until its first page
     * finishes loading. Used to wipe the stale back/forward list (old articles
     * and leftover "about:blank" entries) exactly once per session, so
     * in-article navigation history is preserved afterwards.
     */
    private var freshLoad = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        reportRoot = findViewById(R.id.reportRoot)
        loadingPanel = findViewById(R.id.loadingPanel)
        progressBar = findViewById(R.id.progressBar)
        statusTitle = findViewById(R.id.statusTitle)
        statusMessage = findViewById(R.id.statusMessage)

        articleOverlay = findViewById(R.id.articleOverlay)
        articleWebView = findViewById(R.id.articleWebView)
        articleUrlView = findViewById(R.id.articleUrl)
        articleProgress = findViewById(R.id.articleProgress)

        configureWebView()
        configureArticleWebView()
        findViewById<View>(R.id.articleClose).setOnClickListener { hideArticle() }

        chatOverlay = findViewById(R.id.chatOverlay)
        chatScroll = findViewById(R.id.chatScroll)
        chatList = findViewById(R.id.chatList)
        chatInput = findViewById(R.id.chatInput)
        chatSend = findViewById(R.id.chatSend)
        updateBanner = findViewById(R.id.updateBanner)
        updateBannerText = findViewById(R.id.updateBannerText)
        updateSkip = findViewById(R.id.updateSkip)
        findViewById<View>(R.id.chatClose).setOnClickListener { closeChat() }
        findViewById<View>(R.id.chatKey).setOnClickListener { showKeyDialog(showHint = false) }
        updateBannerText.setOnClickListener { startUpdateDownload() }
        updateSkip.setOnClickListener {
            val skippedId = updateArtifactId?.toString() ?: ""
            getSharedPreferences(CHAT_PREFS_NAME, MODE_PRIVATE).edit()
                .putString(PREF_SKIPPED_ARTIFACT, skippedId).apply()
            updateBanner.visibility = View.GONE
        }
        chatSend.setOnClickListener { sendChatMessage() }

        // Resize the window for the soft keyboard so the chat input row and
        // the newest bubbles stay visible while typing.
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)

        // Starting Python and running the pipeline both block, so do it off
        // the UI thread; the completion path hops back via runOnUiThread().
        Thread(::runPipeline, "cosmos-pipeline").start()
        Thread({ checkForUpdate() }, "cosmos-update-check").start()
    }

    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = true   // only for the report's own inline map pan/zoom script
            allowFileAccess = true    // the report is loaded via file://
            allowContentAccess = true
            blockNetworkLoads = true  // report is self-contained (inline CSS/SVG)
            useWideViewPort = true    // comfortable reading on tablets
            loadWithOverviewMode = true
            builtInZoomControls = true
            displayZoomControls = false
            setSupportZoom(true)
        }
        // Article links must never leave the app: http(s) opens in the overlay
        // below, while file:// (the report itself and history.html) loads in
        // this view as usual. Returning false here is what keeps the report's
        // "Back issues" link in-view instead of bouncing it to the overlay.
        // The history page's cosmos://search link opens the chat instead; any
        // other cosmos:// link is swallowed so the WebView never errors on it.
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val scheme = request.url.scheme?.lowercase(Locale.US)
                if (scheme == "cosmos") {
                    if (request.url.host?.equals("search", ignoreCase = true) == true) {
                        openChat()
                    }
                    return true
                }
                if (scheme == "http" || scheme == "https") {
                    showArticle(request.url.toString())
                    return true
                }
                return false
            }
        }
    }

    private fun configureArticleWebView() {
        articleWebView.settings.apply {
            javaScriptEnabled = true   // publisher pages expect scripts
            domStorageEnabled = true
            blockNetworkLoads = false  // this is the one view allowed on the wire
            allowFileAccess = false
            useWideViewPort = true
            loadWithOverviewMode = true
            builtInZoomControls = true
            displayZoomControls = false
            setSupportZoom(true)
        }
        // Its own client keeps navigation inside the overlay: http(s) loads
        // here, and anything exotic (mailto:, intent:, market:) is dropped
        // rather than handed to another app.
        articleWebView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val scheme = request.url.scheme?.lowercase(Locale.US)
                return scheme != "http" && scheme != "https"
            }

            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                // The document itself is never blocked, only its subresources,
                // so ads/trackers vanish while the article still renders.
                if (request.isForMainFrame) return null
                val host = request.url.host
                return if (isBlockedHost(host)) {
                    WebResourceResponse("text/plain", "utf-8", ByteArrayInputStream(ByteArray(0)))
                } else {
                    null
                }
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                updateArticleUrl(url)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                // First completed page of a session: drop the stale
                // back/forward list once, then leave in-article history alone.
                if (freshLoad) {
                    freshLoad = false
                    articleWebView.clearHistory()
                }
            }

            override fun doUpdateVisitedHistory(view: WebView?, url: String?, isReload: Boolean) {
                updateArticleUrl(url)
            }
        }
        articleWebView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                articleProgress.progress = newProgress
                articleProgress.visibility = if (newProgress >= 100) View.GONE else View.VISIBLE
            }
        }
    }

    /** Show the article overlay and load one http(s) link into it. */
    private fun showArticle(url: String) {
        freshLoad = true
        articleUrlView.text = Uri.parse(url).host ?: url
        articleProgress.progress = 0
        articleProgress.visibility = View.VISIBLE
        articleOverlay.visibility = View.VISIBLE
        articleWebView.loadUrl(url)
    }

    /** Dismiss the overlay, stop the current load, and blank the URL readout. */
    private fun hideArticle() {
        articleWebView.stopLoading()
        articleUrlView.text = ""
        articleProgress.progress = 0
        articleProgress.visibility = View.GONE
        articleOverlay.visibility = View.GONE
    }

    /** Reflect a navigation in the toolbar, but never show "about:blank". */
    private fun updateArticleUrl(url: String?) {
        if (url.isNullOrEmpty() || url == "about:blank") {
            articleUrlView.text = ""
        } else {
            articleUrlView.text = Uri.parse(url).host ?: url
        }
    }

    // ----------------------------------------------------------------------
    // GLM chat ("Search") overlay
    // ----------------------------------------------------------------------

    /** Show the chat overlay, with a welcome bubble on a fresh session. */
    private fun openChat() {
        if (chatOverlay.visibility == View.VISIBLE) return
        chatOverlay.visibility = View.VISIBLE
        if (chatHistory.isEmpty() && chatList.childCount == 0) {
            addAssistantBubble(getString(R.string.chat_welcome), sources = null)
        }
        // First visit without a key: walk the user straight to the dialog.
        if (getApiKey().isEmpty()) {
            showKeyDialog(showHint = true)
        }
    }

    /** Hide the chat overlay; the conversation stays alive for the session. */
    private fun closeChat() {
        chatInput.clearFocus()
        chatOverlay.visibility = View.GONE
    }

    /** Send button: push the user message into the list and start the request. */
    private fun sendChatMessage() {
        if (chatRequestInFlight) return
        val text = chatInput.text.toString().trim()
        if (text.isEmpty()) return
        val key = getApiKey()
        if (key.isEmpty()) {
            showKeyDialog(showHint = true)
            return
        }
        val model = getModel()

        chatInput.setText("")
        chatHistory.add(ChatTurn("user", text))
        addUserBubble(text)

        // Snapshot the payload here on the UI thread (fixed system prompt +
        // the last CHAT_HISTORY_LIMIT turns, which includes the new user
        // message), so the background thread never races later list edits.
        val messages = JSONArray().put(
            JSONObject().put("role", "system").put("content", CHAT_SYSTEM_PROMPT)
        )
        for (turn in chatHistory.takeLast(CHAT_HISTORY_LIMIT)) {
            messages.put(JSONObject().put("role", turn.role).put("content", turn.content))
        }

        chatRequestInFlight = true
        chatSend.isEnabled = false
        chatSend.alpha = 0.4f
        chatThinkingView = addThinkingBubble()

        Thread({ runChatRequest(key, model, messages) }, "cosmos-chat").start()
    }

    /**
     * Background path for one completion request. Tries with the web_search
     * tool first; on an HTTP 400 that blames the tools, retries exactly once
     * without them and flags the outcome so the UI can add a note bubble.
     */
    private fun runChatRequest(key: String, model: String, messages: JSONArray) {
        var outcome = try {
            val first = postChatCompletion(key, model, messages, withTools = true)
            when {
                first.httpCode in 200..299 -> parseChatReply(first.body)
                first.httpCode == 400 && toolsBlamedIn(first.body) -> {
                    val retry = postChatCompletion(key, model, messages, withTools = false)
                    if (retry.httpCode in 200..299) {
                        parseChatReply(retry.body).copy(fallbackUsed = true)
                    } else {
                        ChatOutcome(null, null, retry.httpCode, apiErrorMessage(retry.body), true)
                    }
                }
                else -> ChatOutcome(null, null, first.httpCode, apiErrorMessage(first.body), false)
            }
        } catch (e: Exception) {
            // Transport failures: DNS, connect/read timeout, refused, etc.
            ChatOutcome(null, null, CHAT_ERROR_NETWORK, e.message ?: e.javaClass.simpleName, false)
        }
        // Retry with backoff: Z.ai signals rate limits / overload via HTTP 429 or
        // business codes 1302/1305/1308. Three attempts total (2s, then 5s).
        var attempts = 1
        while (attempts < 3 && outcome.reply == null && outcome.errorCode != null &&
            (outcome.errorCode == 429 || outcome.errorCode == CHAT_CODE_RATE_LIMIT ||
                outcome.errorCode == CHAT_CODE_OVERLOAD || outcome.errorCode == CHAT_CODE_USAGE ||
                outcome.errorCode in 500..599)) {
            val delayMs = if (attempts == 1) 2000L else 5000L
            try { Thread.sleep(delayMs) } catch (e: InterruptedException) { break }
            attempts++
            outcome = try {
                val again = postChatCompletion(key, model, messages, withTools = true)
                if (again.httpCode in 200..299) parseChatReply(again.body)
                else ChatOutcome(null, null, again.httpCode, apiErrorMessage(again.body), false)
            } catch (e: Exception) {
                ChatOutcome(null, null, CHAT_ERROR_NETWORK, e.message ?: e.javaClass.simpleName, false)
            }
        }
        runOnUiThread { onChatOutcome(outcome) }
    }

    /** UI-thread completion: swap the "Thinkingâ€¦" bubble for the reply/error. */
    private fun onChatOutcome(outcome: ChatOutcome) {
        chatRequestInFlight = false
        chatSend.isEnabled = true
        chatSend.alpha = 1f
        removeThinkingBubble()
        if (outcome.reply != null) {
            chatHistory.add(ChatTurn("assistant", outcome.reply))
            if (outcome.fallbackUsed) {
                addNoteBubble(getString(R.string.chat_fallback_note))
            }
            addAssistantBubble(outcome.reply, outcome.sources)
            return
        }
        val detail = outcome.errorMessage ?: ""
        val message = when (outcome.errorCode) {
            CHAT_ERROR_NETWORK -> getString(R.string.chat_network_error, detail)
            CHAT_ERROR_BAD_REPLY -> getString(R.string.chat_empty_reply)
            // Missing/revoked credentials: point at the Key dialog.
            401, 403 -> getString(R.string.chat_api_error, outcome.errorCode, detail) +
                "\n\n" + getString(R.string.chat_key_error_hint)
            429, CHAT_CODE_RATE_LIMIT, CHAT_CODE_OVERLOAD, CHAT_CODE_USAGE ->
                getString(R.string.chat_rate_limited)
            else -> getString(R.string.chat_api_error, outcome.errorCode, detail)
        }
        addErrorBubble(message)
    }

    /**
     * One synchronous POST to the chat API. Called only from the chat
     * background thread; returns the raw status code and response body.
     */
    private fun postChatCompletion(
        key: String,
        model: String,
        messages: JSONArray,
        withTools: Boolean,
    ): ChatHttpResponse {
        val payload = JSONObject()
            .put("model", model)
            .put("messages", messages)
        if (withTools) {
            payload.put(
                "tools",
                JSONArray().put(
                    JSONObject()
                        .put("type", "web_search")
                        .put(
                            "web_search",
                            JSONObject()
                                .put("enable", true)
                                .put("search", true)
                                .put("search_result", 8)
                        )
                )
            )
        }
        payload.put("stream", false)
        payload.put("temperature", 0.6)
        payload.put("max_tokens", 2048)
        // glm-4.7-flash supports explicit thinking control; disable it for
        // fast conversational replies (verified against docs.z.ai schema).
        payload.put("thinking", JSONObject().put("type", "disabled"))

        val connection = URL(CHAT_API_URL).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "POST"
            connection.connectTimeout = 15_000
            connection.readTimeout = 120_000
            connection.doOutput = true
            connection.setRequestProperty("Authorization", "Bearer $key")
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.use { out ->
                out.write(payload.toString().toByteArray(StandardCharsets.UTF_8))
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val body = stream?.use { it.readBytes().toString(StandardCharsets.UTF_8) } ?: ""
            return ChatHttpResponse(code, body)
        } finally {
            connection.disconnect()
        }
    }

    /** Extract the reply text and optional source names from a 200 body. */
    private fun parseChatReply(body: String): ChatOutcome {
        return try {
            val message = JSONObject(body)
                .optJSONArray("choices")
                ?.optJSONObject(0)
                ?.optJSONObject("message")
                ?: return ChatOutcome(null, null, CHAT_ERROR_BAD_REPLY, null, false)
            val content = message.optString("content").trim()
            if (content.isEmpty()) {
                ChatOutcome(null, null, CHAT_ERROR_BAD_REPLY, null, false)
            } else {
                ChatOutcome(content, extractSourceNames(message), 200, null, false)
            }
        } catch (e: Exception) {
            ChatOutcome(null, null, CHAT_ERROR_BAD_REPLY, null, false)
        }
    }

    /**
     * Best-effort "Sources:" names from the message's web_search array. The
     * item shape is not documented, so try the plausible site-name fields and
     * fall back to the link host; give up quietly when nothing parses.
     */
    private fun extractSourceNames(message: JSONObject): String? {
        val items = message.optJSONArray("web_search") ?: return null
        val names = LinkedHashSet<String>()
        for (i in 0 until items.length()) {
            val item = items.optJSONObject(i) ?: continue
            val name = listOf(
                item.optString("site_name"),
                item.optString("media"),
                urlHost(item.optString("link")),
                urlHost(item.optString("url")),
            ).firstOrNull { it.isNotBlank() } ?: continue
            names.add(name)
            if (names.size == CHAT_SOURCE_LIMIT) break
        }
        return if (names.isEmpty()) null else names.joinToString(", ")
    }

    private fun urlHost(url: String): String {
        if (url.isBlank()) return ""
        return try {
            Uri.parse(url).host ?: ""
        } catch (e: Exception) {
            ""
        }
    }

    /** True when an HTTP 400 body points at the tools/web_search field. */
    private fun toolsBlamedIn(body: String): Boolean {
        val lower = body.lowercase(Locale.US)
        return "tool" in lower || "web_search" in lower
    }

    /** Pull the API's error message out of a non-200 body, defensively. */
    private fun apiErrorMessage(body: String): String {
        val fallback = body.take(300)
        return try {
            JSONObject(body).optJSONObject("error")?.optString("message")
                ?.takeIf { it.isNotBlank() } ?: fallback
        } catch (e: Exception) {
            fallback
        }
    }

    /** Append one bubble TextView to [chatList] and scroll it into view. */
    private fun addChatBubble(text: CharSequence, fromUser: Boolean): TextView {
        val bubble = TextView(this)
        bubble.text = text
        bubble.textSize = 15f
        bubble.setTextColor(BUBBLE_TEXT_COLOR)
        bubble.setPadding(dp(14), dp(10), dp(14), dp(10))
        // Never wider than ~3/4 of the screen, whatever the message length.
        bubble.maxWidth = (resources.displayMetrics.widthPixels * 0.78f).toInt()

        val background = GradientDrawable()
        background.setColor(if (fromUser) BUBBLE_USER_BG else BUBBLE_ASSISTANT_BG)
        background.cornerRadius = dp(16).toFloat()
        bubble.background = background

        val params = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT
        )
        params.gravity = if (fromUser) Gravity.END else Gravity.START
        params.topMargin = dp(8)
        chatList.addView(bubble, params)
        scrollChatToBottom()
        return bubble
    }

    private fun addUserBubble(text: String) {
        addChatBubble(text, fromUser = true)
    }

    /** Assistant reply; the optional sources line is dimmed inside the bubble. */
    private fun addAssistantBubble(reply: String, sources: String?) {
        var text: CharSequence = reply
        if (sources != null) {
            val separator = "\n\n"
            val full = reply + separator + getString(R.string.chat_sources_prefix, sources)
            val spannable = SpannableString(full)
            spannable.setSpan(
                ForegroundColorSpan(BUBBLE_MUTED_COLOR),
                reply.length + separator.length,
                full.length,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
            )
            text = spannable
        }
        addChatBubble(text, fromUser = false).setTextIsSelectable(true)
    }

    /** Small muted system note (e.g. the no-web-search fallback notice). */
    private fun addNoteBubble(text: String) {
        val bubble = addChatBubble(text, fromUser = false)
        bubble.textSize = 12f
        bubble.setTextColor(BUBBLE_MUTED_COLOR)
    }

    /** Transient "Thinkingâ€¦" placeholder; kept so it can be removed later. */
    private fun addThinkingBubble(): TextView {
        val bubble = addChatBubble(getString(R.string.chat_thinking), fromUser = false)
        bubble.setTextColor(BUBBLE_MUTED_COLOR)
        return bubble
    }

    private fun removeThinkingBubble() {
        chatThinkingView?.let { chatList.removeView(it) }
        chatThinkingView = null
    }

    /** Red-tinted assistant bubble for API/network failures. */
    private fun addErrorBubble(text: String) {
        val bubble = addChatBubble(text, fromUser = false)
        val background = GradientDrawable()
        background.setColor(BUBBLE_ERROR_BG)
        background.cornerRadius = dp(16).toFloat()
        bubble.background = background
        bubble.setTextColor(BUBBLE_ERROR_TEXT)
    }

    private fun scrollChatToBottom() {
        chatScroll.post { chatScroll.fullScroll(View.FOCUS_DOWN) }
    }

    /**
     * Dialog for the Z.ai API key and model, built in code (no extra layout
     * resource). Values live in SharedPreferences and are never logged.
     */
    private fun showKeyDialog(showHint: Boolean) {
        val prefs = getSharedPreferences(CHAT_PREFS_NAME, MODE_PRIVATE)
        val keyInput = EditText(this).apply {
            hint = getString(R.string.chat_key_field_hint)
            setText(prefs.getString(PREF_API_KEY, ""))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
            setSingleLine(true)
        }
        val modelInput = EditText(this).apply {
            hint = getString(R.string.chat_model_field_hint)
            setText(prefs.getString(PREF_MODEL, DEFAULT_MODEL))
            inputType = InputType.TYPE_CLASS_TEXT
            setSingleLine(true)
        }
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), 0)
        }
        if (showHint) {
            container.addView(TextView(this).apply {
                text = getString(R.string.chat_set_key_hint)
                textSize = 13f
                setTextColor(BUBBLE_MUTED_COLOR)
            })
        }
        container.addView(
            keyInput,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(10) }
        )
        container.addView(
            modelInput,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(10) }
        )

        AlertDialog.Builder(this)
            .setTitle(R.string.chat_key_dialog_title)
            .setView(container)
            .setPositiveButton(R.string.chat_save) { _, _ ->
                prefs.edit()
                    .putString(PREF_API_KEY, keyInput.text.toString().trim())
                    .putString(PREF_MODEL, modelInput.text.toString().trim().ifEmpty { DEFAULT_MODEL })
                    .apply()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun getApiKey(): String =
        getSharedPreferences(CHAT_PREFS_NAME, MODE_PRIVATE).getString(PREF_API_KEY, "").orEmpty()

    private fun getModel(): String =
        getSharedPreferences(CHAT_PREFS_NAME, MODE_PRIVATE)
            .getString(PREF_MODEL, DEFAULT_MODEL)?.takeIf { it.isNotBlank() } ?: DEFAULT_MODEL

    /** Density-independent pixel helper for the programmatic chat chrome. */
    private fun dp(value: Int): Int =
        TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP,
            value.toFloat(),
            resources.displayMetrics
        ).roundToInt()

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        // Back closes the article first, then the chat overlay (the
        // conversation stays in memory for the session), then a report page
        // at a time, before it is allowed to leave the report.
        if (articleOverlay.visibility == View.VISIBLE) {
            goArticleBack()
            return
        }
        if (chatOverlay.visibility == View.VISIBLE) {
            closeChat()
            return
        }
        // Then walk the report itself: past issue -> history page -> today's
        // report -> (nothing left) exit.
        if (webView.canGoBack()) {
            webView.goBack()
            return
        }
        super.onBackPressed()
    }

    /**
     * Step back one real page inside the overlay, skipping any "about:blank"
     * entries, or dismiss the overlay when there is nowhere left to go.
     */
    private fun goArticleBack() {
        val history = articleWebView.copyBackForwardList()
        var target = history.currentIndex - 1
        while (target >= 0 && isBlankEntry(history.getItemAtIndex(target))) {
            target--
        }
        if (target >= 0) {
            articleWebView.goBackOrForward(target - history.currentIndex)
        } else {
            hideArticle()
        }
    }

    private fun isBlankEntry(item: WebHistoryItem): Boolean =
        item.url == "about:blank" || item.originalUrl == "about:blank"

    private fun runPipeline() {
        // App-private storage: writable without any permission, and readable
        // back through file:// for the WebView. The CLI's --output default is
        // a relative "reports/" dir, so always pass the absolute path.
        val outputDir = File(filesDir, "reports")
        val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
        val todayReport = File(outputDir, "$today.html")

        // Same-day caching: if today's brief is already on disk, skip Python
        // and show it again. Fresh install (no file) falls through to a full run.
        if (todayReport.isFile && todayReport.length() > 0L) {
            writeHistoryIndex(outputDir)
            runOnUiThread { displayNewestReport(outputDir) }
            return
        }

        val result = try {
            // Chaquopy must be told it is running on Android before any other
            // Python API is touched; without this it falls back to
            // GenericPlatform and throws at Python.getInstance().
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(this))
            }
            val runner = Python.getInstance().getModule("runner")
            runner.callAttr("run", outputDir.absolutePath, 12, 8).toString()
        } catch (t: Throwable) {
            // Covers PyException (should not happen: runner.py catches
            // everything) as well as native/startup failures (UnsatisfiedLinkError
            // and friends, which extend Error and would otherwise kill the thread).
            "error:${t.javaClass.simpleName}:${t.message}"
        }

        // Regenerate the back-issues index after every successful run, before
        // the report (and its "Back issues" link) is shown.
        if (result.startsWith("ok")) {
            writeHistoryIndex(outputDir)
        }
        runOnUiThread { onPipelineFinished(result, outputDir) }
    }

    private fun onPipelineFinished(result: String, outputDir: File) {
        if (result.startsWith("ok")) {
            displayNewestReport(outputDir)
        } else {
            showError(getString(R.string.error_title), result)
        }
    }

    /**
     * Load the newest *.html report into the report view. Reports are named
     * YYYY-MM-DD.html; newest by modification time also handles a leftover
     * report from an earlier run today.
     */
    private fun displayNewestReport(outputDir: File) {
        val newest = outputDir.listFiles { f -> f.isFile && f.name.endsWith(".html") }
            ?.maxByOrNull { it.lastModified() }
        if (newest != null) {
            loadingPanel.visibility = View.GONE
            reportRoot.visibility = View.VISIBLE
            webView.loadUrl("file://" + newest.absolutePath)
            return
        }
        showError(
            getString(R.string.error_title),
            getString(R.string.error_no_report) + "\n\n" + outputDir.absolutePath
        )
    }

    /** Error path: keep the panel visible, drop the spinner, explain what failed. */
    private fun showError(title: String, message: String) {
        progressBar.visibility = View.GONE
        statusTitle.text = title
        statusMessage.text = message
    }

    /**
     * Write history.html into the reports dir: one link per saved issue, newest
     * first. The report WebView loads file:// in-view, so both the report's
     * "Back issues" footer link and these date links navigate without leaving
     * the WebView.
     */
    private fun writeHistoryIndex(outputDir: File) {
        val todayName = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date()) + ".html"
        val issues = outputDir.listFiles { f ->
            f.isFile && f.name.endsWith(".html") && f.name != HISTORY_FILE && ISSUE_NAME.matches(f.name)
        }?.sortedWith(
            compareByDescending<File> { issueSortKey(it) }.thenByDescending { it.lastModified() }
        ).orEmpty()

        val humanDate = SimpleDateFormat("EEEE, MMMM d, yyyy", Locale.getDefault())
        val entries = StringBuilder()
        for (file in issues) {
            val match = ISSUE_NAME.matchEntire(file.name) ?: continue
            val (year, month, day) = match.destructured
            val date = Calendar.getInstance().apply {
                clear()
                set(year.toInt(), month.toInt() - 1, day.toInt())
            }
            val label = humanDate.format(date.time)
            val prefix = if (file.name == todayName) "Today Â· " else ""
            entries.append("""<li><a href="${file.name}">$prefix$label</a></li>""")
        }

        val html = """
            <!doctype html><html><head><meta charset="utf-8"><title>Cosmos</title><style>
            body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#fff;max-width:900px;margin:40px auto;color:#172033;line-height:1.6;padding:0 16px}
            h1{font-size:18px;margin:0 0 12px}.muted{color:#667085;font-size:12px}
            ul{list-style:none;padding:0;margin:0}li{margin:0 0 16px;font-size:15px}
            a{color:#174ea6;text-decoration:none}
            .head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 16px}
            .head h1{margin:0}
            .search a{display:inline-block;padding:7px 14px;border:1px solid #c6d4ee;border-radius:10px;background:#f4f7fd;font-size:14px;font-weight:600}
            </style></head><body>
            <div class="head"><h1>Cosmos</h1><p class="search"><a href="cosmos://search">${getString(R.string.search_button)}</a></p></div>
            ${if (entries.isEmpty()) """<p class="muted">No saved issues yet.</p>""" else ""}
            <ul>$entries</ul>
            </body></html>
        """.trimIndent()
        File(outputDir, HISTORY_FILE).writeText(html)
    }

    /** Sort key from a YYYY-MM-DD.html name; non-matching names sink to the end. */
    private fun issueSortKey(file: File): Long {
        val match = ISSUE_NAME.matchEntire(file.name) ?: return -1L
        val (year, month, day) = match.destructured
        return year.toLong() * 10000 + month.toLong() * 100 + day.toLong()
    }

    /** One conversation turn ("user" or "assistant") kept for the session. */
    private data class ChatTurn(val role: String, val content: String)

    /**
     * Outcome of one chat request, ready for the UI thread: either a reply
     * (with optional source names) or an error described by an HTTP status
     * code, CHAT_ERROR_NETWORK (-1), or CHAT_ERROR_BAD_REPLY (0).
     */
    private data class ChatOutcome(
        val reply: String?,
        val sources: String?,
        val errorCode: Int,
        val errorMessage: String?,
        val fallbackUsed: Boolean,
    )

    /** Raw status line of one HTTP exchange with the chat API. */
    private class ChatHttpResponse(val httpCode: Int, val body: String)


    // ------------------------------------------------------------------
    // Auto-update: check GitHub Actions artifacts (public repo, token-free),
    // banner when a new build exists, download + offer install.
    // ------------------------------------------------------------------

    private fun checkForUpdate() {
        val prefs = getSharedPreferences(CHAT_PREFS_NAME, MODE_PRIVATE)
        val now = System.currentTimeMillis()
        val last = prefs.getLong(PREF_LAST_CHECK_MS, 0L)
        if (now - last < UPDATE_CHECK_INTERVAL_MS) return
        prefs.edit().putLong(PREF_LAST_CHECK_MS, now).apply()
        try {
            val conn = urlConnection(UPDATE_ARTIFACTS_URL)
            val body = conn.inputStream.use { it.readBytes() }.toString(Charsets.UTF_8)
            conn.disconnect()
            // GitHub wraps the list: {"total_count": N, "artifacts": [...]}
            val arr = JSONObject(body).getJSONArray("artifacts")
            var newest: org.json.JSONObject? = null
            for (i in 0 until arr.length()) {
                val a = arr.getJSONObject(i)
                if (a.optString("name") == UPDATE_ARTIFACT_NAME && !a.optBoolean("expired", false)) {
                    val cur = newest
                    if (cur == null || a.optLong("id", 0) > cur.optLong("id", 0)) newest = a
                }
            }
            val artifact = newest ?: return
            val id = artifact.optLong("id", 0)
            if (id == 0L || id.toString() == prefs.getString(PREF_SKIPPED_ARTIFACT, null)) return
            val label = artifact.updatedAtShort() // "build <date>"
            if (id.toString() == prefs.getString(PREF_LAST_SEEN_ARTIFACT, null)) return // already offered
            prefs.edit().putString(PREF_LAST_SEEN_ARTIFACT, id.toString()).apply()
            runOnUiThread {
                updateArtifactId = id
                updateBannerText.text = getString(R.string.update_banner, label)
                updateBanner.visibility = View.VISIBLE
            }
        } catch (t: Throwable) {
            // Silent: no network, rate limit, parse hiccup â€” never nag the user.
        }
    }

    private fun org.json.JSONObject.updatedAtShort(): String {
        // "2026-09-17T00:13:27Z" -> "build Sep 17"
        val raw = optString("updated_at")
        val datePart = raw.takeIf { it.length >= 10 }?.substring(0, 10) ?: return "latest build"
        return try {
            val inFmt = SimpleDateFormat("yyyy-MM-dd", Locale.US)
            val outFmt = SimpleDateFormat("MMM d", Locale.US)
            "build " + outFmt.format(inFmt.parse(datePart)!!)
        } catch (e: ParseException) {
            "latest build"
        }
    }

    private fun startUpdateDownload() {
        val id = updateArtifactId ?: return
        updateBannerText.text = getString(R.string.update_downloading, 0)
        Thread({
            try {
                val zip = File(cacheDir, UPDATE_ZIP_NAME)
                val conn = urlConnection("$UPDATE_BASE_URL$UPDATE_ARTIFACTS_PATH/$id/zip")
                val total = conn.contentLengthLong
                conn.inputStream.use { input ->
                    zip.outputStream().use { out ->
                        val buf = ByteArray(64 * 1024)
                        var read = 0L
                        var lastPct = -1
                        while (true) {
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            read += n
                            if (total > 0) {
                                val pct = (read * 100 / total).toInt()
                                if (pct != lastPct) {
                                    lastPct = pct
                                    runOnUiThread { updateBannerText.text = getString(R.string.update_downloading, pct) }
                                }
                            }
                        }
                    }
                }
                val apk = File(filesDir, UPDATE_APK_PATH)
                apk.parentFile?.mkdirs()
                extractApk(zip, apk)
                zip.delete()
                runOnUiThread { launchInstaller(apk) }
            } catch (t: Throwable) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.update_install_failed), Toast.LENGTH_SHORT).show()
                    updateBannerText.text = getString(R.string.update_banner, "latest build")
                }
            }
        }, "cosmos-update").start()
    }

    private fun extractApk(zip: File, target: File) {
        ZipInputStream(zip.inputStream()).use { zin ->
            while (true) {
                val entry: ZipEntry = zin.nextEntry ?: break
                if (entry.name.endsWith(".apk")) {
                    FileOutputStream(target).use { out -> zin.copyTo(out) }
                    return
                }
                zin.closeEntry()
            }
        }
        throw IllegalStateException("No APK inside the artifact")
    }

    private fun launchInstaller(apk: File) {
        try {
            val uri = Uri.parse("content://${UPDATE_AUTHORITY}/update/${apk.name}")
            val intent = Intent(Intent.ACTION_INSTALL_PACKAGE).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(intent)
        } catch (t: Throwable) {
            Toast.makeText(this, getString(R.string.update_install_failed), Toast.LENGTH_SHORT).show()
        }
    }

    private fun urlConnection(url: String): HttpURLConnection {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15000
        conn.readTimeout = 120000
        conn.setRequestProperty("User-Agent", "cosmos-app")
        return conn
    }


    private companion object {
        private var updateArtifactId: Long? = null
        // --- GLM chat ("Search") constants ---

        /** Z.ai OpenAI-compatible chat completions endpoint. */
        private const val CHAT_API_URL = "https://api.z.ai/api/paas/v4/chat/completions"

        /** Sent as the fixed system message ahead of the capped history. */
        private const val CHAT_SYSTEM_PROMPT =
            "You are Cosmos, the assistant inside a personal news reader. Be " +
                "concise and factual. ALWAYS use web search for any question about " +
                "current events, news, prices, weather, sports, schedules, or anything " +
                "time-sensitive - even if you believe you already know the answer. " +
                "Never answer such questions from memory alone; your built-in knowledge " +
                "has a cutoff and is outdated. If web search is unavailable or returns " +
                "nothing, say so explicitly instead of guessing."

        /** Where the chat's API key and model name are stored. */
        private const val CHAT_PREFS_NAME = "cosmos_settings"
        private const val PREF_API_KEY = "api_key"
        private const val PREF_MODEL = "model"

        /** A genuinely free Z.ai model; glm-5.2 exists but is paid. */
        private const val DEFAULT_MODEL = "glm-4.7-flash"

        /** Turn cap for the request's message history, to bound tokens. */
        private const val CHAT_HISTORY_LIMIT = 20

        /** Max source names kept for the reply's "Sources:" line. */
        private const val CHAT_SOURCE_LIMIT = 5

        /** Error codes beyond HTTP: transport failure or a bad 200 payload. */
        private const val CHAT_ERROR_NETWORK = -1
        private const val CHAT_ERROR_BAD_REPLY = 0

        /** Bubble palette; inline hex because no color resources may be added. */
        private val BUBBLE_USER_BG = Color.parseColor("#E8F0FE")
        private val BUBBLE_ASSISTANT_BG = Color.parseColor("#F1F3F4")
        private val BUBBLE_TEXT_COLOR = Color.parseColor("#1F1F1F")
        private val BUBBLE_MUTED_COLOR = Color.parseColor("#5F6368")
        private val BUBBLE_ERROR_BG = Color.parseColor("#FCE8E6")
        private val BUBBLE_ERROR_TEXT = Color.parseColor("#B3261E")

        /** Matches the pipeline's report filenames, e.g. 2026-09-15.html. */
        val ISSUE_NAME = Regex("""^(\d{4})-(\d{2})-(\d{2})\.html$""")

        /** The back-issues index lives alongside the reports it lists. */
        const val HISTORY_FILE = "history.html"

        /**
         * High-confidence ad/tracker/analytic hosts blocked in the article
         * overlay. Host-suffix matchable, so every subdomain is covered too.
         * Immutable by construction (built once here, read from the WebView's
         * non-UI intercept thread). Deliberately omits shared CDNs and
         * ambiguous domains: when unsure, the domain is left out.
         */
        val BLOCKED_HOSTS: Set<String> = setOf(
            // Google ads / analytics
            "doubleclick.net",
            "googlesyndication.com",
            "googleadservices.com",
            "adservice.google.com",
            "google-analytics.com",
            "googletagmanager.com",
            "ads.youtube.com",
            "adsystem.com",
            "app-measurement.com",
            // Meta / social pixels
            "connect.facebook.net",
            "graph.facebook.com",
            "ads-twitter.com",
            "analytics.twitter.com",
            "static.ads-twitter.com",
            "snap.licdn.com",
            "px.ads.linkedin.com",
            "ads.tiktok.com",
            "analytics.tiktok.com",
            // Ad exchanges / SSPs
            "adnxs.com",
            "criteo.com",
            "casalemedia.com",
            "pubmatic.com",
            "rubiconproject.com",
            "openx.net",
            "indexww.com",
            "sharethrough.com",
            "smartadserver.com",
            "teads.tv",
            "3lift.com",
            "bidswitch.net",
            "sovrn.com",
            "undertone.com",
            "adform.net",
            "adroll.com",
            "everesttech.net",
            "yieldmo.com",
            "amazon-adsystem.com",
            "media.net",
            // Content recommendation widgets
            "taboola.com",
            "outbrain.com",
            "revcontent.com",
            "zergnet.com",
            "mgid.com",
            "diginext.website",
            "sphinn.com",
            // Measurement / ad verification
            "scorecardresearch.com",
            "quantserve.com",
            "quantcount.com",
            "moatads.com",
            "doubleverify.com",
            "adsafeprotected.com",
            "chartbeat.com",
            "chartbeat.net",
            "bidr.io",
            "adsymptotic.com",
            "krxd.net",
        )

        /**
         * True when [host] is, or sits under, a blocked domain. Walks the
         * dot-labels right-to-left, so a typical host resolves in a couple of
         * set lookups with no regex or full-host precomputation needed.
         */
        fun isBlockedHost(host: String?): Boolean {
            if (host.isNullOrBlank()) return false
            var candidate = host.lowercase(Locale.US)
            while (true) {
                if (candidate in BLOCKED_HOSTS) return true
                val dot = candidate.indexOf('.')
                if (dot < 0) return false
                candidate = candidate.substring(dot + 1)
            }
        }
    }
}


/**
 * Minimal read-only ContentProvider so the system package installer (API 24+)
 * can read the downloaded APK from app-private storage via a content:// URI.
 * Top-level on purpose: the manifest references com.cosmos.app.UpdateFileProvider
 * directly, and a nested class would need a MainActivity\\$ suffix to inflate.
 */
class UpdateFileProvider : ContentProvider() {
    override fun onCreate(): Boolean = true
    override fun getType(uri: android.net.Uri): String = "application/vnd.android.package-archive"
    override fun openFile(uri: android.net.Uri, mode: String): ParcelFileDescriptor {
        if (mode != "r") throw SecurityException("read-only provider")
        val name = uri.lastPathSegment ?: throw IllegalArgumentException("no file")
        if (!name.endsWith(".apk")) throw IllegalArgumentException("apk only")
        val file = File(requireNotNull(context).filesDir, "$UPDATE_APK_DIR/$name")
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    }
    override fun query(uri: android.net.Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, order: String?): Cursor? = null
    override fun insert(uri: android.net.Uri, values: ContentValues?): android.net.Uri? = null
    override fun delete(uri: android.net.Uri, selection: String?, args: Array<out String>?): Int = 0
    override fun update(uri: android.net.Uri, values: ContentValues?, selection: String?, args: Array<out String>?): Int = 0
}

