"""Geometric evaluation: stories as points that want to sit at the origin.

The ranking model in :mod:`news_hub.core` is a metric model, not a hand-tuned
additive score. Every story is embedded once as a point

    d = (d_age, d_authority, d_corroboration, d_relevance, d_substance) in [0, 1]^5

where each coordinate is a *deficit*: 0 means the axis is fully satisfied and 1
means the evidence for that axis is entirely absent. The ideal briefing item is
therefore the origin ``(0, 0, 0, 0, 0)``, and "how good is this item" becomes
"how far is this point from the origin".

Because the axes are deficit coordinates, the geometry is a genuine box, not a
pair of unrelated scores:

* the cube is flat in the weighted metric ``W = diag(w)`` with ``sum(w) == 1``,
  so the weighted radius ``|d|_W = sqrt(sum(w_i d_i^2))`` lives in ``[0, 1]``.
  A radius of 0 is a perfect item, 1 is an item with no evidence whatsoever.
* a *selection* is a ball ``B(0, r*)`` around the origin. The surface of that
  ball is the decision boundary, so items are not cut off by rank order -- every
  item with ``|d|_W <= r*`` is kept, including ties on the boundary.
* the direction ``u = d / |d|_2`` is the item's *failure profile*. Two items
  with nearby ``u`` are weak for the same reason; the geodesic angle between two
  profiles is a real spherical distance, and the great circle they span is a
  2-D chart through the origin.
* ``(d_age, d_authority, d_corroboration)`` is the trust cube in ``R^3`` whose
  origin is the origin of the full space. It is the literal 3-D picture: points
  that live near ``(0, 0, 0)`` are fresh, authoritative and independently
  confirmed.

Everything here is pure math over numbers. Story text never enters this module,
which is why the properties are cheap to test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

#: Axis order of the evidence cube. Fixed; every vector in this module uses it.
AXES: tuple[str, ...] = ("age", "authority", "corroboration", "relevance", "substance")

#: Relative importance of each axis. Sums to 1 by construction (asserted in tests).
WEIGHTS: dict[str, float] = {
    "age": 0.30,
    "authority": 0.24,
    "corroboration": 0.20,
    "relevance": 0.14,
    "substance": 0.12,
}

#: Long-form axis descriptions, used by the report so the numbers are auditable.
AXIS_NOTES: dict[str, str] = {
    "age": "age of the item against a 48h freshness horizon",
    "authority": "primary institution (tier 1) versus aggregated/discovery feed",
    "corroboration": "how many independent outlets carry the same story",
    "relevance": "how strongly title-weighted text matches topic vocabulary",
    "substance": "whether the publisher supplied a usable summary",
}

#: The literal 3-D subspace: fresh, authoritative, confirmed.
TRUST_AXES: tuple[str, ...] = ("age", "authority", "corroboration")

#: Saturation constants. Each converts a raw measurement into a [0, 1] deficit.
AGE_HORIZON_HOURS = 48.0
CORROBORATION_SATURATION = 3
RELEVANCE_SATURATION = 5.0
SUBSTANCE_SATURATION = 280.0

#: No date is not proof of staleness; treat it as a moderate, visible deficit.
UNKNOWN_AGE_AXIS = 0.70

#: Feeds disagree about timezones, so a few hours in the future is just skew.
FUTURE_GRACE_HOURS = 6.0

#: Authority is a *strength*; the deficit is one minus this.
TIER_STRENGTH: dict[int, float] = {1: 1.0, 2: 0.62, 3: 0.30}
DEFAULT_TIER_STRENGTH = 0.15


def clamp01(value: float) -> float:
    """Clamp to the unit interval; the cube has no outside."""
    if value != value:  # NaN
        return 1.0
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


# --------------------------------------------------------------------------- #
# Axis builders: raw measurement -> deficit in [0, 1]
# --------------------------------------------------------------------------- #
def age_axis(age_hours: float | None) -> float:
    """Staleness, measured as absolute distance from now beyond a small grace.

    A future timestamp is a data problem, not freshness. Event listings and
    preprint feeds routinely carry dates weeks ahead, and clamping those to
    "age zero" would rank a conference announcement for next month as today's
    news. Beyond the grace window a far-future date costs exactly what a
    far-past one costs.
    """
    if age_hours is None:
        return UNKNOWN_AGE_AXIS
    hours = age_hours if age_hours > -FUTURE_GRACE_HOURS else abs(age_hours)
    return clamp01(hours / AGE_HORIZON_HOURS)


def authority_axis(tier: int) -> float:
    return clamp01(1.0 - TIER_STRENGTH.get(tier, DEFAULT_TIER_STRENGTH))


def corroboration_axis(corroborators: int) -> float:
    return clamp01(1.0 - min(1.0, max(0, corroborators) / CORROBORATION_SATURATION))


def relevance_axis(keyword_hits: float) -> float:
    return clamp01(1.0 - min(1.0, max(0.0, keyword_hits) / RELEVANCE_SATURATION))


def substance_axis(description_chars: int) -> float:
    return clamp01(1.0 - min(1.0, max(0, description_chars) / SUBSTANCE_SATURATION))


def axes(
    *,
    age_hours: float | None,
    tier: int,
    corroborators: int = 0,
    keyword_hits: float = 0.0,
    description_chars: int = 0,
) -> tuple[float, ...]:
    """Build the full 5-D deficit vector in ``AXES`` order."""
    return (
        age_axis(age_hours),
        authority_axis(tier),
        corroboration_axis(corroborators),
        relevance_axis(keyword_hits),
        substance_axis(description_chars),
    )


# --------------------------------------------------------------------------- #
# Metric geometry
# --------------------------------------------------------------------------- #
def radius(vector: Sequence[float]) -> float:
    """Prior-metric radius ``|d|_W`` in ``[0, 1]``: distance from the ideal origin."""
    return PRIOR_METRIC.radius(vector)


def distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Prior-metric distance between two points of the cube."""
    return PRIOR_METRIC.distance(left, right)


