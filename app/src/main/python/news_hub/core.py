"""The low-resource news intelligence pipeline.

The project deliberately keeps collection, judgment, and presentation separate:
network sources provide leads; the report preserves links and evidence rather
than pretending an AI summary is primary evidence.

Judgment is geometric. :mod:`news_hub.geometry` turns each lead into a point of
the evidence cube whose origin is the ideal item (fresh, authoritative,
independently confirmed, on topic, with a real summary). Ranking is the distance
from that origin, and *selection* is a ball around it: an item is chosen when it
sits inside the surface ``|d|_W = r*``, so boundary ties are kept instead of
being cut off by rank order. The report prints the surface radius, each item's
coordinates, and the single axis that pushed it away from the origin.
"""
from __future__ import annotations

import html
import json
import math
import re
import time
from html.entities import html5 as HTML_ENTITIES
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Sequence

from . import __version__
from . import geometry
from .sources import CATEGORIES, DEFAULT_SOURCES, TIER_LABELS, Source

#: Identifies the real tool and how to reach it, without pretending to be a
#: browser or pointing at a placeholder domain.
USER_AGENT = f"news_hub/{__version__} (+Python stdlib urllib; personal news brief)"
DEFAULT_TIMEOUT = 12
DEFAULT_JOBS = 8
#: How much of the publisher's blurb each report item shows; the full stored
#: description (up to 1500 characters) still drives the substance axis.
SUMMARY_CHARS = 600

#: What to go find when an item is not at the ideal, keyed by its blocking axis.
REMEDIES: dict[str, str] = {
    "corroboration": "needs a second independent outlet or a primary document",
    "age": "stale for a 48h brief — look for a newer report",
    "authority": "specialist or discovery feed — find the primary announcement",
    "relevance": "thin topic vocabulary — confirm it really belongs to this category",
    "substance": "publisher gave no summary — this is a headline only",
}


def _clip(value: str, limit: int = SUMMARY_CHARS) -> str:
    """Trim display text to a word boundary without touching the stored evidence."""
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:.") + " …"


@dataclass
class Story:
    title: str
    url: str
    source: str
    category: str
    published: datetime | None = None
    description: str = ""
    source_tier: int = 2
    tags: list[str] = field(default_factory=list)
    related_sources: list[str] = field(default_factory=list)
    keyword_hits: float = 0.0
    axes: tuple[float, ...] = ()
    radius: float = 0.0
    trust: tuple[float, float, float] = (0.0, 0.0, 0.0)
    trust_radius: float = 0.0
    blocker: str = ""
    blocker_share: float = 0.0
    nearest: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Display quality in 0-100: the complement of the distance to the ideal."""
        return round(100.0 * (1.0 - self.radius), 1)

    @property
    def corroborators(self) -> int:
        return len(self.related_sources)

    @property
    def summary(self) -> str:
        """Publisher blurb trimmed for display; the full text still drives the axes."""
        return _clip(self.description)

    @property
    def remedy(self) -> str:
        """What to go collect to shrink this item's distance from the ideal."""
        if self.blocker_share <= 0.0:
            return "already at the ideal origin"
        return REMEDIES.get(self.blocker, "evidence is thin")

    @property
    def date_label(self) -> str:
        if not self.published:
            return "Date unavailable"
        return self.published.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @property
    def evidence_label(self) -> str:
        if self.related_sources:
            return f"Corroborated by: {', '.join(self.related_sources)}"
        return "Single-source lead — verify before acting."


@dataclass
class Selection:
    """The ball around the origin that produced the brief, and how it was filled."""

    stories: list[Story]
    radius: float
    quota: int
    at_capacity: bool = False
    mode: str = "ball"
    feasible: int = 0
    candidates: int = 0

    @property
    def surface(self) -> str:
        return f"|d|_W <= {self.radius:.3f}"


KEYWORDS: dict[str, tuple[str, ...]] = {
    "Technology": (
        "artificial intelligence", "machine learning", "semiconductor", "data center",
        "chip", "chips", "cyber", "cybersecurity", "cloud", "software", "robotics",
        "robot", "quantum", "space", "satellite", "biotech", "algorithm", "encryption",
        "malware", "ransomware", "open source", "model", "gpu", "battery", "nuclear",
        "technology", "ai",
    ),
    "Geopolitics": (
        "ceasefire", "sanctions", "sanction", "diplomacy", "diplomatic", "military",
        "missile", "airstrike", "invasion", "treaty", "alliance", "nato", "united nations",
        "border", "troops", "conflict", "war", "election", "parliament", "coalition",
        "summit", "president", "prime minister", "government", "sovereignty", "annex",
        "gaza", "ukraine", "russia", "china", "iran", "israel", "taiwan", "venezuela",
    ),
    "Economics": (
        "interest rate", "central bank", "monetary policy", "balance sheet",
        "inflation", "deflation", "recession", "gross domestic product", "gdp",
        "unemployment", "jobless", "jobs report", "tariff", "tariffs", "trade deficit",
        "supply chain", "bond yield", "treasury", "debt ceiling", "fiscal", "stimulus",
        "currency", "commodity", "crude", "opec", "housing", "mortgage", "earnings",
        "bank", "banks", "markets", "stocks", "economy", "economist", "fed",
    ),
}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _text(element: ET.Element | None) -> str:
    return _strip_markup("".join(element.itertext())) if element is not None else ""


def _first(element: ET.Element, names: tuple[str, ...]) -> ET.Element | None:
    for child in element.iter():
        if child.tag.rsplit("}", 1)[-1].lower() in names:
            return child
    return None


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


_NAMED_ENTITY = re.compile(r"&([a-zA-Z][a-zA-Z0-9]{1,31});")
_BARE_AMPERSAND = re.compile(r"&(?!#\d+;|#x[0-9a-fA-F]+;|amp;|lt;|gt;|quot;|apos;)")
_INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
# ``link`` and ``meta`` are deliberately excluded: in RSS and Atom ``<link>`` is a
# real text element, and rewriting it would corrupt every well-formed feed.
_VOID_HTML_TAG = re.compile(r"<(br|hr|img|wbr|source|embed|param|track|area)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)/?>", re.IGNORECASE)


