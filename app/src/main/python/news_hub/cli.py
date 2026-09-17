from __future__ import annotations

import argparse
import codecs
import textwrap
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    DEFAULT_SOURCES,
    REMEDIES,
    blocker_histogram,
    build_metric,
    check_sources,
    collect_with_stats,
    render_text,
    select_on_manifold,
    write_report,
)
from .sources import OPTIONAL_SOURCES, dump_sources, load_sources


#: Stand-ins for the common characters WinAnsi (cp1252) cannot carry, so a PDF
#: line never silently loses them. Smart quotes, dashes, ellipsis and Western
#: European accents are all inside WinAnsi and pass through untouched.
_PDF_STAND_INS = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u00ad": "",  # soft hyphen
        "\u2212": "-",  # minus sign
        "\u2264": "<=",  # less-than or equal
        "\u2265": ">=",  # greater-than or equal
        "\u2190": "<-",  # leftwards arrow
        "\u2192": "->",  # rightwards arrow
        "\u2248": "~",  # almost equal
        "\u2009": " ",  # thin space
        "\u200a": " ",  # hair space
        "\u202f": " ",  # narrow no-break space
        "\u2028": " ",  # line separator
        "\u2029": " ",  # paragraph separator
        "\u200b": "",  # zero-width space
    }
)


def _winansi_fallback(exc: UnicodeEncodeError) -> tuple[str, int]:
    """One best-effort ASCII character per character WinAnsi cannot encode.

    Accented letters degrade to their base form (``ń`` -> ``n``) so names stay
    readable; scripts with no Latin decomposition degrade to ``?``, which at
    least marks that text was dropped rather than corrupting the word.
    """
    run = exc.object[exc.start : exc.end]
    stripped = unicodedata.normalize("NFKD", run).encode("ascii", "ignore").decode("ascii")
    return (stripped if len(stripped) == len(run) else "?" * len(run), exc.end)


codecs.register_error("news_hub_winansi", _winansi_fallback)


def _pdf_escape(text: str) -> str:
    """Encode one PDF content-stream line in WinAnsi and escape string specials.

    The old ``latin-1`` pass mangled smart quotes, dashes and accents into
    ``?``. The font object now declares ``/Encoding /WinAnsiEncoding``, which
    carries all of those; anything WinAnsi cannot represent goes through the
    explicit stand-in table and fallback above instead of a silent replacement.
    """
    text = text.translate(_PDF_STAND_INS)
    text = text.encode("cp1252", "news_hub_winansi").decode("cp1252")
    for old, new in (("\\", r"\\"), ("(", r"\("), (")", r"\)"), ("\r", " ")):
        text = text.replace(old, new)
    return text