def euclidean_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(clamp01(value) ** 2 for value in vector))


def profile(vector: Sequence[float]) -> tuple[float, ...] | None:
    """Unit failure profile ``u = d / |d|_2``, or ``None`` at the exact origin."""
    norm = euclidean_norm(vector)
    if norm <= 1e-12:
        return None
    return tuple(clamp01(value) / norm for value in vector)


def profile_angle(left: Sequence[float], right: Sequence[float]) -> float:
    """Geodesic angle (radians) between two failure profiles on the unit sphere.

    This is the spherical distance between directions, so it groups stories that
    are weak for the same reason independently of how weak they are.
    """
    unit_left, unit_right = profile(left), profile(right)
    if unit_left is None or unit_right is None:
        return 0.0
    cosine = sum(a * b for a, b in zip(unit_left, unit_right))
    return math.acos(max(-1.0, min(1.0, cosine)))


def dominant_axis(vector: Sequence[float]) -> tuple[str, float]:
    """Which axis costs the prior-metric radius the most, and its share of the total."""
    return PRIOR_METRIC.blocker(vector)


def trust_vector(vector: Sequence[float]) -> tuple[float, float, float]:
    """Project onto the 3-D trust cube ``(age, authority, corroboration)``."""
    lookup = dict(zip(AXES, vector))
    return tuple(clamp01(lookup[name]) for name in TRUST_AXES)  # type: ignore[return-value]


def trust_radius(vector: Sequence[float]) -> float:
    """Prior-metric radius inside the 3-D trust cube, renormalised to ``[0, 1]``."""
    return PRIOR_METRIC.trust_radius(vector)