def _sanitize_xml(payload: bytes) -> bytes:
    """Repair the sloppiness real publishers ship in their feeds.

    Plenty of otherwise solid feeds are not well-formed XML: they carry HTML
    entities XML does not define (``&nbsp;``), bare ampersands, control
    characters, or unclosed void tags. ElementTree refuses all of it, so one
    careless publisher would silently cost the brief a whole source. Named
    entities are rewritten as numeric references using the standard library's
    entity table, unknown ones are escaped, and void tags are closed.
    """
    text = _INVALID_XML_CHARS.sub(" ", payload.decode("utf-8", "replace"))

    def replace_entity(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in {"amp", "lt", "gt", "quot", "apos"}:
            return match.group(0)
        character = HTML_ENTITIES.get(name + ";")
        if character is None or len(character) != 1:
            return "&amp;" + name + ";"
        return f"&#{ord(character)};"

    def close_void_tag(match: re.Match[str]) -> str:
        attributes = match.group(2).rstrip().rstrip("/")
        return f"<{match.group(1)}{attributes}/>"

    text = _NAMED_ENTITY.sub(replace_entity, text)
    text = _VOID_HTML_TAG.sub(close_void_tag, text)
    text = _BARE_AMPERSAND.sub("&amp;", text)
    return text.encode("utf-8")


def parse_feed(payload: bytes, source: Source) -> list[Story]:
    """Parse RSS 2, Atom, and namespaced feeds defensively.

    A strict parse is tried first; a malformed payload is retried once after
    :func:`_sanitize_xml` rather than throwing a whole source away.
    """
    try:
        return _extract_stories(payload, source)
    except ET.ParseError:
        return _extract_stories(_sanitize_xml(payload), source)


def _extract_stories(payload: bytes, source: Source) -> list[Story]:
    root = ET.fromstring(payload)
    stories: list[Story] = []
    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    for node in nodes:
        title = _text(_first(node, ("title",)))
        link_node = _first(node, ("link",))
        url = ""
        if link_node is not None:
            url = link_node.attrib.get("href", "") or _text(link_node)
        description = _text(_first(node, ("description", "summary", "content")))
        published = _parse_date(_text(_first(node, ("pubdate", "published", "updated", "date"))))
        if title and url:
            stories.append(
                Story(
                    title[:300],
                    url.strip(),
                    source.name,
                    source.category,
                    published,
                    description[:1500],
                    source.tier,
                )
            )
    return stories


#: Extra attempts after the first one, and the pause between them, for feeds
#: that answer HTTP 429 (rate limiting) — GDELT in particular refuses
#: datacentre traffic this way.
_RATE_LIMIT_RETRIES = 1
_RATE_LIMIT_BACKOFF = 2.0


def _fetch_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Fetch one URL, retrying once with a short pause on HTTP 429.

    One patient retry turns most rate-limit refusals into a usable payload
    without slowing down the healthy catalog. Every other failure still raises
    immediately, so a dead feed stays cheap to skip.
    """
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < _RATE_LIMIT_RETRIES:
                time.sleep(_RATE_LIMIT_BACKOFF)
                continue
            if exc.code == 429:
                raise OSError(
                    f"HTTP 429 rate limit on {url} after {_RATE_LIMIT_RETRIES + 1} attempts; "
                    "wait and try again later"
                ) from exc
            raise


def _parse_gdelt_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_gdelt(payload: bytes, source: Source) -> list[Story]:
    data = json.loads(payload.decode("utf-8"))
    stories: list[Story] = []
    for item in data.get("articles", []):
        title, url = item.get("title", "").strip(), item.get("url", "").strip()
        if title and url:
            domain = item.get("domain") or source.name
            stories.append(
                Story(
                    title[:300],
                    url,
                    domain,
                    source.category,
                    _parse_gdelt_date(item.get("seendate", "")),
                    "GDELT discovery lead; open the publisher link for the full report.",
                    source.tier,
                )
            )
    return stories


def fetch_feed(source: Source, timeout: int = DEFAULT_TIMEOUT) -> list[Story]:
    payload = _fetch_bytes(source.url, timeout)
    if source.resolved_kind() == "gdelt":
        return parse_gdelt(payload, source)
    return parse_feed(payload, source)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _keyword_count(text: str, keyword: str) -> int:
    """Count a keyword in ``text``.

    Single words are matched on word boundaries; multi-word phrases are matched
    as phrases. The boundary marker must be a single backslash (``\\b``): a
    doubled backslash matches a literal backslash and silently zeroes out every
    single-word count, which is what an earlier version of this file did.
    """
    if " " in keyword:
        return text.count(keyword)
    return len(re.findall(r"\b" + re.escape(keyword) + r"\b", text))


def classify(story: Story) -> str:
    """Pick the topic using weighted keyword evidence and a tier-aware prior.

    Title matches count double, since a publisher's own headline is a stronger
    topic signal than a blurb. A tier-1 source (central bank, regulator, ministry)
    labels its own material reliably, so its declared category only loses to
    keyword evidence that is clearly stronger rather than marginally stronger.
    """
    title, description = story.title.lower(), story.description.lower()
    scores = {
        category: sum(_keyword_count(title, word) * 2.0 + _keyword_count(description, word) for word in words)
        for category, words in KEYWORDS.items()
    }
    best = max(CATEGORIES, key=lambda category: scores[category])
    if scores[best] <= 0:
        return story.category
    declared = scores.get(story.category, 0.0)
    if story.source_tier <= 1 and best != story.category and scores[best] < 1.5 * declared + 1.0:
        return story.category
    return best


def _tokens(title: str) -> set[str]:
    stop = {"this", "that", "with", "from", "about", "after", "says", "what", "amid", "into"}
    return {token for token in re.findall(r"[a-z0-9]{4,}", title.lower()) if token not in stop}


def _similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(1, len(left | right))


def _recency_key(story: Story) -> tuple[float, int, str]:
    published = story.published or datetime.min.replace(tzinfo=timezone.utc)
    return (-published.timestamp(), story.source_tier, story.title)


def _authority_key(story: Story) -> tuple[int, float, str]:
    published = story.published or datetime.min.replace(tzinfo=timezone.utc)
    return (story.source_tier, -published.timestamp(), story.title)


def deduplicate(stories: Iterable[Story], similarity: float = 0.52) -> list[Story]:
    """Collapse near-identical headlines while preserving corroborating outlets.

    Clustering is done against each cluster's *representative* headline rather
    than only the items already kept, so a story appearing in five outlets folds
    into one entry. The representative is the most authoritative, then freshest
    member, and the entry keeps that member's own title, link, and source: the
    link must always match the outlet the report credits it to.
    """
    clusters: list[list[Story]] = []
    for story in sorted(stories, key=_recency_key):
        words = _tokens(story.title)
        for cluster in clusters:
            if _similarity(words, _tokens(cluster[0].title)) >= similarity:
                cluster.append(story)
                break
        else:
            clusters.append([story])

    kept: list[Story] = []
    for cluster in clusters:
        representative = min(cluster, key=_authority_key)
        seen: list[str] = []
        for member in sorted(cluster, key=_recency_key):
            if member.source != representative.source and member.source not in seen:
                seen.append(member.source)
        representative.related_sources = seen
        kept.append(representative)
    return sorted(kept, key=_recency_key)


# --------------------------------------------------------------------------- #
# Geometric judgment
# --------------------------------------------------------------------------- #
def keyword_hits(story: Story) -> float:
    """Weighted topic-vocabulary evidence for the story's assigned category."""
    title, description = story.title.lower(), story.description.lower()
    return sum(
        _keyword_count(title, word) * 2.0 + _keyword_count(description, word)
        for word in KEYWORDS.get(story.category, ())
    )


def place(story: Story, now: datetime | None = None) -> Story:
    """Measure one story: fill in its cube coordinates.

    Deliberately metric-independent, because the metric is *fitted* to these very
    coordinates: you cannot score a batch before you have measured it.
    """
    now = now or datetime.now(timezone.utc)
    age_hours: float | None = None
    if story.published:
        # Kept signed on purpose: a future date is a defect the age axis must see,
        # not something to clamp into a perfect freshness score.
        age_hours = (now - story.published).total_seconds() / 3600.0
    story.keyword_hits = keyword_hits(story)
    story.axes = geometry.axes(
        age_hours=age_hours,
        tier=story.source_tier,
        corroborators=len(story.related_sources),
        keyword_hits=story.keyword_hits,
        description_chars=len(story.description),
    )
    return story


def score_with(story: Story, metric: geometry.Metric) -> Story:
    """Apply a metric to a placed story: radius, trust projection, blocking axis."""
    story.radius = metric.radius(story.axes)
    story.trust = geometry.trust_vector(story.axes)
    story.trust_radius = metric.trust_radius(story.axes)
    story.blocker, story.blocker_share = metric.blocker(story.axes)
    return story


def embed(story: Story, now: datetime | None = None, metric: geometry.Metric | None = None) -> Story:
    """Place one story in the evidence cube and record its geometry."""
    return score_with(place(story, now), metric or geometry.PRIOR_METRIC)


def build_metric(
    stories: Iterable[Story],
    mode: str = "variance",
    ridge: float = 0.05,
    now: datetime | None = None,
) -> geometry.Metric:
    """Fit the ranking metric to the batch being ranked (see ``geometry.fit_metric``).

    Stories that have not been measured yet are placed first: fitting on an empty
    batch would silently return the prior metric, which is exactly the kind of
    quiet downgrade that is hard to notice in a report.
    """
    rows = [story.axes if story.axes else place(story, now).axes for story in stories]
    return geometry.fit_metric(rows, geometry.WEIGHTS, mode=mode, ridge=ridge)


def ensure_scored(
    stories: Iterable[Story],
    metric: geometry.Metric | None = None,
    now: datetime | None = None,
) -> list[Story]:
    """Guarantee every story carries cube coordinates, so no renderer sees a blank row."""
    metric = metric or geometry.PRIOR_METRIC
    return [story if story.axes else score_with(place(story, now), metric) for story in stories]


def rank(
    stories: Iterable[Story],
    now: datetime | None = None,
    metric: geometry.Metric | None = None,
) -> list[Story]:
    """Place every story in the cube, order them by distance from the origin, and
    record each leading story's local neighbourhood."""
    now = now or datetime.now(timezone.utc)
    metric = metric or geometry.PRIOR_METRIC
    placed = [score_with(place(story, now), metric) if not story.axes else score_with(story, metric) for story in stories]
    ordered = sorted(placed, key=lambda story: (story.radius, _recency_key(story)))
    _annotate_neighbours(ordered, metric)
    return ordered


def _annotate_neighbours(
    stories: Sequence[Story],
    metric: geometry.Metric | None = None,
    limit: int = 24,
    count: int = 2,
) -> None:
    """Record each leading story's nearest neighbours in the cube.

    Two items 0.05 apart are the same event worded differently; the report shows
    that instead of letting the reader rediscover it by opening every link.
    """
    for story in stories:
        story.nearest = []  # keep the annotation idempotent across repeated ranking
    if len(stories) < 3:
        return
    points = [story.axes for story in stories]
    for index in range(min(limit, len(stories))):
        for other in geometry.nearest_neighbours(points[index], points, count=count, metric=metric):
            stories[index].nearest.append(
                f"{stories[other].source} — {_clip(stories[other].title, 72)}"
            )


def select_on_manifold(
    stories: Sequence[Story],
    quota: int = 24,
    max_radius: float | None = None,
    metric: geometry.Metric | None = None,
    mode: str = "ball",
    sigma: float = 0.15,
    pool: int = 3,
) -> Selection:
    """Choose the brief: a candidate ball around the ideal, then a rule for filling it.

    The ball is the *feasibility* rule: the surface ``|d|_W = r*`` is the
    boundary, so every item inside qualifies, ties included, and ``max_radius`` is
    a hard ceiling that shortens the brief rather than padding it.

    Filling that set is a separate question, because a ball is a per-item test
    and says nothing about how items relate to each other. ``mode="ball"`` keeps
    the classic rule: everything inside, best first. ``mode="diverse"`` keeps
    everything inside as *candidates* -- sized at ``pool`` times the brief -- and
    then picks ``quota`` items greedily by quality times spread. Diversity needs
    slack: with a ball holding exactly ``quota`` items there is nothing to choose.
    """
    metric = metric or geometry.PRIOR_METRIC
    radii = [story.radius for story in stories]
    candidates = quota if mode == "ball" else max(quota, int(round(quota * max(1, pool))))
    indices, star = geometry.ball_selection(radii, candidates, max_radius)
    feasible = [stories[index] for index in indices]
    chosen = feasible
    if mode == "diverse" and len(feasible) > quota:
        picked = geometry.diverse_selection(
            points=[story.axes for story in feasible],
            qualities=[max(0.0, 1.0 - story.radius) for story in feasible],
            quota=quota,
            metric=metric,
            sigma=sigma,
        )
        chosen = [feasible[index] for index in picked]
    capacity = max_radius is not None and star >= float(max_radius) - 1e-12 and len(chosen) < quota
    return Selection(chosen, star, quota, capacity, mode, len(feasible), len(feasible))


def collect_with_stats(
    sources: Iterable[Source] = DEFAULT_SOURCES,
    limit_per_source: int = 20,
    timeout: int = DEFAULT_TIMEOUT,
    jobs: int = DEFAULT_JOBS,
    now: datetime | None = None,
    metric_mode: str = "variance",
    ridge: float = 0.05,
) -> tuple[list[Story], list[str], list[dict[str, object]]]:
    """Fetch every source concurrently, then classify, fold duplicates, and rank.

    Concurrency matters because the catalog is now broad: a handful of slow or
    unresponsive feeds must not multiply the wait for the whole brief. Failures
    are captured per source and surfaced; one dead feed never kills a run.
    """
    source_list = list(sources)

    def pull(source: Source) -> tuple[Source, list[Story], str]:
        try:
            return source, fetch_feed(source, timeout)[:limit_per_source], ""
        except Exception as exc:  # feeds are optional; a failure must stay visible
            return source, [], f"{exc.__class__.__name__}: {exc}"

    stats: list[dict[str, object]] = []
    stories: list[Story] = []
    errors: list[str] = []
    if source_list:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for source, items, error in pool.map(pull, source_list):
                stats.append(
                    {
                        "name": source.name,
                        "category": source.category,
                        "tier": source.tier,
                        "items": len(items),
                        "error": error,
                    }
                )
                if error:
                    errors.append(f"{source.name} [{source.category}]: {error}")
                stories.extend(items)

    for story in stories:
        story.category = classify(story)
    placed = [place(story, now) for story in deduplicate(stories)]
    metric = build_metric(placed, metric_mode, ridge)
    ranked = rank(placed, now, metric)
    return ranked, errors, stats


def collect(
    sources: Iterable[Source] = DEFAULT_SOURCES,
    limit_per_source: int = 20,
    timeout: int = DEFAULT_TIMEOUT,
    jobs: int = DEFAULT_JOBS,
    metric_mode: str = "variance",
) -> tuple[list[Story], list[str]]:
    stories, errors, _ = collect_with_stats(
        sources, limit_per_source, timeout, jobs, metric_mode=metric_mode
    )
    return stories, errors


def check_sources(
    sources: Iterable[Source] = DEFAULT_SOURCES,
    timeout: int = DEFAULT_TIMEOUT,
    jobs: int = DEFAULT_JOBS,
) -> list[dict[str, object]]:
    """Probe every source and report whether it answered with parsable items.

    Feed URLs rot silently. This turns "the brief looks thin today" into "this
    endpoint has been dead for a week", which is the difference between a tool
    you can trust and a tool that hides its own blind spots.
    """
    source_list = list(sources)

    def probe(source: Source) -> dict[str, object]:
        started = time.monotonic()
        try:
            items = fetch_feed(source, timeout)
            return {
                "name": source.name,
                "url": source.url,
                "category": source.category,
                "tier": source.tier,
                "ok": bool(items),
                "items": len(items),
                "error": "" if items else "reachable but returned no parsable items",
                "seconds": round(time.monotonic() - started, 2),
            }
        except Exception as exc:
            return {
                "name": source.name,
                "url": source.url,
                "category": source.category,
                "tier": source.tier,
                "ok": False,
                "items": 0,
                "error": f"{exc.__class__.__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 2),
            }

    if not source_list:
        return []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(probe, source_list))


