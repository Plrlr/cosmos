package com.cosmos.app

import android.app.Activity
import android.app.AlertDialog
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import android.widget.TextView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Locale

/**
 * Single-activity app: show a loading screen, run the news_hub pipeline in
 * Python on a background thread, then display the generated HTML report in a
 * WebView -- or swap the loading screen's text for a readable error.
 *
 * The report view is read-only chrome around the brief, plus two ways to get
 * more: tapping an article link opens it in a full-screen in-app browser
 * (never an external app or browser), and the history bar reopens any issue
 * the pipeline has already written to app-private storage.
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

    private lateinit var historyButton: TextView
    private lateinit var articleOverlay: View
    private lateinit var articleWebView: WebView
    private lateinit var articleUrlView: TextView
    private lateinit var articleProgress: ProgressBar

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        reportRoot = findViewById(R.id.reportRoot)
        loadingPanel = findViewById(R.id.loadingPanel)
        progressBar = findViewById(R.id.progressBar)
        statusTitle = findViewById(R.id.statusTitle)
        statusMessage = findViewById(R.id.statusMessage)

        historyButton = findViewById(R.id.historyButton)
        articleOverlay = findViewById(R.id.articleOverlay)
        articleWebView = findViewById(R.id.articleWebView)
        articleUrlView = findViewById(R.id.articleUrl)
        articleProgress = findViewById(R.id.articleProgress)

        configureWebView()
        configureArticleWebView()
        findViewById<View>(R.id.articleClose).setOnClickListener { hideArticle() }
        historyButton.setOnClickListener { showHistory() }

        // Starting Python and running the pipeline both block, so do it off
        // the UI thread; onPipelineFinished() hops back via runOnUiThread().
        Thread(::runPipeline, "cosmos-pipeline").start()
    }

    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = false // the report is static HTML; no JS needed
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
        // below, while file:// (the report itself) loads in this view as usual.
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val scheme = request.url.scheme?.lowercase(Locale.US)
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

            override fun doUpdateVisitedHistory(view: WebView?, url: String?, isReload: Boolean) {
                if (!url.isNullOrEmpty()) {
                    articleUrlView.text = Uri.parse(url).host ?: url
                }
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
        articleUrlView.text = Uri.parse(url).host ?: url
        articleProgress.progress = 0
        articleProgress.visibility = View.VISIBLE
        articleOverlay.visibility = View.VISIBLE
        articleWebView.loadUrl(url)
    }

    /** Dismiss the overlay and release the loaded page. */
    private fun hideArticle() {
        articleOverlay.visibility = View.GONE
        articleWebView.stopLoading()
        articleWebView.loadUrl("about:blank")
        articleUrlView.text = ""
    }

    /**
     * List the issues already written to filesDir/reports/YYYY-MM-DD.html and
     * load the chosen one into the report view. The report WebView shows any
     * local file the same way, so opening yesterday's brief costs nothing.
     */
    private fun showHistory() {
        val files = File(filesDir, "reports")
            .listFiles { file -> file.isFile && file.name.endsWith(".html") }
            ?.sortedWith(compareByDescending<File> { issueSortKey(it) }.thenByDescending { it.lastModified() })
            .orEmpty()

        if (files.isEmpty()) {
            AlertDialog.Builder(this)
                .setTitle(getString(R.string.history_title))
                .setMessage(getString(R.string.history_empty))
                .setPositiveButton(android.R.string.ok, null)
                .show()
            return
        }

        val shown = files.take(HISTORY_LIMIT)
        val labels = shown.map { describeIssue(it) }.toMutableList()
        if (files.size > shown.size) {
            // The tail is reported instead of silently dropped.
            labels.add(getString(R.string.history_overflow, files.size - shown.size))
        }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.history_title))
            .setItems(labels.toTypedArray()) { _, which ->
                if (which < shown.size) {
                    webView.loadUrl("file://" + shown[which].absolutePath)
                }
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    /** Sort key from a YYYY-MM-DD.html name; non-matching names sink to the end. */
    private fun issueSortKey(file: File): Long {
        val match = ISSUE_NAME.find(file.name) ?: return -1L
        val (year, month, day) = match.destructured
        return year.toLong() * 10000 + month.toLong() * 100 + day.toLong()
    }

    /** "Today" for the current issue, otherwise a readable date; the raw name as a fallback. */
    private fun describeIssue(file: File): String {
        val match = ISSUE_NAME.find(file.name) ?: return file.name
        val (year, month, day) = match.destructured
        val today = Calendar.getInstance()
        val isToday = today.get(Calendar.YEAR) == year.toInt() &&
            today.get(Calendar.MONTH) == month.toInt() - 1 &&
            today.get(Calendar.DAY_OF_MONTH) == day.toInt()
        if (isToday) {
            return getString(R.string.history_today)
        }
        val date = Calendar.getInstance().apply {
            clear()
            set(year.toInt(), month.toInt() - 1, day.toInt())
        }
        return SimpleDateFormat("d MMMM yyyy", Locale.getDefault()).format(date.time)
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        // Back closes the article first, then a page of it at a time, before
        // it is allowed to leave the report.
        if (articleOverlay.visibility == View.VISIBLE) {
            if (articleWebView.canGoBack()) {
                articleWebView.goBack()
            } else {
                hideArticle()
            }
            return
        }
        super.onBackPressed()
    }

    private fun runPipeline() {
        // App-private storage: writable without any permission, and readable
        // back through file:// for the WebView. The CLI's --output default is
        // a relative "reports/" dir, so always pass the absolute path.
        val outputDir = File(filesDir, "reports")
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
        runOnUiThread { onPipelineFinished(result, outputDir) }
    }

    private fun onPipelineFinished(result: String, outputDir: File) {
        if (result.startsWith("ok")) {
            // Reports are named YYYY-MM-DD.html; newest by modification time
            // also handles a leftover report from an earlier run today.
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
                getString(R.string.error_no_report) + "\n\n" + outputDir.absolutePath +
                    "\n\nPipeline status: " + result
            )
        } else {
            showError(getString(R.string.error_title), result)
        }
    }

    /** Error path: keep the panel visible, drop the spinner, explain what failed. */
    private fun showError(title: String, message: String) {
        progressBar.visibility = View.GONE
        statusTitle.text = title
        statusMessage.text = message
    }

    private companion object {
        /** Matches the pipeline's report filenames, e.g. 2026-09-15.html. */
        val ISSUE_NAME = Regex("""^(\d{4})-(\d{2})-(\d{2})\.html$""")

        /** The dialog stays scannable: older issues are summarised, not listed. */
        const val HISTORY_LIMIT = 60
    }
}