# --------------------------------------------------------------------------- #
# Selection: the ball around the origin
# --------------------------------------------------------------------------- #
def ball_selection(
    radii: Sequence[float],
    quota: int,
    max_radius: float | None = None,
) -> tuple[list[int], float]:
    """Pick the smallest ball around the origin that holds ``quota`` items.

    Returns ``(indices, r_star)``. The surface ``|d|_W = r*`` is the boundary, so
    *ties on the boundary are kept*: the chosen set can be larger than ``quota``,
    which is the honest outcome when several items are equally close to the
    origin. Indices come back best-first (smallest radius first).

    ``max_radius`` is the caller's hard ceiling. When the quota cannot be met
    inside it, the returned set is genuinely smaller -- the model reports that
    the evidence is thin instead of padding the brief.
    """
    order = sorted(range(len(radii)), key=lambda index: radii[index])
    if not order:
        return [], 0.0
    quota = max(1, min(int(quota), len(order)))
    star = radii[order[quota - 1]]
    if max_radius is not None:
        star = min(star, float(max_radius))
    tolerance = 1e-12
    chosen = [index for index in order if radii[index] <= star + tolerance]
    return chosen, star


def region_summary(
    points: Iterable[Sequence[float]],
    metric: Metric | None = None,
) -> dict[str, object]:
    """Barycentre and spread of a topic region, plus its worst axis.

    The barycentre is the mean point of the region: the part of the cube where
    that topic's coverage actually sits. High coordinates are the axes the topic
    is weak on, which is the actionable part -- it says what to go collect.
    """
    metric = metric or PRIOR_METRIC
    rows = [tuple(clamp01(value) for value in point) for point in points]
    rows = [row for row in rows if len(row) == len(AXES)]
    if not rows:
        return {"size": 0, "centroid": (), "spread": 0.0, "weakest": "", "mean_radius": 0.0}
    centroid = tuple(sum(row[index] for row in rows) / len(rows) for index in range(len(rows[0])))
    mean_r = sum(metric.radius(row) for row in rows) / len(rows)
    variance = sum((metric.radius(row) - mean_r) ** 2 for row in rows) / len(rows)
    weakest, _ = metric.blocker(centroid)
    return {
        "size": len(rows),
        "centroid": centroid,
        "spread": math.sqrt(variance),
        "weakest": weakest,
        "mean_radius": mean_r,
    }


def nearest_neighbours(
    target: Sequence[float],
    others: Sequence[Sequence[float]],
    count: int = 3,
    metric: Metric | None = None,
) -> list[int]:
    """Indices of the ``count`` closest points to ``target`` (excluding itself)."""
    metric = metric or PRIOR_METRIC
    ranked = sorted(
        (index for index in range(len(others)) if others[index] is not target),
        key=lambda index: metric.distance(target, others[index]),
    )
    return ranked[:count]


# --------------------------------------------------------------------------- #
# 2-D chart: classical MDS
# --------------------------------------------------------------------------- #
def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(row[j] * vector[j] for j in range(len(vector))) for row in matrix]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm <= 1e-12 else [value / norm for value in vector]


def mds(
    points: Sequence[Sequence[float]],
    dims: int = 2,
    iterations: int = 120,
    metric: Metric | None = None,
) -> list[tuple[float, ...]]:
    """Classical (Torgerson) multidimensional scaling, no third-party libraries.

    Double-centres the squared distance matrix and pulls out the top ``dims``
    eigenpairs with power iteration plus deflation. The result is a flat chart
    of the cube that preserves distance as well as two dimensions can, which is
    what the salience map plots. The starting vector is deterministic, so the
    same input always yields the same chart.
    """
    metric = metric or PRIOR_METRIC
    count = len(points)
    if count < 3:
        return [(0.0,) * dims for _ in range(count)]
    squared = [[metric.distance(a, b) ** 2 for b in points] for a in points]
    row_means = [sum(row) / count for row in squared]
    grand = sum(row_means) / count
    centered = [
        [-0.5 * (squared[i][j] - row_means[i] - row_means[j] + grand) for j in range(count)]
        for i in range(count)
    ]
    columns: list[list[float]] = []
    for _ in range(dims):
        vector = _unit([1.0 + (i % 7) * 0.1 + ((i * 13) % 5) * 0.07 for i in range(count)])
        for _ in range(iterations):
            candidate = _unit(_matvec(centered, vector))
            if max(abs(a - b) for a, b in zip(candidate, vector)) <= 1e-12:
                vector = candidate
                break
            vector = candidate
        eigenvalue = sum(a * b for a, b in zip(vector, _matvec(centered, vector)))
        if eigenvalue <= 1e-12:
            columns.append([0.0] * count)
            continue
        columns.append([value * math.sqrt(eigenvalue) for value in vector])
        centered = [
            [centered[i][j] - eigenvalue * vector[i] * vector[j] for j in range(count)]
            for i in range(count)
        ]
    return [tuple(columns[axis][index] for axis in range(dims)) for index in range(count)]