def write_simple_pdf(text: str, path: Path, width: int = 105, lines_per_page: int = 48) -> None:
    """Write a plain, searchable PDF using only the Python standard library.

    Lines are word-wrapped and *appended* one per PDF line. An earlier version
    extended the list with a string, which iterates it character by character and
    turned every short line into a column of single letters.
    """
    wrapped: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            wrapped.append("")
            continue
        wrapped.extend(
            textwrap.wrap(raw, width=width, break_long_words=True, break_on_hyphens=False)
            or [raw[:width]]
        )
    pages = [wrapped[index : index + lines_per_page] for index in range(0, len(wrapped), lines_per_page)] or [[]]
    # Object numbering: 1 catalog, 2 pages, 3 font, then page/content pairs.
    object_bytes = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # filled after page IDs are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    actual_pages: list[bytes] = []
    for index, _ in enumerate(pages):
        # Each page object is followed by its content stream: page 4 -> stream 5.
        content_number = 5 + index * 2
        actual_pages.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>".encode()
        )
    object_bytes[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + i * 2} 0 R' for i in range(len(pages)))}] "
        f"/Count {len(pages)} >>"
    ).encode()
    final_objects: list[bytes] = object_bytes
    for index, page_lines in enumerate(pages):
        stream_lines = ["BT", "/F1 9 Tf", "48 752 Td", "11 TL"]
        for line in page_lines:
            stream_lines.append(f"({_pdf_escape(line)}) Tj T* ")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("cp1252")
        final_objects.extend([actual_pages[index], f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"])
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(final_objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(final_objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size {len(final_objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(output)


def _run_check(sources, args) -> int:
    print(f"Probing {len(sources)} sources (timeout {args.timeout}s, {args.jobs} workers)...")
    results = check_sources(sources, args.timeout, args.jobs)
    failures = 0
    for entry in results:
        status = "ok " if entry["ok"] else "FAIL"
        if not entry["ok"]:
            failures += 1
        detail = f"{entry['items']:>4} items in {entry['seconds']:>5}s"
        suffix = f"  {entry['error']}" if entry["error"] else ""
        print(f"[{status}] tier {entry['tier']} {entry['category'][:10]:<10} {detail}  {entry['name']}{suffix}")
    print(f"\n{len(results) - failures}/{len(results)} sources usable.")
    if failures:
        print("Remove or replace the failures in your source config; a silent dead feed hides a blind spot.")
    return 1 if failures else 0


def _write_config(sources, path: Path) -> int:
    dump_sources(sources, path)
    print(f"Wrote {len(sources)} sources to {path}. Edit it, then run with --sources {path}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a source-linked daily intelligence brief ranked by distance from the ideal evidence point.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", default="reports", help="directory for generated reports")
    parser.add_argument("--limit", type=int, default=20, help="maximum stories read from each source")
    parser.add_argument("--per-topic", type=int, default=8, help="stories shown per topic")
    parser.add_argument("--radius", type=float, default=None, help="hard ceiling on |d|_W; items outside it are dropped")
    parser.add_argument("--quota", type=int, default=24, help="minimum items the selection ball must hold")
    parser.add_argument(
        "--metric",
        choices=("variance", "whitened", "prior"),
        default="variance",
        help="how the cube is measured: variance-scaled weights, whitened weights, or the hand-set priors",
    )
    parser.add_argument(
        "--ridge",
        type=float,
        default=0.05,
        help="stabiliser for --metric: a variance floor (variance) or a diagonal addition (whitened)",
    )
    parser.add_argument(
        "--selection",
        choices=("diverse", "ball"),
        default="diverse",
        help="fill the ball by quality alone, or by quality times spread across the cube",
    )
    parser.add_argument("--sigma", type=float, default=0.15, help="diversity length scale in cube units for --selection diverse")
    parser.add_argument("--pool", type=int, default=3, help="candidates per brief slot when choosing a diverse set")
    parser.add_argument("--per-source", type=int, default=2, help="most items one outlet may occupy in a section")
    parser.add_argument("--sources", default=None, help="path to a JSON source catalog (see --write-config)")
    parser.add_argument(
        "--with-optional",
        action="store_true",
        help="also probe the flaky catalog entries (GDELT, retired-feed discovery lanes)",
    )
    parser.add_argument("--timeout", type=int, default=12, help="per-source network timeout in seconds")
    parser.add_argument("--jobs", type=int, default=8, help="parallel source fetches")
    parser.add_argument("--offline", action="store_true", help="create an empty report without network access")
    parser.add_argument("--no-pdf", action="store_true", help="skip PDF generation")
    parser.add_argument("--check-sources", action="store_true", help="probe every source and exit")
    parser.add_argument("--write-config", action="store_true", help="write the current catalog to --sources and exit")
    args = parser.parse_args(argv)

    if args.write_config:
        # Writing a template must not require the target file to exist yet.
        if not args.sources:
            parser.error("--write-config needs --sources <path> to know where to write")
        template = tuple(DEFAULT_SOURCES) + (OPTIONAL_SOURCES if args.with_optional else ())
        return _write_config(template, Path(args.sources))

    sources: tuple = DEFAULT_SOURCES
    if args.sources:
        try:
            sources = load_sources(args.sources)
        except (OSError, ValueError) as exc:
            parser.error(f"could not load source config {args.sources}: {exc}")
        print(f"Loaded {len(sources)} sources from {args.sources}.")
    elif args.with_optional:
        sources = tuple(sources) + OPTIONAL_SOURCES
        print(f"Added {len(OPTIONAL_SOURCES)} optional sources; some may fail on your network.")
    stats: list[dict[str, object]] = []
    if args.offline:
        # Offline short-circuits before every network path, including
        # --check-sources: an offline run must never touch the wire.
        if args.check_sources:
            print("Offline mode: skipping --check-sources (no network access requested).")
            return 0
        stories, errors = [], []
        print("Offline mode: no live feeds were requested.")
    elif args.check_sources:
        return _run_check(sources, args)
    else:
        print(f"Collecting from {len(sources)} sources with {args.jobs} workers...")
        stories, errors, stats = collect_with_stats(
            sources, args.limit, args.timeout, args.jobs, metric_mode=args.metric, ridge=args.ridge
        )

    generated = datetime.now(timezone.utc)
    metric = build_metric(stories, args.metric, args.ridge)
    selection = select_on_manifold(
        stories,
        args.quota,
        args.radius,
        metric=metric,
        mode=args.selection,
        sigma=args.sigma,
        pool=args.pool,
    )
    print(f"Metric: {metric.describe()}")
    print(
        f"{selection.candidates} of {len(stories)} ranked stories sit inside |d|_W <= "
        f"{selection.radius:.3f}; brief shows {len(selection.stories)} ({len(errors)} source errors)."
    )
    if selection.mode == "diverse":
        print(
            f"Diversity pick: {len({story.source for story in selection.stories})} distinct outlets from "
            f"{len(selection.stories)} slots."
        )
        if selection.skipped:
            print(
                f"Spread test: {selection.skipped} qualifying candidate(s) were not picked, "
                f"{len(selection.skipped_similar)} of them near-copies of a chosen lead."
            )
    if selection.at_capacity:
        print("Radius ceiling reached; the brief is deliberately short because the evidence is thin.")

    html_path, text_path = write_report(
        stories,
        errors,
        args.output,
        generated,
        args.per_topic,
        selection,
        stats,
        metric,
        args.per_source,
    )
    print(f"HTML: {html_path}\nText: {text_path}")
    if not args.no_pdf:
        # Same stem as the HTML/TXT pair (the local date), so the three files
        # always read as one issue no matter what the UTC clock says.
        pdf_path = html_path.with_suffix(".pdf")
        write_simple_pdf(
            render_text(
                stories,
                errors,
                generated,
                args.per_topic,
                selection,
                stats,
                metric,
                args.per_source,
            ),
            pdf_path,
        )
        print(f"PDF: {pdf_path}")

    reasons = blocker_histogram(selection.stories)
    if reasons:
        counts = "; ".join(f"{name} x{count}" for name, count in reasons[:3])
        print(f"Most common reasons the kept items are not at the ideal: {counts}")
        # The per-item remedy line is gone from the report, so the aggregate fix is
        # surfaced here instead: it is the one action that would improve most of
        # today's brief.
        print(f"  biggest single fix available: {REMEDIES.get(reasons[0][0], 'evidence is thin')}")
    if errors:
        print("Some sources failed; see the report's collection notes. No source failure stops the brief.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