# --------------------------------------------------------------------------- #
# The salience map
# --------------------------------------------------------------------------- #
CATEGORY_COLORS: dict[str, str] = {
    "Technology": "#2563eb",
    "Geopolitics": "#b45309",
    "Economics": "#047857",
}
_ISO_X = 0.8660254037844386  # cos(30 degrees)
_ISO_Y = 0.5  # sin(30 degrees)


def _iso(point: Sequence[float]) -> tuple[float, float]:
    """Isometric projection of the 3-D trust cube onto the page.

    ``(age, authority, corroboration)`` all increase away from the origin, so the
    ideal corner projects to the upper-centre of the map and the three failure
    directions fan out from it.
    """
    x, y, z = (float(value) for value in point[:3])
    return (x - y) * _ISO_X, (x + y) * _ISO_Y - z


#: Single-letter prefixes for the map labels, so a marker points at a section entry.
CATEGORY_LETTERS: dict[str, str] = {"Technology": "T", "Geopolitics": "G", "Economics": "E"}

#: The three failure directions of the trust cube, in axis order.
AXIS_RAYS: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("age", (1.0, 0.0, 0.0)),
    ("authority", (0.0, 1.0, 0.0)),
    ("corroboration", (0.0, 0.0, 1.0)),
)


def _surface_points(
    star: float,
    metric: geometry.Metric | None = None,
    samples: int = 220,
) -> list[tuple[float, float, float]]:
    """Points on the boundary surface ``|d| = star`` inside the trust subspace.

    The ball of a metric is a quadratic form, so it is an ellipsoid only when the
    metric is diagonal. Rather than assume that, each sampled direction is pushed
    out until the metric's own form reaches ``star``: that also keeps the drawing
    honest when the metric is whitened and the axes are coupled.
    """
    metric = metric or geometry.PRIOR_METRIC
    points: list[tuple[float, float, float]] = []
    step = 3.883222077450933  # golden angle, spreads samples evenly on the sphere
    for index in range(samples):
        z = 1.0 - 2.0 * (index + 0.5) / samples
        radius_xy = max(0.0, 1.0 - z * z) ** 0.5
        theta = step * index
        direction = (radius_xy * math.cos(theta), radius_xy * math.sin(theta), z)
        scale = metric.trust_quadratic(direction)
        if scale <= 1e-12:
            continue
        reach = star / math.sqrt(scale)
        candidate = tuple(component * reach for component in direction)
        if all(-1e-9 <= value <= 1.0 + 1e-9 for value in candidate):
            points.append(candidate)  # type: ignore[arg-type]
    return points


