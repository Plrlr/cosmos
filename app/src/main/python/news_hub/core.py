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
from typing import Callable, Iterable, Sequence

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


def _fresh_age(seconds: float) -> str:
    """Wording for an age under a day: 'just now', '5 minutes ago', '2 hours ago'.

    Shared by story timestamps and the report's Generated line so the two never
    drift apart; callers keep it below 86400 seconds.
    """
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(seconds // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def _relative_label(published: datetime, now: datetime | None = None) -> str:
    """Render ``published`` in the reader's local time with a relative prefix.

    The report is read on the user's own device, so local time is always the
    meaningful one. Age buckets: under a day gets a relative prefix ('2 hours
    ago'), under two days reads 'yesterday, HH:MM', under a week 'N days ago,
    HH:MM', and older items show the absolute local time alone; the year is
    appended only when the item is not from the current one. The optional
    ``now`` keeps the buckets testable without mocking the clock.
    """
    now = now or datetime.now(timezone.utc)
    seconds = (now - published).total_seconds()
    local = published.astimezone()
    if local.year == now.astimezone().year:
        absolute, day = local.strftime("%b %d, %H:%M"), local.strftime("%b %d")
    else:
        absolute, day = local.strftime("%b %d, %Y, %H:%M"), local.strftime("%b %d, %Y")
    if seconds < 86400:
        return f"{_fresh_age(seconds)} · {absolute}"
    if seconds < 172800:
        return f"yesterday, {local:%H:%M} · {day}"
    if seconds < 604800:
        return f"{int(seconds // 86400)} days ago, {local:%H:%M} · {day}"
    return absolute


def _generation_stamp(generated: datetime) -> str:
    """Reader-local header stamp for the report itself: 'Sep 16, 14:32'.

    The relative suffix ('· 2 hours ago') is worth showing only while the brief
    is fresh; after a day the age is the least interesting fact about it.
    """
    now = datetime.now(timezone.utc)
    local = generated.astimezone()
    pattern = "%b %d, %H:%M" if local.year == now.astimezone().year else "%b %d, %Y, %H:%M"
    stamp = local.strftime(pattern)
    age = (now - generated).total_seconds()
    if 0 <= age < 86400:
        stamp += f" · {_fresh_age(age)}"
    return stamp


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
        return _relative_label(self.published)

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


def _map_frame(
    points: Sequence[Sequence[float]],
    padding: float = 0.22,
    project: Callable[[Sequence[float]], tuple[float, float]] = _iso,
) -> tuple[float, float, float, float]:
    """Square, isometric bounding box of the plotted points that keeps the origin in view.

    Framing on the data rather than the whole unit cube is what makes the map
    readable: stories cluster near the origin, so a view sized to the full cube
    would spend nearly all of its area on empty space. The frame is kept square
    so the isometric projection stays undistorted. ``project`` is the projection
    applied to each point before framing, so the SVG map can frame on the
    rotated projection while the ASCII map frames on the plain one.
    """
    projected = [project(point) for point in points]
    projected.append((0.0, 0.0))  # the ideal origin, always in frame
    xs = [point[0] for point in projected]
    ys = [point[1] for point in projected]
    span = max(1e-6, max(xs) - min(xs), max(ys) - min(ys)) * (1.0 + padding)
    center_x, center_y = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    return (center_x - span / 2.0, center_y - span / 2.0, span, span)


#: Padding around the drawn data inside the SVG salience map, in px. Every
#: dot, vector and label lives inside this margin, and the label-extent
#: guard keeps every text box inside it.
MAP_PADDING = 30.0

#: Font size of the map's marker labels, in px.
MAP_LABEL_FONT_SIZE = 11.0

#: Estimated rendered width of one character at :data:`MAP_LABEL_FONT_SIZE`, in px.
MAP_LABEL_CHAR_WIDTH = 6.2


def _xy(point: Sequence[float]) -> tuple[float, float]:
    """Project a trust point onto the salience map's 2-D x-y plane.

    The map plots ``(age, authority)`` deficits only; corroboration is excluded
    from this view.
    """
    return float(point[0]), float(point[1])


def _estimated_text_width(text: str, font_size: float = MAP_LABEL_FONT_SIZE) -> float:
    """Estimated rendered width of ``text`` at ``font_size`` px."""
    return len(text) * MAP_LABEL_CHAR_WIDTH * (font_size / MAP_LABEL_FONT_SIZE)


def _text_box(
    x: float,
    y: float,
    text: str,
    anchor: str = "start",
    font_size: float = MAP_LABEL_FONT_SIZE,
) -> tuple[float, float, float, float]:
    """Estimated ``(left, top, right, bottom)`` screen box of a text element.

    Shared by the renderer and the tests so the label-extent guarantee is
    computed from one definition.
    """
    width = _estimated_text_width(text, font_size)
    if anchor == "middle":
        left, right = x - width / 2.0, x + width / 2.0
    elif anchor == "end":
        left, right = x - width, x
    else:
        left, right = x, x + width
    top = y - font_size
    bottom = y + font_size * 0.2
    return left, top, right, bottom


def _clamp_label(
    x: float,
    y: float,
    text: str,
    anchor: str,
    width: int,
    height: int,
    pad: float = MAP_PADDING,
) -> tuple[float, float, str]:
    """Shift a text anchor so its estimated box stays inside the padded canvas.

    This is the label-extent guard: an anchor point can itself sit in-bounds
    while the glyphs it introduces still spill past the canvas edge, so every
    ``<text>`` element is nudged before it is emitted.
    """
    anchor = anchor or "start"
    left, top, right, bottom = _text_box(x, y, text, anchor)
    min_x, max_x = pad, width - pad
    min_y, max_y = pad, height - pad
    if left < min_x:
        x += min_x - left
    elif right > max_x:
        x -= right - max_x
    if top < min_y:
        y += min_y - top
    elif bottom > max_y:
        y -= bottom - max_y
    return x, y, anchor


def render_ascii_map(
    stories: Sequence[Story],
    star: float,
    metric: geometry.Metric | None = None,
    width: int = 78,
    height: int = 24,
) -> str:
    """Draw the selection ball in the text report, zoomed on the chosen items.

    Coordinates are ``(age, authority, corroboration)`` deficits projected
    isometrically. '.' traces the selection surface, and each selected item
    carries the number it has in its topic section. The frame is
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
        if grid[start[0]][start[1]] not in "0123456789abcdefghijklmnopqrstuvwxyz+":
            return start
        for radius_step in range(1, 6):
            for delta_row in range(-radius_step, radius_step + 1):
                for delta_column in range(-radius_step, radius_step + 1):
                    if max(abs(delta_row), abs(delta_column)) != radius_step:
                        continue
                    row, column = start[0] + delta_row, start[1] + delta_column
                    if 0 <= row < height and 0 <= column < width:
                        if grid[row][column] not in "0123456789abcdefghijklmnopqrstuvwxyz+":
                            return row, column
        return None

    legend = "0123456789abcdefghijklmnopqrstuvwxyz"
    for index, story in enumerate(stories):
        position = free_cell(story.trust)
        if position is None:
            continue
        grid[position[0]][position[1]] = legend[index] if index < len(legend) else "+"

    lines = ["+" + "-" * width + "+"]
    lines += ["|" + "".join(row) + "|" for row in grid]
    lines.append("+" + "-" * width + "+")
    lines.append(f"'.' marks the selection surface |d|_W = {star:.3f}")
    lines.append("markers 0-9a-z are the selected items in ranking order (0 is closest to the ideal)")
    lines.append("axis rays grow the deficit: age up-right, authority up-left, corroboration down")
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


def _map_frame_2d(
    points: Sequence[Sequence[float]],
    padding: float = 0.12,
) -> tuple[float, float, float, float]:
    """Bounding box of 2-D ``(age, authority)`` points with symmetric padding.

    The SVG map frames the whole collected cloud on this box, so the chosen
    items and the rejected context dots share one honest view. The 2-D plane is
    not forced square, so each axis keeps its own span and its own padding.
    """
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(1e-6, max_x - min_x)
    span_y = max(1e-6, max_y - min_y)
    pad_x = max(0.04, span_x * padding)
    pad_y = max(0.04, span_y * padding)
    return (min_x - pad_x, min_y - pad_y, span_x + 2.0 * pad_x, span_y + 2.0 * pad_y)


def _render_map_svg(
    stories: Sequence[Story],
    star: float,
    rejected: Sequence[Story] = (),
    labels: dict[int, str] | None = None,
    metric: geometry.Metric | None = None,
    width: int = 560,
    height: int = 560,
) -> str:
    """Inline SVG of the salience map as a bare 2-D dot field.

    Every collected item — the chosen stories and the rejected leads alike — is
    plotted at ``(age deficit, authority deficit)``; corroboration is left out
    of this view. Authority grows upward (negative SVG y), age grows to the
    right, and the origin (fresh, authoritative) sits at the bottom-left of the
    frame. Each chosen story carries a thin vector from that origin to its dot,
    so a vector's length and direction are the item's deficit magnitude and
    direction. The frame is fitted to the whole data cloud with symmetric
    padding. The map is a pure dot field on a transparent background: no axes,
    ticks, gridlines, or script — the SVG element is the entire output.
    """
    pad = MAP_PADDING
    plot_w = width - 2.0 * pad
    plot_h = height - 2.0 * pad

    # Frame the 2-D plane on the whole collected cloud (selected + context dots).
    cloud = [_xy(story.trust) for story in stories]
    cloud.extend(_xy(story.trust) for story in rejected)
    low_x, low_y, span_x, span_y = _map_frame_2d(cloud)

    def to_screen(point: Sequence[float]) -> tuple[float, float]:
        x, y = _xy(point)
        sx = pad + (x - low_x) / span_x * plot_w
        sy = pad + (1.0 - (y - low_y) / span_y) * plot_h
        return sx, sy

    # The origin (age=0, authority=0) sits below-left of any non-negative
    # deficit, so it usually lands at the plot corner; clamp it into the frame.
    origin_x, origin_y = to_screen((0.0, 0.0))
    origin_x = min(max(pad, origin_x), width - pad)
    origin_y = min(max(pad, origin_y), height - pad)

    def text_element(x: float, y: float, text: str, anchor: str, cls: str, color: str) -> str:
        lx, ly, la = _clamp_label(x, y, text, anchor, width, height, pad)
        return (
            f'<text class="{cls}" x="{lx:.1f}" y="{ly:.1f}" text-anchor="{la}" '
            f'font-size="11" fill="{color}">{_esc(text)}</text>'
        )

    parts: list[str] = []

    # One thin, low-opacity vector per chosen story, from the origin to the
    # same nudged centre as the story's dot.
    positions = []
    for index, story in enumerate(stories):
        sx, sy = to_screen(story.trust)
        # Deterministic nudge so items that agree on both trust axes stay visible.
        sx += 5.0 * math.cos(index * 2.399)
        sy += 5.0 * math.sin(index * 2.399)
        positions.append((sx, sy))
    vectors = [
        f'<line class="vector" x1="{origin_x:.1f}" y1="{origin_y:.1f}" x2="{sx:.1f}" y2="{sy:.1f}"/>'
        for sx, sy in positions
    ]
    parts.append(
        '<g class="vectors" stroke="#8fa2bc" stroke-width="0.6" stroke-opacity="0.45" '
        f'fill="none">{"".join(vectors)}</g>'
    )

    # Grey context dots: every rejected lead, painted behind the story dots.
    cloud_parts = [
        f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="1.6" fill="#b9c2d0" fill-opacity="0.45"/>'
        for sx, sy in (to_screen(story.trust) for story in rejected[:400])
    ]
    parts.append(f'<g class="cloud">{"".join(cloud_parts)}</g>')

    for index, story in enumerate(stories):
        sx, sy = positions[index]
        size = max(3.0, 11.0 * (1.0 - min(1.0, story.trust_radius)))
        color = CATEGORY_COLORS.get(story.category, "#475467")
        parts.append(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{size:.1f}" fill="{color}" fill-opacity="0.8" '
            f'stroke="#0b1220" stroke-width="0.8"><title>{_esc(story.title)} | |d|_W '
            f'{story.radius:.3f}</title></circle>'
        )
        marker = (labels or {}).get(id(story), str(index))
        parts.append(text_element(sx + size + 1.5, sy + 3.5, marker, "start", "marker", "#334155"))

    return (
        f'<svg class="map" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Salience map of age and authority deficits with rejected leads in grey">'
        f'{"".join(parts)}</svg>'
    )


def _render_map_section(
    stories: Sequence[Story],
    rejected: Sequence[Story],
    star: float,
    labels: dict[int, str] | None = None,
    metric: geometry.Metric | None = None,
) -> str:
    """The bare dot-field map plus a factual caption."""
    if not stories:
        return '<p>Nothing met the selection bar, so there is no map to draw.</p>'
    return (
        '<p class="story-meta">Each dot is a collected story, and the grey dots are rejected leads. '
        'Every thin line runs from the origin to a chosen story\'s dot, so a line\'s length and '
        'direction are that story\'s deficit: shorter is stronger. Each marker is the topic-section '
        'label of a chosen item (T = technology, G = geopolitics, E = economics).</p>'
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
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>Cosmos · {generated.astimezone().date()}</title><style>
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
svg.map{{width:100%;height:auto;border:1px solid #dfe5ee;border-radius:6px}}
.ladder{{font-family:ui-monospace,Consolas,monospace;font-size:11px;white-space:pre;overflow-x:auto}}
footer{{margin-top:38px;color:#667085;font-size:12px}}
.back-issues{{margin-top:12px;font-size:12px;color:#667085}}.back-issues a{{color:#667085}}
</style></head><body>
<h1>Cosmos</h1>
<p class="subtitle">Generated {_generation_stamp(generated)} · ranked leads, preserved evidence, no invented reporting</p>
<div class="method"><strong>Selection surface:</strong> {selection.candidates} of {len(stories)} collected items sit inside the ball
<span class="coords">|d|_W &lt;= {selection.radius:.3f}</span> around the origin (fresh, authoritative, independently
confirmed, on topic, with a usable summary); {excluded} fell outside it. The brief shows {chosen} of them.{capacity_note}
Filling rule: {_describe_selection(selection)}.
Metric: <span class="coords">{_esc(metric.describe())}</span>. An item gets in on where it sits in the cube,
not because it won a leaderboard.</div>
<section><h2>What changed</h2><ul>{executive or '<li>No live stories collected.</li>'}</ul></section>
<section><h2>Problems to watch</h2><ul>{problems or '<li>No geopolitics/economics leads collected.</li>'}</ul></section>
<section><h2>Salience map · age × authority</h2>
{_render_map_section(selection.stories, rejected, selection.radius, labels, metric)}
</section>
<section><h2>Topic regions</h2><div class="scroller">{_render_regions(stories, metric)}</div></section>
{''.join(sections)}
{f'<section><h2>Collection notes</h2><ul>{error_block}</ul>{stats_block}</section>' if errors or stats else ''}
<footer>Personal research brief. Source quality is a ranking aid, not a guarantee. Verify consequential claims with primary documents.</footer>
<p class="back-issues"><a href="history.html">Back issues</a></p>
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
        f"Generated: {_generation_stamp(generated)}",
        "",
        "Headlines are source-linked leads; corroboration and confidence are explicit.",
        "",
        "SELECTION SURFACE",
        "-----------------",
        f"Ball around the origin: |d|_W <= {selection.radius:.3f}",
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
    # The file date is the reader's "today", not UTC's: the Android same-day
    # cache compares against the device clock, and so does anyone reading the name.
    stem = generated.astimezone().strftime("%Y-%m-%d")
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
