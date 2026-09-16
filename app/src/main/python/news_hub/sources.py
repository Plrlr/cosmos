"""Source catalog and configuration.

The default catalog was probed live rather than guessed: every entry below is an
endpoint that actually answered and parsed when this module was written. Two
long-standing entries from the first version had to go -- Reuters retired its
``feeds.reuters.com`` RSS endpoints and the IMF news RSS URL now returns HTTP
errors. Both are replaced by working equivalents, and Reuters coverage is kept
through a Google News discovery lane that is explicitly labelled as discovery
rather than proof.

Feed URLs rot. Run ``python -m news_hub --check-sources`` to re-probe the whole
catalog on your own network before trusting it, and keep expensive or flaky
feeds out of the default set. Every retirements found in the first version are
recorded in this file's history: Reuters killed ``feeds.reuters.com``, the IMF
news RSS URL 404s, the World Bank ``?format=rss`` endpoint now serves HTML, and
Carnegie's ``solr`` feed now returns a web page rather than items.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

CATEGORIES: tuple[str, ...] = ("Technology", "Geopolitics", "Economics")

#: Tier meaning, kept next to the data so ranking decisions stay auditable.
TIER_LABELS: dict[int, str] = {
    1: "primary institution / first-party announcement",
    2: "established reporting or research institution",
    3: "specialist publication or discovery feed (leads, not proof)",
}


@dataclass(frozen=True)
class Source:
    """One collection endpoint.

    ``kind`` is ``"auto"`` for the usual case; set ``"gdelt"`` for the GDELT JSON
    article API and ``"rss"`` to force XML parsing.
    """

    name: str
    url: str
    category: str
    tier: int = 2
    kind: str = "auto"
    note: str = ""

    def resolved_kind(self) -> str:
        if self.kind and self.kind != "auto":
            return self.kind
        return "gdelt" if "gdeltproject.org" in self.url else "rss"


def google_news_source(
    query: str,
    name: str,
    category: str,
    tier: int = 3,
    window_hours: int = 24,
) -> Source:
    """Build a Google News discovery lane for ``query``.

    Discovery lanes keep an outlet visible after its own feeds are retired (the
    Reuters feeds, for example) without pretending an aggregator is the
    publisher, which is why they are always tier 3. Use the ``site:`` operator
    for a specific publisher; ``allinurl:`` silently returns nothing here.
    """
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(f"{query} when:{window_hours}h")
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    return Source(name, url, category, tier, "rss", note="aggregated discovery lane")


DEFAULT_SOURCES: tuple[Source, ...] = (
    # --- Tier 1: primary institutions -------------------------------------- #
    Source("Federal Reserve Press", "https://www.federalreserve.gov/feeds/press_all.xml", "Economics", 1),
    Source("ECB Press", "https://www.ecb.europa.eu/rss/press.html", "Economics", 1),
    Source("ECB Blog", "https://www.ecb.europa.eu/rss/blog.html", "Economics", 1),
    Source("BIS Press Releases", "https://www.bis.org/doclist/all_pressrels.rss", "Economics", 1),
    Source("WTO News", "https://www.wto.org/library/rss/latest_news_e.xml", "Economics", 1),
    Source("BLS Latest Numbers", "https://www.bls.gov/feed/bls_latest.rss", "Economics", 1),
    Source("EIA Today in Energy", "https://www.eia.gov/rss/todayinenergy.xml", "Economics", 1),
    Source("CISA Advisories", "https://www.cisa.gov/cybersecurity-advisories/all.xml", "Technology", 1),
    Source("NIST News", "https://www.nist.gov/news-events/news/rss.xml", "Technology", 1),
    Source("OpenAI News", "https://openai.com/news/rss.xml", "Technology", 1),
    Source("Google AI Blog", "https://blog.google/technology/ai/rss/", "Technology", 1),
    Source("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "Geopolitics", 1),
    Source(
        "US Department of Defense",
        "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20",
        "Geopolitics",
        1,
    ),
    # --- Tier 2: established reporting and research ------------------------ #
    Source("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "Technology", 2),
    Source("MIT Technology Review", "https://www.technologyreview.com/feed/", "Technology", 2),
    Source("Ars Technica", "https://arstechnica.com/feed/", "Technology", 2),
    Source("arXiv cs.AI", "https://export.arxiv.org/rss/cs.AI", "Technology", 2, note="preprints: not peer reviewed"),
    Source("arXiv cs.CR", "https://export.arxiv.org/rss/cs.CR", "Technology", 2, note="preprints: not peer reviewed"),
    Source("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "Geopolitics", 2),
    Source("NPR World", "https://feeds.npr.org/1004/rss.xml", "Geopolitics", 2),
    Source("NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "Geopolitics", 2),
    Source("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "Geopolitics", 2),
    # The bare ``/feed/`` path now serves the HTML homepage; the article post-type
    # query is the endpoint that still returns RSS.
    Source("Brookings", "https://www.brookings.edu/feed/?post_type=article", "Geopolitics", 2),
    Source("Atlantic Council", "https://www.atlanticcouncil.org/feed/", "Geopolitics", 2),
    Source("European Council on Foreign Relations", "https://ecfr.eu/feed/", "Geopolitics", 2),
    Source("International Crisis Group", "https://www.crisisgroup.org/rss.xml", "Geopolitics", 2),
    Source("Stimson Center", "https://www.stimson.org/feed/", "Geopolitics", 2),
    Source("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "Economics", 2),
    Source("NPR Business", "https://feeds.npr.org/1006/rss.xml", "Economics", 2),
    Source("Financial Times", "https://www.ft.com/rss/home", "Economics", 2),
    Source("The Economist: Finance", "https://www.economist.com/finance-and-economics/rss.xml", "Economics", 2),
    Source("PIIE", "https://www.piie.com/rss/update.xml", "Economics", 2),
    Source("arXiv q-fin", "https://export.arxiv.org/rss/q-fin", "Economics", 2, note="preprints: not peer reviewed"),
    # --- Tier 3: specialist and discovery ---------------------------------- #
    Source("The Verge", "https://www.theverge.com/rss/index.xml", "Technology", 3),
    Source("TechCrunch", "https://techcrunch.com/feed/", "Technology", 3),
    Source("Wired", "https://www.wired.com/feed/rss", "Technology", 3),
    google_news_source("site:reuters.com", "Reuters discovery lane", "Geopolitics"),
    google_news_source("site:reuters.com (markets OR economy OR inflation)", "Reuters markets discovery lane", "Economics"),
)

#: Catalog entries that answered here but are flaky by nature: GDELT rate-limits
#: and blocks datacentre traffic, and the World Bank retired its RSS endpoint
#: (``?format=rss`` now serves HTML). They are kept out of the default set so
#: that a fresh install reports zero failures, and are easy to opt into with
#: ``--sources`` after checking them on your own network.
OPTIONAL_SOURCES: tuple[Source, ...] = (
    Source(
        "GDELT Technology",
        "https://api.gdeltproject.org/api/v2/doc/doc?query=(technology%20OR%20semiconductor%20OR%20cyber)&mode=artlist&format=json&maxrecords=30&sort=datedesc",
        "Technology",
        3,
        "gdelt",
        note="rate-limited; may refuse non-residential traffic",
    ),
    Source(
        "GDELT Geopolitics",
        "https://api.gdeltproject.org/api/v2/doc/doc?query=(war%20OR%20sanctions%20OR%20diplomacy%20OR%20conflict)&mode=artlist&format=json&maxrecords=30&sort=datedesc",
        "Geopolitics",
        3,
        "gdelt",
        note="rate-limited; may refuse non-residential traffic",
    ),
    Source(
        "GDELT Economics",
        "https://api.gdeltproject.org/api/v2/doc/doc?query=(inflation%20OR%20tariffs%20OR%20recession%20OR%20central%20bank)&mode=artlist&format=json&maxrecords=30&sort=datedesc",
        "Economics",
        3,
        "gdelt",
        note="rate-limited; may refuse non-residential traffic",
    ),
    google_news_source("site:worldbank.org", "World Bank discovery lane", "Economics", window_hours=48),
    google_news_source("site:imf.org", "IMF discovery lane", "Economics", window_hours=48),
)


def _coerce_source(raw: object, index: int) -> Source:
    if not isinstance(raw, dict):
        raise ValueError(f"source #{index} must be an object, got {type(raw).__name__}")
    missing = [key for key in ("name", "url") if not str(raw.get(key, "")).strip()]
    if missing:
        raise ValueError(f"source #{index} is missing required field(s): {', '.join(missing)}")
    try:
        tier = int(raw.get("tier", 2))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"source #{index} has a non-numeric tier") from exc
    category = str(raw.get("category", "Technology")).strip()
    if category not in CATEGORIES:
        raise ValueError(
            f"source #{index} category {category!r} is not one of {', '.join(CATEGORIES)}"
        )
    return Source(
        name=str(raw["name"]).strip(),
        url=str(raw["url"]).strip(),
        category=category,
        tier=max(1, min(3, tier)),
        kind=str(raw.get("kind", "auto")).strip() or "auto",
        note=str(raw.get("note", "")).strip(),
    )


def load_sources(path: str | Path) -> tuple[Source, ...]:
    """Load a custom catalog from JSON.

    Accepts either a bare list or ``{"sources": [...]}``. Malformed entries raise
    ``ValueError`` naming the offending index, because a silent typo in a source
    list is the failure mode that makes a brief look complete when it is not.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("sources", [])
    if not isinstance(payload, list):
        raise ValueError("source config must be a list or an object with a 'sources' list")
    sources = tuple(_coerce_source(raw, index) for index, raw in enumerate(payload, 1))
    if not sources:
        raise ValueError("source config contains no sources")
    return sources


def dump_sources(sources: Iterable[Source], path: str | Path) -> Path:
    """Write a catalog to JSON, so the default set can be edited in place."""
    target = Path(path)
    target.write_text(
        json.dumps({"sources": [asdict(source) for source in sources]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