def _map_frame(points: Sequence[Sequence[float]], padding: float = 0.22) -> tuple[float, float, float, float]:
    """Square, isometric bounding box of the plotted points that keeps the origin in view.

    Framing on the data rather than the whole unit cube is what makes the map
    readable: stories cluster near the origin, so a view sized to the full cube
    would spend nearly all of its area on empty space. The frame is kept square
    so the isometric projection stays undistorted.
    """
    projected = [_iso(point) for point in points]
    projected.append((0.0, 0.0))  # the ideal origin, always in frame
    xs = [point[0] for point in projected]
    ys = [point[1] for point in projected]
    span = max(1e-6, max(xs) - min(xs), max(ys) - min(ys)) * (1.0 + padding)
    center_x, center_y = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    return (center_x - span / 2.0, center_y - span / 2.0, span, span)


def _ray_to_frame(direction: tuple[float, float], frame: tuple[float, float, float, float]) -> tuple[float, float]:
    """Clip a ray leaving the origin to the frame, so axis lines never run off it."""
    low_x, low_y, span_x, span_y = frame
    high_x, high_y = low_x + span_x, low_y + span_y
    factors = []
    for component, low, high in ((direction[0], low_x, high_x), (direction[1], low_y, high_y)):
        if abs(component) > 1e-12:
            factors.append(((high if component > 0 else low)) / component)
    factor = min(factors) if factors else 0.0
    return direction[0] * factor, direction[1] * factor