def _corners(dims: int) -> list[tuple[float, ...]]:
    """Every vertex of the unit cube. The metric's normalising constant is their max."""
    return [tuple(float(bit) for bit in format(mask, f"0{dims}b")) for mask in range(1 << dims)]


def _quadratic(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> float:
    return sum(
        vector[i] * matrix[i][j] * vector[j]
        for i in range(len(vector))
        for j in range(len(vector))
    )


def invert_symmetric(matrix: Sequence[Sequence[float]], ridge: float) -> list[list[float]]:
    """Invert ``matrix + ridge * I`` by Gauss-Jordan with partial pivoting.

    The ridge is what makes this safe: it bounds the inverse away from the
    degenerate directions the data happens not to span.
    """
    size = len(matrix)
    work = [
        [float(matrix[i][j]) + (ridge if i == j else 0.0) for j in range(size)]
        + [1.0 if i == j else 0.0 for j in range(size)]
        for i in range(size)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) < 1e-12:
            continue
        work[column], work[pivot] = work[pivot], work[column]
        divisor = work[column][column]
        work[column] = [value / divisor for value in work[column]]
        for row in range(size):
            if row != column and abs(work[row][column]) > 1e-18:
                factor = work[row][column]
                work[row] = [
                    value - factor * pivot_value
                    for value, pivot_value in zip(work[row], work[column])
                ]
    size_rank = size
    return [[work[i][size_rank + j] for j in range(size)] for i in range(size)]


@dataclass(frozen=True)
class Metric:
    """How the cube is measured: the weights, and whether axes are decorrelated.

    A metric carries a quadratic form ``q(d) = d^T M d`` divided by ``scale``, so
    the origin is always the ideal and ``radius`` always lands in ``[0, 1]``.
    ``weights`` is the diagonal of ``M``, which is the whole story for diagonal
    metrics and the reported per-axis loading for whitened ones.
    """

    name: str
    weights: tuple[float, ...]
    matrix: tuple[tuple[float, ...], ...] = ()
    scale: float = 1.0
    trust_scale: float = 1.0
    detail: str = ""

    def form(self, vector: Sequence[float]) -> float:
        """Distance-like form of a *deficit* vector. Coordinates are clamped to the cube."""
        values = [clamp01(value) for value in vector]
        if self.matrix:
            return max(0.0, _quadratic(self.matrix, values))
        return sum(weight * value * value for weight, value in zip(self.weights, values))

    def squared(self, vector: Sequence[float]) -> float:
        return self.form(vector) / self.scale

    def radius(self, vector: Sequence[float]) -> float:
        return math.sqrt(self.squared(vector))

    def distance(self, left: Sequence[float], right: Sequence[float]) -> float:
        """Metric distance between two cube points.

        Clamping would silence the negative half of the difference and make the
        function asymmetric, so a difference vector is measured with signed
        coordinates -- the form is a genuine norm there.
        """
        difference = [float(a) - float(b) for a, b in zip(left, right)]
        if self.matrix:
            squared = max(0.0, _quadratic(self.matrix, difference))
        else:
            squared = sum(weight * value * value for weight, value in zip(self.weights, difference))
        return math.sqrt(squared / (self.scale or 1.0))

    def contributions(self, vector: Sequence[float]) -> tuple[float, ...]:
        """Exact per-axis decomposition of the squared radius: ``sum == squared(d)``.

        Euler's theorem for a homogeneous quadratic gives ``q = sum_i d_i (M d)_i``,
        so nothing here is approximate. With a coupled metric an axis can
        contribute *negatively* -- it correlates with an axis that is already low
        -- which is real information, so the values are not clipped.
        """
        values = [clamp01(value) for value in vector]
        scale = self.scale or 1.0
        if not self.matrix:
            return tuple(weight * value * value / scale for weight, value in zip(self.weights, values))
        gradients = [
            sum(self.matrix[i][j] * values[j] for j in range(len(values))) for i in range(len(values))
        ]
        return tuple(value * gradient / scale for value, gradient in zip(values, gradients))

    def shares(self, vector: Sequence[float]) -> tuple[float, ...]:
        """Contributions normalised by their absolute total, so they can be compared."""
        raw = self.contributions(vector)
        total = sum(abs(value) for value in raw)
        if total <= 1e-12:
            return tuple(0.0 for _ in raw)
        return tuple(value / total for value in raw)

    def blocker(self, vector: Sequence[float]) -> tuple[str, float]:
        """The axis that costs this item the most radius, and its share of the total."""
        shares = self.shares(vector)
        if not shares:
            return AXES[0], 0.0
        index = max(range(len(shares)), key=lambda position: shares[position])
        return AXES[index], shares[index]

    def trust_quadratic(self, cube: Sequence[float]) -> float:
        """The metric restricted to the trust subspace, taking 3-D coordinates.

        Used to draw the selection surface on the map: it is the same form the
        ranking uses, so the dotted boundary in the report really is the boundary
        items were judged against rather than a decorative circle.
        """
        indices = [AXES.index(name) for name in TRUST_AXES]
        values = [clamp01(value) for value in cube]
        if self.matrix:
            return max(
                0.0,
                sum(
                    values[a] * self.matrix[i][j] * values[b]
                    for a, i in enumerate(indices)
                    for b, j in enumerate(indices)
                ),
            )
        return sum(self.weights[i] * values[a] * values[a] for a, i in enumerate(indices))

    def trust_form(self, vector: Sequence[float]) -> float:
        """The same form evaluated on a full 5-D vector."""
        return self.trust_quadratic(trust_vector(vector))

    def trust_radius(self, vector: Sequence[float]) -> float:
        return math.sqrt(self.trust_form(vector) / self.trust_scale)

    def describe(self) -> str:
        weights = ", ".join(f"{name} {weight:.2f}" for name, weight in zip(AXES, self.weights))
        return f"{self.name} weights: {weights}" + (f" · {self.detail}" if self.detail else "")


def _metric_from_matrix(name: str, matrix: list[list[float]], detail: str = "") -> Metric:
    diagonal = [max(0.0, matrix[i][i]) for i in range(len(AXES))]
    total = sum(diagonal) or 1.0
    # Reported weights are normalised diagonal loadings, so modes stay comparable
    # in the report; the matrix itself is used untouched.
    weights = tuple(value / total for value in diagonal)
    scale = max(_quadratic(matrix, corner) for corner in _corners(len(AXES))) or 1.0
    trust_indices = [AXES.index(axis) for axis in TRUST_AXES]
    trust_corners = _corners(len(TRUST_AXES))
    trust_scale = max(
        sum(
            corner[a] * matrix[i][j] * corner[b]
            for a, i in enumerate(trust_indices)
            for b, j in enumerate(trust_indices)
        )
        for corner in trust_corners
    ) or 1.0
    return Metric(
        name=name,
        weights=weights,
        matrix=tuple(tuple(row) for row in matrix),
        scale=scale,
        trust_scale=trust_scale,
        detail=detail,
    )


def prior_metric() -> Metric:
    """The hand-set weights: equal weight per axis regardless of how it spreads."""
    weights = tuple(WEIGHTS[name] for name in AXES)
    total = sum(weights)
    return Metric(
        name="prior",
        weights=tuple(weight / total for weight in weights),
        scale=1.0,
        trust_scale=sum(WEIGHTS[name] for name in TRUST_AXES) / total,
        detail="hand-set weights, ignores how much each axis actually varies",
    )


def axis_spread(points: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """Population standard deviation of each axis over a batch of points."""
    rows = [[clamp01(value) for value in point] for point in points]
    if not rows:
        return tuple(0.0 for _ in AXES)
    deviations = []
    for index in range(len(AXES)):
        column = [row[index] for row in rows]
        mean = sum(column) / len(column)
        deviations.append(math.sqrt(sum((value - mean) ** 2 for value in column) / len(column)))
    return tuple(deviations)


def fit_metric(
    points: Sequence[Sequence[float]],
    prior: Mapping[str, float] = WEIGHTS,
    mode: str = "variance",
    ridge: float = 0.05,
) -> Metric:
    """Fit the metric to the batch being ranked.

    ``prior`` keeps the hand-set weights, which is fine as a *prior* but wrong as
    a final budget: in practice one axis (corroboration) sits at 1.0 for almost
    every lead, so it absorbs a fifth of the weight while moving nothing in the
    ranking. Two fixes are offered, and both keep the origin as the ideal point:

    * ``variance``: ``w_i ~ prior_i * (sigma_i^2 + ridge)``. Weight follows the
      spread an axis actually shows, so an axis that never varies stops eating
      the budget, and one that varies a lot is listened to. ``ridge`` is a
      variance floor that stops a quiet-but-important axis from vanishing, and
      it is what makes a single-item batch safe.
    * ``whitened``: ``M = (E[d d^T] + ridge I)^-1``. This decorrelates coupled
      axes (freshness and authority are strongly anti-correlated in real feeds:
      institutions publish rarely) and reduces to the variance rule along the
      diagonal. Note the deliberate choice of the *uncentered* second moment:
      centering would move the ideal point off the origin and destroy the whole
      premise, so the origin stays the fixed point of the form.

    Both modes renormalise so ``radius`` stays in ``[0, 1]`` on the cube.
    """
    rows = [[clamp01(value) for value in point] for point in points]
    if len(rows) < 2 or mode == "prior":
        return prior_metric()
    spreads = axis_spread(rows)
    spread_text = ", ".join(f"{name} {value:.2f}" for name, value in zip(AXES, spreads))
    if mode == "whitened":
        size = len(AXES)
        second_moment = [
            [sum(row[i] * row[j] for row in rows) / len(rows) for j in range(size)]
            for i in range(size)
        ]
        matrix = invert_symmetric(second_moment, ridge)
        return _metric_from_matrix(
            "whitened", matrix, f"ridge {ridge}; observed sd {spread_text}"
        )
    raw = [prior[name] * (spread**2 + ridge) for name, spread in zip(AXES, spreads)]
    total = sum(raw) or 1.0
    return Metric(
        name="variance",
        weights=tuple(value / total for value in raw),
        scale=1.0,
        trust_scale=sum(value / total for name, value in zip(AXES, raw) if name in TRUST_AXES),
        detail=f"variance-scaled (ridge {ridge}); observed sd {spread_text}",
    )


PRIOR_METRIC = prior_metric()


# --------------------------------------------------------------------------- #
# Set-level selection: quality x diversity
# --------------------------------------------------------------------------- #
def spread_similarity(
    a: Sequence[float],
    b: Sequence[float],
    metric: Metric | None = None,
    sigma: float = 0.15,
) -> float:
    """How close two items sit in the cube: ``1.0`` is the same point, ``0.0`` unrelated.

    This is the ``exp(-|d_i - d_j|^2 / (2 sigma^2))`` factor that decides whether two
    leads occupy the same neighbourhood, and it lives in one place so the diversity
    pick and the report's explanation of that pick can never drift apart.
    """
    metric = metric or PRIOR_METRIC
    spread = max(1e-6, abs(float(sigma)))
    return math.exp(-(metric.distance(a, b) ** 2) / (2.0 * spread * spread))


def diverse_selection(
    points: Sequence[Sequence[float]],
    qualities: Sequence[float],
    quota: int,
    metric: Metric | None = None,
    sigma: float = 0.15,
    jitter: float = 1e-3,
) -> list[int]:
    """Greedy maximum-a-posteriori subset of a determinantal point process.

    A ball is a per-item criterion, so it happily hands back five near-identical
    leads from one feed. This adds the missing set-level term. The kernel is

        L_ij = q_i q_j exp(-|d_i - d_j|^2 / (2 sigma^2))

    so the probability of a subset is ``det(L_S)``: large when the items are
    individually good (``q``) *and* spread out in the cube. Greedy MAP maximizes
    it one item at a time, with the incremental Cholesky update from Chen et al.
    (2018), which keeps each step cheap. ``jitter`` on the diagonal keeps the
    loop well posed when items are identical: diversity then degrades smoothly
    back to quality order instead of stalling.

    Returns the chosen indices in picking order. The first pick is always the
    highest-quality item, so "closest to the origin wins" still holds for the top
    of the brief.
    """
    metric = metric or PRIOR_METRIC
    size = len(points)
    quota = max(0, min(int(quota), size))
    if quota == 0 or size == 0:
        return []
    quality = [max(1e-6, float(value)) for value in qualities]
    kernel = [
        [1.0 + jitter if i == j else spread_similarity(a, b, metric, sigma) for j, b in enumerate(points)]
        for i, a in enumerate(points)
    ]
    likelihood = [
        [quality[i] * quality[j] * kernel[i][j] for j in range(size)] for i in range(size)
    ]
    remaining = [max(0.0, likelihood[i][i]) for i in range(size)]
    residuals: list[list[float]] = [[] for _ in range(size)]
    chosen: list[int] = []
    for _ in range(quota):
        best = max((index for index in range(size) if index not in chosen), key=lambda index: remaining[index], default=None)
        if best is None or remaining[best] <= 1e-12:
            break
        chosen.append(best)
        pivot = math.sqrt(remaining[best])
        baseline = residuals[best]
        for index in range(size):
            if index in chosen:
                continue
            row = residuals[index]
            projection = sum(a * b for a, b in zip(row, baseline))
            coefficient = (likelihood[index][best] - projection) / pivot
            row.append(coefficient)
            remaining[index] = max(0.0, remaining[index] - coefficient * coefficient)
    return chosen


def diversity_drops(
    points: Sequence[Sequence[float]],
    picked: Sequence[int],
    metric: Metric | None = None,
    sigma: float = 0.15,
    floor: float = 0.0,
) -> list[tuple[int, int, float]]:
    """Name what the spread test left behind: ``(index, twin, similarity)`` rows.

    :func:`diverse_selection` returns only what it kept, which leaves the pipeline's
    strongest claim unauditable: a lead that cleared the ball can vanish from the
    brief without the report saying why. This re-derives the reason from the same
    kernel -- for every item the pick did not take, the chosen item it most
    resembles and how close the two are -- so the report can show its work.

    ``floor`` suppresses the near-misses, which were dropped on quality rather than
    redundancy. Rows come back most-similar first, so a caller can show the top few
    and count the rest; ties inside the pick resolve to the earlier, better pick.
    """
    metric = metric or PRIOR_METRIC
    size = len(points)
    kept = [index for index in dict.fromkeys(picked) if 0 <= index < size]
    if not kept:
        return []
    dropped: list[tuple[int, int, float]] = []
    for index in range(size):
        if index in kept:
            continue
        twin = max(kept, key=lambda candidate: spread_similarity(points[index], points[candidate], metric, sigma))
        similarity = spread_similarity(points[index], points[twin], metric, sigma)
        if similarity >= floor:
            dropped.append((index, twin, similarity))
    dropped.sort(key=lambda row: (-row[2], row[0]))
    return dropped


def normalize_coordinates(points: Sequence[Sequence[float]]) -> list[tuple[float, ...]]:
    """Scale a chart into ``[0, 1]^dims`` for plotting, keeping degenerate axes centred."""
    if not points:
        return []
    dims = len(points[0])
    scaled: list[tuple[float, ...]] = []
    for axis in range(dims):
        column = [point[axis] for point in points]
        low, high = min(column), max(column)
        span = high - low
        if span <= 1e-12:
            scaled.append(tuple(0.5 for _ in points))
        else:
            scaled.append(tuple((value - low) / span for value in column))
    return [tuple(column[index] for column in scaled) for index in range(len(points))]
