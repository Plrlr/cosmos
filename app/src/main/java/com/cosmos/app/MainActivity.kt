package com.cosmos.app

import android.app.Activity
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.WebChromeClient
import android.webkit.WebHistoryItem
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import android.widget.TextView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.ByteArrayInputStream
import java.io.File
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

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

        // Starting Python and running the pipeline both block, so do it off
        // the UI thread; the completion path hops back via runOnUiThread().
        Thread(::runPipeline, "cosmos-pipeline").start()
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

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        // Back closes the article first, then a page of it at a time, before
        // it is allowed to leave the report.
        if (articleOverlay.visibility == View.VISIBLE) {
            goArticleBack()
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
            val prefix = if (file.name == todayName) "Today · " else ""
            entries.append("""<li><a href="${file.name}">$prefix$label</a></li>""")
        }

        val html = """
            <!doctype html><html><head><meta charset="utf-8"><title>Cosmos</title><style>
            body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#fff;max-width:900px;margin:40px auto;color:#172033;line-height:1.6;padding:0 16px}
            h1{font-size:18px;margin:0 0 12px}.muted{color:#667085;font-size:12px}
            ul{list-style:none;padding:0;margin:0}li{margin:0 0 16px;font-size:15px}
            a{color:#174ea6;text-decoration:none}
            </style></head><body>
            <h1>Cosmos</h1>
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

    private companion object {
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