def render_ascii_map(
    stories: Sequence[Story],
    star: float,
    metric: geometry.Metric | None = None,
    width: int = 78,
    height: int = 24,
) -> str:
    """Draw the selection ball in the text report, zoomed on the chosen items.

    Coordinates are ``(age, authority, corroboration)`` deficits projected
    isometrically. 'O' is the ideal origin, '.' the selection surface, and each
    selected item carries the number it has in its topic section. The frame is
    fitted to the selection, because rejected leads sit far enough out that
    including them would compress the interesting structure into a few cells.
    """
    if not stories:
        return ""
    surface = _surface_points(star, metric, samples=90)
    frame = _map_frame([story.trust for story in stories] + surface)
    low_x, low_y, span_x, span_y = frame
    grid = [[" "] * width for _ in range(height)]

    def cell(point: Sequence[float]) -> tuple[int, int] | None:
        x, y = _iso(point)
        column = int((x - low_x) / span_x * (width - 1))
        row = int((1.0 - (y - low_y) / span_y) * (height - 1))
        if 0 <= row < height and 0 <= column < width:
            return row, column
        return None

    for _, direction in AXIS_RAYS:
        for step in range(1, 24):
            position = cell(tuple(component * step / 24.0 for component in direction))
            if position and grid[position[0]][position[1]] == " ":
                grid[position[0]][position[1]] = "-"

    for candidate in surface:
        position = cell(candidate)
        if position and grid[position[0]][position[1]] in " -":
            grid[position[0]][position[1]] = "."

    def free_cell(point: Sequence[float]) -> tuple[int, int] | None:
        """Nearest cell to ``point`` that is not already carrying a marker.

        Many selected items differ only in one axis, so they land in the same
        character cell. Searching outward keeps every marker readable instead of
        letting the last one drawn erase the others.
        """
        start = cell(point)
        if start is None:
            return None
        if grid[start[0]][start[1]] not in "0123456789abcdefghijklmnopqrstuvwxyz+O":
            return start
        for radius_step in range(1, 6):
            for delta_row in range(-radius_step, radius_step + 1):
                for delta_column in range(-radius_step, radius_step + 1):
                    if max(abs(delta_row), abs(delta_column)) != radius_step:
                        continue
                    row, column = start[0] + delta_row, start[1] + delta_column
                    if 0 <= row < height and 0 <= column < width:
                        if grid[row][column] not in "0123456789abcdefghijklmnopqrstuvwxyz+O":
                            return row, column
        return None

    legend = "0123456789abcdefghijklmnopqrstuvwxyz"
    for index, story in enumerate(stories):
        position = free_cell(story.trust)
        if position is None:
            continue
        grid[position[0]][position[1]] = legend[index] if index < len(legend) else "+"

    origin = cell((0.0, 0.0, 0.0))
    if origin:
        grid[origin[0]][origin[1]] = "O"

    lines = ["+" + "-" * width + "+"]
    lines += ["|" + "".join(row) + "|" for row in grid]
    lines.append("+" + "-" * width + "+")
    lines.append(f"O = ideal origin (0,0,0) | '.' marks the selection surface |d|_W = {star:.3f}")
    lines.append("markers 0-9a-z are the selected items in ranking order (0 is closest to the ideal)")
    lines.append("rays from O grow the deficit: age up-right, authority up-left, corroboration down")
    return "\n".join(lines)


def render_radius_histogram(
    stories: Sequence[Story],
    star: float,
    bins: int = 10,
    bar_width: int = 40,
) -> str:
    """Distribution of ``|d|_W`` over everything collected, with the surface marked.

    This is the honest counterweight to a zoomed map: it shows how much of the
    day's material the selection ball actually excluded.
    """
    if not stories:
        return ""
    counts = [0] * bins
    for story in stories:
        counts[min(bins - 1, max(0, int(story.radius * bins)))] += 1
    peak = max(counts) or 1
    surface_bin = min(bins - 1, max(0, int(star * bins)))
    lines = [f"{'|d|_W band':<12}{'items':>6}"]
    for index, count in enumerate(counts):
        low = index / bins
        bar = "#" * int(round(count / peak * bar_width))
        flag = "   <- selection surface" if index == surface_bin else ""
        lines.append(f"{low:.1f}-{low + 1.0 / bins:.1f}{'':<5}{count:>6} {bar}{flag}")
    return "\n".join(lines)


def _inside_frame(point: tuple[float, float], frame: tuple[float, float, float, float]) -> bool:
    low_x, low_y, span_x, span_y = frame
    return low_x <= point[0] <= low_x + span_x and low_y <= point[1] <= low_y + span_y


def _map_frame_and_context(
    stories: Sequence[Story],
    rejected: Sequence[Story],
    star: float,
    metric: geometry.Metric | None = None,
    samples: int = 260,
) -> tuple[tuple[float, float, float, float], list[tuple[float, float, float]], list[Story]]:
    """Frame the map on the selection, keeping only the context that fits inside it.

    Sizing the frame to every collected lead compresses the chosen items into a
    few pixels, so the frame follows the selection and the surface and rejected
    leads are drawn only where they land inside it. The caller reports how many
    did not fit, because a clipped cloud must never read as "nothing else was
    collected".
    """
    surface = _surface_points(star, metric, samples=samples)
    frame = _map_frame([story.trust for story in stories] + surface)
    visible = [story for story in rejected if _inside_frame(_iso(story.trust), frame)]
    return frame, surface, visible


