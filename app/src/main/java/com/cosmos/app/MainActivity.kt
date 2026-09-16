package com.cosmos.app

import android.app.Activity
import android.os.Bundle
import android.view.View
import android.webkit.WebView
import android.widget.ProgressBar
import android.widget.TextView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/**
 * Single-activity app: show a loading screen, run the news_hub pipeline in
 * Python on a background thread, then display the generated HTML report in a
 * WebView -- or swap the loading screen's text for a readable error.
 *
 * Framework widgets only (no AndroidX): the theme, WebView and views all come
 * from android.*, so the build has zero runtime dependencies beyond Chaquopy
 * and the Kotlin stdlib.
 */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private lateinit var loadingPanel: View
    private lateinit var progressBar: ProgressBar
    private lateinit var statusTitle: TextView
    private lateinit var statusMessage: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        loadingPanel = findViewById(R.id.loadingPanel)
        progressBar = findViewById(R.id.progressBar)
        statusTitle = findViewById(R.id.statusTitle)
        statusMessage = findViewById(R.id.statusMessage)

        configureWebView()

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
                webView.visibility = View.VISIBLE
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
}
