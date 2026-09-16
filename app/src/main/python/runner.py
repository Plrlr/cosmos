"""Bridge between the Android app and the news_hub CLI.

Chaquopy imports this module and calls :func:`run` on a background thread.
The CLI's ``main`` uses argparse, which exits via ``SystemExit`` on bad
arguments, and the pipeline itself can raise on unexpected failures. This
wrapper converts every exit path -- normal completion, ``SystemExit``, or any
exception -- into a plain status string, so nothing can propagate across the
Python/Kotlin boundary and strand the UI on its loading screen.

The package itself is stdlib-only, so no ``pip`` configuration is needed:
Chaquopy ships the whole Python standard library with the app.
"""
from __future__ import annotations

from news_hub.cli import main


def run(output_dir: str, timeout: int = 12, jobs: int = 8) -> str:
    """Run the full news pipeline and return a machine-readable status.

    ``output_dir`` must be an absolute path in app-private storage (Kotlin
    passes ``context.filesDir/reports``); the CLI defaults ``--output`` to a
    relative ``reports/`` dir, which would land somewhere non-deterministic
    on Android, so it is always given explicitly.

    Statuses:
      ``ok:<code>``                  -- pipeline completed; ``<code>`` is
                                       ``main()``'s int return value
      ``exit:<code>``                -- argparse called ``SystemExit`` (e.g.
                                       bad arguments; should not happen with
                                       this fixed argument list)
      ``error:<ExcName>:<message>``  -- any other exception
    """
    try:
        code = main(
            [
                "--output", output_dir,
                "--timeout", str(timeout),
                "--jobs", str(jobs),
                "--no-pdf",
            ]
        )
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        return f"exit:{code}"
    except BaseException as exc:  # never let anything escape into Kotlin
        return f"error:{type(exc).__name__}:{exc}"
    return f"ok:{code}"