def _render_map_svg(
    stories: Sequence[Story],
    star: float,
    rejected: Sequence[Story] = (),
    labels: dict[int, str] | None = None,
    metric: geometry.Metric | None = None,
    width: int = 560,
    height: int = 560,
) -> str:
    """Inline SVG of the same cube the text map draws, with in-frame context leads."""
    frame, surface, visible = _map_frame_and_context(stories, rejected, star, metric)
    low_x, low_y, span_x, span_y = frame
    pad = 26.0
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)
    offset_x = (width - span_x * scale) / 2.0
    offset_y = (height - span_y * scale) / 2.0

    def to_screen(point: Sequence[float]) -> tuple[float, float]:
        x, y = _iso(point)
        sx = offset_x + (x - low_x) * scale
        sy = offset_y + (1.0 - (y - low_y) / span_y) * span_y * scale
        return sx, sy

    parts: list[str] = ['<g class="cloud">']
    for story in visible[:400]:
        sx, sy = to_screen(story.trust)
        parts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="1.6"/>')
    parts.append("</g>")

    parts.append('<g class="axes">')
    ox, oy = to_screen((0.0, 0.0, 0.0))
    for label, direction in AXIS_RAYS:
        end = _ray_to_frame(_iso(direction), frame)
        ex, ey = to_screen((0.0, 0.0, 0.0))
        ex += end[0] * scale
        ey -= end[1] * scale
        parts.append(f'<line x1="{ox:.1f}" y1="{oy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}"/>')
        parts.append(
            f'<text class="axlabel" x="{ox + end[0] * scale * 0.94:.1f}" '
            f'y="{oy - end[1] * scale * 0.94:.1f}">{_esc(label)}</text>'
        )
    parts.append("</g>")

    parts.append('<g class="surface">')
    for candidate in surface:
        sx, sy = to_screen(candidate)
        parts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="1.9"/>')
    parts.append("</g>")

    for index, story in enumerate(stories):
        sx, sy = to_screen(story.trust)
        # Deterministic nudge so items that agree on all three trust axes stay visible.
        sx += 5.0 * math.cos(index * 2.399)
        sy += 5.0 * math.sin(index * 2.399)
        size = max(3.0, 11.0 * (1.0 - min(1.0, story.trust_radius)))
        color = CATEGORY_COLORS.get(story.category, "#475467")
        parts.append(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{size:.1f}" fill="{color}" fill-opacity="0.8" '
            f'stroke="#0b1220" stroke-width="0.8"><title>{_esc(story.title)} | |d|_W '
            f'{story.radius:.3f}</title></circle>'
        )
        marker = (labels or {}).get(id(story), str(index))
        parts.append(
            f'<text class="marker" x="{sx + size + 1.5:.1f}" y="{sy + 3.5:.1f}">{_esc(marker)}</text>'
        )

    parts.append(f'<circle class="ideal" cx="{ox:.1f}" cy="{oy:.1f}" r="4.5"/>')
    parts.append(f'<text class="idealtext" x="{ox + 7:.1f}" y="{oy - 6:.1f}">ideal (0,0,0)</text>')
    return (
        f'<svg class="map" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Trust cube map of the selected stories with rejected leads in grey">'
        f'{"".join(parts)}</svg>'
    )


def _render_map_section(
    stories: Sequence[Story],
    rejected: Sequence[Story],
    star: float,
    labels: dict[int, str] | None = None,
    metric: geometry.Metric | None = None,
) -> str:
    """The map plus an honest caption about what its frame actually shows."""
    if not stories:
        return '<p>Nothing met the selection bar, so there is no map to draw.</p>'
    _, _, visible = _map_frame_and_context(stories, rejected, star, metric)
    hidden = len(rejected) - len(visible)
    note = (
        f" The frame is fitted to the selection, so {hidden} of {len(rejected)} rejected leads sit outside it."
        if hidden
        else ""
    )
    return (
        '<p class="story-meta">Every item is plotted at its (age, authority, corroboration) deficits, projected '
        f'isometrically. The dotted ellipsoid is the selection surface |d|_W = {star:.3f} and the red ring is the ideal '
        '(0,0,0), so closer is stronger. Grey dots are rejected leads, and each marker is the topic-section label of a '
        f"chosen item (T = technology, G = geopolitics, E = economics).{note}</p>"
        + _render_map_svg(stories, star, rejected, labels, metric)
    )


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _describe_selection(selection: Selection) -> str:
    """Plain-language description of how the brief's items were chosen."""
    if selection.mode == "diverse":
        return (
            f"{selection.feasible} candidates passed the surface test, then a greedy determinantal pick of "
            f"{len(selection.stories)} maximising quality times spread, so one outlet cannot own the brief"
        )
    return f"everything inside the surface, ranked by distance from the origin ({selection.feasible} items)"


def _capped(
    items: Sequence[Story],
    per_source: int,
    limit: int,
) -> tuple[list[Story], int]:
    """Take up to ``limit`` items, allowing at most ``per_source`` from any one outlet.

    A feed that publishes every hour can otherwise own a whole section, which is
    true but useless: the section would be one outlet's afternoon. Overflow is
    counted rather than silently dropped, and the scan continues so a lower-ranked
    lead from a different publisher can take the slot.
    """
    counts: Counter[str] = Counter()
    chosen: list[Story] = []
    displaced = 0
    for story in items:
        if len(chosen) >= limit:
            break
        if counts[story.source] >= per_source:
            displaced += 1
            continue
        counts[story.source] += 1
        chosen.append(story)
    return chosen, displaced


def _displaced_note(displaced: int, per_source: int, total: int) -> str:
    if not displaced:
        return ""
    return (
        f"<p class=\"story-meta\">{displaced} further lead(s) from the same outlets were displaced by "
        f"the per-source cap of {per_source} (of {total} in this section).</p>"
    )


def _render_regions(stories: Sequence[Story], metric: geometry.Metric) -> str:
    rows = []
    for category in CATEGORIES:
        items = [story for story in stories if story.category == category]
        summary = geometry.region_summary([story.axes for story in items], metric)
        if not summary["size"]:
            rows.append(f"<tr><td>{category}</td><td colspan=\"7\">nothing collected</td></tr>")
            continue
        centroid = ", ".join(f"{value:.2f}" for value in summary["centroid"])  # type: ignore[union-attr]
        rows.append(
            f"<tr><td>{category}</td><td>{summary['size']}</td><td>{summary['mean_radius']:.3f}</td>"
            f"<td>{summary['spread']:.3f}</td><td>{_esc(str(summary['weakest']))}</td>"
            f"<td class=\"coords\">{centroid}</td></tr>"
        )
    return (
        "<table class=\"regions\"><thead><tr><th>Region</th><th>Items</th><th>Mean radius</th>"
        "<th>Spread</th><th>Weakest axis</th><th>Barycentre (age, authority, corroboration, relevance, substance)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_html(
    stories: list[Story],
    errors: list[str],
    generated: datetime | None = None,
    per_category: int = 8,
    selection: Selection | None = None,
    stats: Sequence[dict[str, object]] | None = None,
    metric: geometry.Metric | None = None,
    per_source: int = 2,
) -> str:
    """Render the brief, including the geometric selection surface and map."""
    generated = generated or datetime.now(timezone.utc)
    stories = ensure_scored(stories, metric, generated)
    metric = metric or build_metric(stories)
    selection = selection or select_on_manifold(stories, quota=per_category * 3, metric=metric)
    selected_ids = {id(story) for story in selection.stories}
    rejected = [story for story in stories if id(story) not in selected_ids]

    # Everything downstream describes the brief itself, so the summary, the
    # sections and the map always talk about the same set of items.
    brief = selection.stories or stories
    section_items: dict[str, list[Story]] = {}
    displaced_counts: dict[str, int] = {}
    labels: dict[int, str] = {}
    for category in CATEGORIES:
        pool = [item for item in brief if item.category == category]
        items, displaced = _capped(pool, per_source, per_category)
        section_items[category] = items
        displaced_counts[category] = displaced
        for index, story in enumerate(items, 1):
            labels[id(story)] = f"{CATEGORY_LETTERS.get(category, '?')}{index}"

    sections: list[str] = []
    for category in CATEGORIES:
        items = section_items[category]
        cards = []
        for index, story in enumerate(items, 1):
            inside = "inside" if id(story) in selected_ids else "outside"
            neighbours = (
                "<p class=\"near\"><strong>Nearest in the cube:</strong> "
                + _esc("; ".join(story.nearest))
                + "</p>"
                if story.nearest
                else ""
            )
            cards.append(
                f'<article class="story">'
                f'<div class="story-meta">{index:02d} · {_esc(story.source)} · {_esc(story.date_label)} · '
                f'quality {story.score} · |d|_W {story.radius:.3f} · trust |d| {story.trust_radius:.3f} · '
                f'tier {story.source_tier} · {inside} the selection ball</div>'
                f'<h3><a href="{_esc(story.url)}">{_esc(story.title)}</a></h3>'
                f'<p>{_esc(story.summary or "No publisher summary available; open the source for the full report.")}</p>'
                f'<p class="coords">cube coordinates (age, authority, corroboration, relevance, substance): '
                f'{", ".join(f"{value:.2f}" for value in story.axes)}</p>'
                f'<p class="evidence">{_esc(story.evidence_label)}</p>{neighbours}</article>'
            )
        candidates_in_topic = len([item for item in stories if item.category == category])
        sections.append(
            f'<section><h2>{category}</h2>'
            f'<p class="story-meta">{len(items)} brief item(s) in this topic '
            f'({candidates_in_topic} collected).</p>'
            f'{_displaced_note(displaced_counts[category], per_source, len([item for item in brief if item.category == category]))}'
            f'{"".join(cards) or "<p>This topic produced nothing that cleared the surface today.</p>"}</section>'
        )

    top, _ = _capped(brief, per_source, 5)
    executive = "".join(
        f'<li><a href="{_esc(story.url)}">{_esc(story.title)}</a> '
        f'<span class="story-meta">({_esc(story.category)}, {_esc(story.source)}, quality {story.score})</span></li>'
        for story in top
    )
    watch_pool = [story for story in brief if story.category in {"Geopolitics", "Economics"}]
    watch, _ = _capped(watch_pool, per_source, 5)
    problems = "".join(
        f'<li><a href="{_esc(story.url)}">{_esc(story.title)}</a> <span class="story-meta">'
        f'|d|_W {story.radius:.3f}</span></li>'
        for story in watch
    )
    error_block = "".join(f"<li>{_esc(error)}</li>" for error in errors)
    failed = [entry for entry in (stats or []) if entry.get("error")]
    empty = [entry for entry in (stats or []) if not entry.get("error") and not entry["items"]]
    label_parts = []
    if failed:
        label_parts.append(f"{len(failed)} error{'s' if len(failed) != 1 else ''}")
    if empty:
        label_parts.append(f"{len(empty)} empty")
    stats_label = f" ({', '.join(label_parts)})" if label_parts else ""
    stats_block = ""
    if stats:
        rows = "".join(
            f"<tr><td>{_esc(str(entry['name']))}</td><td>{_esc(str(entry['category']))}</td>"
            f"<td>{entry['tier']}</td><td>{entry['items']}</td>"
            f"<td>{_esc(str(entry['error'])) if entry['error'] else ('ok' if entry['items'] else 'empty feed')}</td></tr>"
            for entry in stats
        )
        stats_block = (
            '<details class="stats"><summary>Per-source collection detail'
            f'{stats_label}</summary>'
            '<div class="scroller"><table><thead><tr><th>Source</th><th>Topic</th><th>Tier</th>'
            "<th>Items</th><th>Status</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div></details>"
        )

    chosen = len(selection.stories)
    excluded = max(0, len(stories) - chosen)
    capacity_note = (
        " The radius ceiling was reached, so fewer items met the bar than requested: the evidence is genuinely thin."
        if selection.at_capacity
        else ""
    )
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>Cosmos · {generated.date()}</title><style>
body{{font-family:Arial,sans-serif;max-width:900px;margin:40px auto;color:#172033;line-height:1.5;padding:0 16px}}
h1{{font-size:30px;margin-bottom:4px}}h2{{border-bottom:2px solid #2563eb;padding-bottom:5px;margin-top:34px}}
h3{{margin:5px 0;font-size:18px}}a{{color:#174ea6}}.subtitle,.story-meta,.evidence,.coords{{color:#667085;font-size:12px}}
.story{{border-bottom:1px solid #dfe5ee;padding:14px 0}}.story p{{margin:5px 0}}.evidence{{font-style:italic}}
.coords{{font-family:ui-monospace,Consolas,monospace}}
.notice{{padding:12px;background:#fff6df;border-left:4px solid #e09b19;margin:14px 0}}
.method{{padding:12px;background:#eef4ff;border-left:4px solid #2563eb;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:12px;margin:10px 0}}
th,td{{border:1px solid #dfe5ee;padding:6px 8px;text-align:left;vertical-align:top}}
.scroller{{overflow-x:auto}}
th{{background:#f6f8fb}}details.stats summary{{cursor:pointer;color:#174ea6;font-size:13px}}
svg.map{{width:100%;height:auto;border:1px solid #dfe5ee;background:#fbfcfe;border-radius:6px}}
svg.map .axes line{{stroke:#b6c2d4;stroke-width:1;stroke-dasharray:4 4}}
svg.map .surface circle{{fill:#7c8ba1;fill-opacity:.18}}
svg.map .cloud circle{{fill:#b9c2d0;fill-opacity:.45}}
svg.map .axlabel{{font-size:10px;fill:#667085}}svg.map .marker{{font-size:10px;fill:#334155}}
svg.map .ideal{{fill:none;stroke:#b91c1c;stroke-width:1.6}}svg.map .idealtext{{font-size:11px;fill:#b91c1c}}
.ladder{{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:pre;overflow-x:auto}}
footer{{margin-top:38px;color:#667085;font-size:12px}}
</style></head><body>
<h1>Cosmos</h1>
<p class="subtitle">Generated {generated.strftime("%Y-%m-%d %H:%M UTC")} · ranked leads, preserved evidence, no invented reporting</p>
<div class="method"><strong>Selection surface:</strong> {selection.candidates} of {len(stories)} collected items sit inside the ball
<span class="coords">|d|_W &lt;= {selection.radius:.3f}</span> around the ideal origin (fresh, authoritative, independently
confirmed, on topic, with a usable summary); {excluded} fell outside it. The brief shows {chosen} of them.{capacity_note}
Filling rule: {_describe_selection(selection)}.
Metric: <span class="coords">{_esc(metric.describe())}</span>. An item gets in on where it sits in the cube,
not because it won a leaderboard.</div>
<section><h2>What changed</h2><ul>{executive or '<li>No live stories collected.</li>'}</ul></section>
<section><h2>Problems to watch</h2><ul>{problems or '<li>No geopolitics/economics leads collected.</li>'}</ul></section>
<section><h2>Salience map · trust cube</h2>
{_render_map_section(selection.stories, rejected, selection.radius, labels, metric)}
</section>
<section><h2>Topic regions</h2><div class="scroller">{_render_regions(stories, metric)}</div></section>
{''.join(sections)}
{f'<section><h2>Collection notes</h2><ul>{error_block}</ul>{stats_block}</section>' if errors or stats else ''}
<footer>Personal research brief. Source quality is a ranking aid, not a guarantee. Verify consequential claims with primary documents.</footer>
</body></html>'''


def render_text(
    stories: list[Story],
    errors: list[str],
    generated: datetime | None = None,
    per_category: int = 8,
    selection: Selection | None = None,
    stats: Sequence[dict[str, object]] | None = None,
    metric: geometry.Metric | None = None,
    per_source: int = 2,
) -> str:
    generated = generated or datetime.now(timezone.utc)
    stories = ensure_scored(stories, metric, generated)
    metric = metric or build_metric(stories)
    selection = selection or select_on_manifold(stories, quota=per_category * 3, metric=metric)
    chosen = len(selection.stories)
    lines = [
        "COSMOS",
        f"Generated: {generated.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Headlines are source-linked leads; corroboration and confidence are explicit.",
        "",
        "SELECTION SURFACE",
        "-----------------",
        f"Ball around the ideal origin: |d|_W <= {selection.radius:.3f}",
        f"{selection.candidates} of {len(stories)} collected items qualified; "
        f"{max(0, len(stories) - selection.candidates)} fell outside the surface.",
        f"Brief shows {chosen} of the {selection.candidates} candidates.",
        f"Filling rule: {_describe_selection(selection)}",
        f"Metric: {metric.describe()}",
        "Coordinates are deficits in [0,1]: 0 is ideal, 1 is no evidence at all.",
    ]
    if selection.at_capacity:
        lines.append("Radius ceiling reached: fewer items met the bar than requested.")
    brief = selection.stories or stories
    lines.extend(["", "WHAT CHANGED", "-----------"])
    top, _ = _capped(brief, per_source, 5)
    lines.extend(
        [f"- {story.title} [{story.category}] ({story.source})" for story in top]
        or ["- No live stories collected."]
    )
    lines.extend(["", "PROBLEMS TO WATCH", "-----------------"])
    watch, _ = _capped(
        [story for story in brief if story.category in {"Geopolitics", "Economics"}], per_source, 5
    )
    lines.extend(
        [
            f"- {story.title} (|d|_W {story.radius:.3f})"
            for story in watch
        ]
        or ["- No geopolitics/economics leads collected."]
    )
    ascii_map = render_ascii_map(selection.stories, selection.radius, metric)
    if ascii_map:
        lines.extend(["", "SALIENCE MAP · TRUST CUBE", "=========================", ascii_map])
        histogram = render_radius_histogram(stories, selection.radius)
        if histogram:
            lines.extend(
                [
                    "",
                    "WHERE THE SURFACE CUT THE DAY'S MATERIAL",
                    "-----------------------------------------",
                    histogram,
                ]
            )
    lines.extend(["", "TOPIC REGIONS", "============="])
    for category in CATEGORIES:
        items = [story for story in stories if story.category == category]
        summary = geometry.region_summary([story.axes for story in items], metric)
        if not summary["size"]:
            lines.append(f"{category}: nothing collected")
            continue
        centroid = ", ".join(f"{value:.2f}" for value in summary["centroid"])  # type: ignore[union-attr]
        lines.append(
            f"{category}: {summary['size']} items · mean radius {summary['mean_radius']:.3f} · "
            f"spread {summary['spread']:.3f} · weakest axis {summary['weakest']}"
        )
        lines.append(f"  barycentre (age, authority, corroboration, relevance, substance): {centroid}")
    for category in CATEGORIES:
        lines.extend(["", category.upper(), "=" * len(category)])
        items, displaced = _capped(
            [item for item in brief if item.category == category], per_source, per_category
        )
        if displaced:
            lines.append(
                f"({displaced} further lead(s) displaced by the per-source cap of {per_source})"
            )
        for index, story in enumerate(items, 1):
            lines.extend(
                [
                    f"{index}. {story.title}",
                    f"   Source: {story.source} (tier {story.source_tier}) | {story.date_label} | "
                    f"quality {story.score} | |d|_W {story.radius:.3f}",
                    f"   {story.summary or 'Open source for full report.'}",
                    f"   Cube (age, authority, corroboration, relevance, substance): "
                    f"{', '.join(f'{value:.2f}' for value in story.axes)}",
                    f"   Evidence: {', '.join(story.related_sources) if story.related_sources else 'single-source lead'}",
                ]
            )
            if story.nearest:
                lines.append(f"   Nearest in the cube: {'; '.join(story.nearest)}")
            lines.append(f"   URL: {story.url}")
    if errors:
        lines.extend(["", "COLLECTION NOTES", "-----------------", *[f"- {error}" for error in errors]])
    if stats:
        lines.extend(["", "PER-SOURCE DETAIL", "-----------------"])
        lines.extend(
            f"- {entry['name']} [{entry['category']}, tier {entry['tier']}]: {entry['items']} items"
            + (f" — {entry['error']}" if entry.get("error") else "")
            for entry in stats
        )
    return "\n".join(lines) + "\n"


def write_report(
    stories: list[Story],
    errors: list[str],
    output_dir: str | Path = "reports",
    generated: datetime | None = None,
    per_category: int = 8,
    selection: Selection | None = None,
    stats: Sequence[dict[str, object]] | None = None,
    metric: geometry.Metric | None = None,
    per_source: int = 2,
) -> tuple[Path, Path]:
    generated = generated or datetime.now(timezone.utc)
    metric = metric or build_metric(stories)
    selection = selection or select_on_manifold(stories, quota=per_category * 3, metric=metric)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = generated.strftime("%Y-%m-%d")
    html_path, text_path = directory / f"{stem}.html", directory / f"{stem}.txt"
    html_path.write_text(
        render_html(stories, errors, generated, per_category, selection, stats, metric, per_source),
        encoding="utf-8",
    )
    text_path.write_text(
        render_text(stories, errors, generated, per_category, selection, stats, metric, per_source),
        encoding="utf-8",
    )
    return html_path, text_path


def blocker_histogram(stories: Sequence[Story]) -> list[tuple[str, int]]:
    """Most common reasons items failed to reach the origin: what to go collect next."""
    return Counter(story.blocker for story in stories).most_common()
