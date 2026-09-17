# The mathematics of Cosmos, derived from scratch

Everything the brief does, in order, with each step derived rather than asserted.
Numbers marked **(live)** were measured on a real run against the full source
catalog (669 collected items, 1 dead feed); numbers marked **(check)** were
verified by direct computation.

If you only read one line: **the ranking is a norm, the selection is an order
statistic, the diversification is a determinant, and the daily reweighting is a
second-moment estimate.**

---

## Notation

| symbol | meaning |
|---|---|
| `x_1, x_2, ..., x_n` | the collected articles (live: `n = 669`) |
| `x_i = (x_i1, ..., x_i5)` | article `i` as five measurements: age, authority, corroboration, relevance, substance |
| `x_ij` | one measurement, always a **deficit** in `[0, 1]`: `0` = that axis is fully satisfied, `1` = that evidence is entirely absent |
| `w_1, ..., w_5` | the weights, `sum(w_j) = 1` |
| `q_i`, `r_i` | article `i`'s squared "cost" and its radius (distance from the ideal) |
| `r*` | the selection radius: the boundary of the ball |
| `k(i, j)` | similarity between two articles, in `[0, 1]` |
| `sigma` | the similarity length scale (`0.15` by default) |

**A warning about your sketch, up front.** You wrote it as "x1, x2, x3 represent
the articles, then we apply the weights to get a value of each article". The
weights are not applied to the articles. Each article is *already five numbers*,
and the weights are applied **inside** one article, to its five measurements. So
there are two levels:

```
one article   x_i  -> five measurements -> ONE number r_i     (the weights act here)
the brief     {x_1 ... x_n} -> ranked list -> a chosen subset  (comparison acts here)
```

That is the whole confusion. Step 2 handles the first line, Steps 3-6 the second.

---

## Step 0. The decision everything else follows from

We must choose what the numbers *mean*. We choose: **every measurement is a
deficit.** `x_ij = 0` means axis `j` is fully satisfied; `x_ij = 1` means the
evidence for it is entirely absent.

Two things follow immediately, and they are why this choice is load-bearing:

1. **The ideal article exists and is unique.** It is `0 = (0,0,0,0,0)`. Not "the
   best article of today", not an average - an object whose coordinates each mean
   "fully satisfied". So "is this article good?" becomes "how far is this point
   from `0`?", and ranking becomes a *distance* problem.
2. **Badness is monotone in distance.** Any measure of quality is now a
   decreasing function of the distance from the origin, so we can rank by one
   number, and selection becomes a *ball* around `0`.

If you had stored satisfaction scores `1 - x_ij` instead, the ideal would be
`(1,1,1,1,1)`, the ball would become an intersection of half-spaces, and the
per-axis decomposition in Step 5 would lose its exact form. Deficit coordinates
are what make the rest of this clean.

---

## Step 1. From an article to five numbers

Each `x_ij` comes from a raw measurement by one of five maps.

### 1.1 The shared shape

For an axis whose raw measurement `y >= 0` has a "good enough" point `Y*`:

```
sufficiency  s(y) = min(1, y / Y*)          ramp, then saturate
deficit      d(y) = 1 - s(y) = max(0, 1 - y/Y*)

x_ij = d(y_ij)
```

Why this and not something fancier: we need (a) monotone - more is never worse,
(b) bounded - the cube has no outside, (c) **saturating** - past "enough" there is
no further reward, or one publisher who writes 5,000-character summaries would
own the brief on a single axis. The simplest function with all three, anchored so
that `d(0) = 1` and `d(Y*) = 0`, is exactly the above.

### 1.2 The five axes

| j | axis | raw measurement `y` | `Y*` | `x_ij` |
|---|---|---|---|---|
| 1 | age | hours from now | 48 | `clamp(g(h) / 48)`, see 1.3 |
| 2 | authority | source tier | - | `1 - strength(tier)`, see 1.4 |
| 3 | corroboration | other outlets carrying it | 3 | `1 - min(1, y/3)` |
| 4 | relevance | title-weighted keyword hits | 5 | `1 - min(1, y/5)` |
| 5 | substance | characters of summary | 280 | `1 - min(1, y/280)` |

The `Y*` values are **choices**. They say: 48 hours is the freshness horizon, 3
independent outlets is "confirmed enough", 5 keyword hits is "clearly on topic",
280 characters is "the publisher actually gave me something". Each is defensible
and none is derived from anything.

### 1.3 The age map is not a plain ramp

```
g(h) = h     if h > -6      (a future date within 6 hours is timezone skew)
       |h|   if h <= -6     (beyond that, the future is staleness of the same size)
x_i1 = g(h) / 48            ;   no date at all  ->  0.70
```

Two decisions inside one formula:

- **`|h|`, not `9`.** Event listings and preprint feeds carry dates weeks ahead.
  If a missing or negative `h` were clamped to zero, "conference announcement for
  next month" would be the *freshest possible* item. That is a scoring bug with
  an obvious failure mode, so a far-future date costs exactly what a far-past one
  costs.
- **`0.70` for a missing date.** `1.0` would claim *proof* of staleness; `0.0`
  would claim freshness, which is worse. So we place it between, and above a
  genuinely mediocre item (`0.70 > 0.625` = a 30-hour-old story), so that it is
  visible but not fatal.

`clamp(.)` maps a broken measurement (`NaN`) to `1.0`: a measurement failure must
**hurt** an article, never silently help it.

### 1.4 Authority is a table, not a formula

`strength = {1: 1.00, 2: 0.62, 3: 0.30}`, unknown `0.15`, and `x_i2 = 1 - strength`.

Why not the obvious `1 - (tier-1)/2`, which is also a function on `{1,2,3}`? Because
tier is ordinal, not interval: the gap between a central bank and a trade
publication is not the same size as the gap between two blogs. Equally-spaced
codes would assert that it is. A table asserts each gap explicitly instead. The
consequence is the design: a tier-3 article starts **0.70 away from perfect
before we know anything else about it**.

### 1.5 Result of Step 1

```
x_i = (x_i1, x_i2, x_i3, x_i4, x_i5)  in [0,1]^5
```

**(check)** The ideal is reachable: `age 0h, tier 1, 9 corroborators, 9 keyword
hits, 999 characters -> (0,0,0,0,0)`. A scoring model whose ideal point cannot be
reached has no meaningful zero, so this is worth stating as a property.

---

## Step 2. From five numbers to one number (this is "applying the weights")

We want one badness number per article. We require of it:

1. `q >= 0`, and `q = 0` only for the ideal article;
2. `q(t*x) = t^2 * q(x)` - scaling every deficit by `t` scales badness by `t^2`, so
   `sqrt(q)` is a norm and "closer to the ideal" is well-defined;
3. the cost of one axis does not depend on the other axes.

Euler's theorem for homogeneous functions of degree 2 gives `q(x) = 1/2 x^T H x`
with `H` the Hessian; requirement 3 forces `H` diagonal; requirements 1-2 force
every diagonal entry positive. So

```
q_i = sum over j of  w_j * (x_ij)^2                    (1)
r_i = sqrt(q_i)                                        (2)
```

**(1) is not a style choice: it is the only function satisfying 1-3.** Any other
"apply weights to the article" formula (a plain weighted sum `sum w_j x_ij`, for
instance) breaks at least one of the three.

### 2.1 Why `sum(w_j) = 1`

Since `x_ij <= 1`:

```
q_i = sum_j w_j x_ij^2  <=  sum_j w_j  = 1
```

**(check)** The maximum of the form over all 32 corners of the cube is exactly
`1.0`. So `0 <= r_i <= 1` **for free**, with no normalisation step at scoring time.
Every threshold in the system (`--radius 0.25`, the printed `|d|_W <= 0.456`) is
therefore on a fixed ruler: 0 is the perfect article, 1 is an article with no
evidence on any axis.

### 2.2 What the squaring does - the exact statement

Let `S_i = sum_j w_j x_ij` be the weighted mean deficit of article `i`. Weighted
Cauchy-Schwarz gives

```
q_i = sum_j w_j x_ij^2  >=  (sum_j w_j x_ij)^2 / (sum_j w_j)  =  S_i^2
```

with equality **if and only if all five measurements are equal**.

So the radius is bounded below by the weighted mean, and the gap `q_i - S_i^2`
measures how **unevenly** the failures are spread. Same total damage, concentrated
on fewer axes, costs strictly more.

**(check)** Two axes, equal weights, both with weighted mean `0.30`:

| article | measurement | `q` (squared) | linear `sum w_j x_ij` |
|---|---|---|---|
| A | (0.30, 0.30) | `0.09` | 0.30 |
| B | (0.10, 0.50) | `0.13` | 0.30 |

Linear scoring ties them. Squaring says B is worse, which is the intent: *"a
solid story with no summary and slightly stale"* should beat *"a fresh headline
from nowhere with no text at all"*. **This is the entire reason the exponent is 2,
and it is a theorem, not a preference.** For `n` equal-weight axes the excess is
`(1/n) * sum_j (x_ij - mean)^2` - the variance of the measurement vector.

### 2.3 Why the square root at the end

`sqrt` is monotone, so it changes no ranking at all. It exists so `r_i` sits on
the same `[0,1]` scale as the inputs, which is what makes `r_i <= r*` a *surface*
and lets `|d|` mean what a distance means.

### 2.4 Quality, the display number

```
quality_i = 100 * (1 - r_i)
```

Also monotone, therefore ranking-free. It is a display convenience - and see
Step 7 for the trap in comparing it across days.

---

## Step 3. Comparing articles - and the correction to your sketch

Your sketch said: compute a value per article, "then we compare that with each
other". That is right for **one** of the two comparisons in the system, and it is
not the one you think.

**The ranking comparison is not between articles. It is between each article and
the ideal.** Step 2 already produced each `r_i` without reference to any other
article's measurements. Sorting the `r_i` is not how the numbers are produced; it
is just how they are displayed. Two articles do not influence each other's
quality at all.

```
rank:   sort x_1 ... x_n by r_i ascending          (3)
```

### 3.1 Two exact decompositions from the same form

`q` is homogeneous of degree 2, so Euler's theorem gives `q(x) = sum_j x_j * (dq/dx_j)`.
Defining the per-axis contribution as

```
c_ij = x_ij * (d q / d x_ij)  =  w_j * x_ij^2      (diagonal metric)      (4)
```

gives an **exact** partition, not an approximation:

```
sum_j c_ij = q_i                                    (5)
```

**(check)** Holds to 1e-12 for random points, for both the diagonal and the
coupled metric. From it, the "blocking axis" of an article is
`argmax_j c_ij / sum_j |c_ij|` - which axis is costing it the most radius, and
what share of the total.

### 3.2 The trust projection

Let `P` select `(age, authority, corroboration)`. Then

```
q_trust(x)    = q(P x)
trust_radius  = sqrt( q_trust(x) / trust_scale )
trust_scale   = sum of the weights on those three axes
```

**(check)** `trust_scale = 0.74` for the fixed weights. The renormalisation is the
same trick as 2.1 applied to the sub-space, and it is why an article can honestly
report both `|d|_W = 0.39` and `trust |d| = 0.28`: the second is a properly
rescaled 3-dimensional distance, not the first three terms of the first.

---

## Step 4. The cut: one radius, and where ties are kept

Given the sorted radii `r_(1) <= r_(2) <= ... <= r_(n)` and a quota `k`:

```
r* = r_(k)                                    (6)
keep article i  <=>  r_i <= r*                (7)
```

This is your "we only get the minimum of allowed briefs", and it is here that the
word *minimum* is exactly right.

### 4.1 Why `r*` is the `k`-th smallest

The smallest ball centred on the ideal that contains `k` articles must have radius
equal to the `k`-th smallest radius: any smaller and it contains fewer than `k`
articles, any larger and it is not the smallest. There is no search and no
iteration - it is one order statistic.

### 4.2 Ties are kept, so the brief can be bigger than the quota

**(7)** keeps *everything* at or inside `r*`, which is not the same as "the best
`k`". The two differ exactly when several articles are tied on the boundary.

**(check)** Radii `[0.10, 0.22, 0.22, 0.22, 0.40]`, quota `3`: `r* = 0.22` and
**four** articles are kept, because three are tied at the boundary.

This is your "if there are the same measurement of two articles we add those, it
doesn't cut it off", and it is correct. The justification: cutting a boundary tie
by rank order would mean choosing arbitrarily between articles the model says are
*exactly equally good*. So the brief is allowed to exceed the quota, and the
report says so out loud (`24 of 180 ranked stories sit inside |d|_W <= 0.281;
brief shows 26`).

### 4.3 Where "it could be less" is true, and where it is not

You wrote that if nothing fits the criteria the brief can be less than the
minimum. Careful - by default **it cannot**: since `r*` is *defined* by the quota,
the ball contains the quota by construction. The brief comes out short in exactly
four situations:

| situation | what happens |
|---|---|
| `--radius R` is set and binds | `r* = min(r_(k), R)` can only ever *lower* the line, so the brief is deliberately short and the report sets a flag saying the evidence is genuinely thin. This is the only "nothing fits the criteria" case - the criteria have to exist first. |
| fewer than `k` articles were collected | trivially short. |
| the per-topic cap (`8`) bites | a section shows fewer than 8 even with qualifying leads inside the ball; overflow is counted as "displaced". |
| the diversity pick (Step 5) | picks exactly `k` from the larger candidate pool, so this stage can only trim, never extend. |

So: `8` is **not** the minimum brief size. `8` is the **per-topic** cap
(`--per-topic 8`), and there are three topics, so `3 x 8 = 24` is exactly the
default total quota (`--quota 24`). That is the number you were remembering.

---

## Step 5. Comparing articles *with each other*: the diversity term

This is where articles finally interact - your "compare that with each other" -
and it exists because a ball is a **per-item test**. It has no way to say "these
two together are redundant": five near-identical points each pass `r_i <= r*`
individually. We need an objective defined on *subsets*.

### 5.1 The kernel

```
k(i, j) = exp( -|x_i - x_j|^2 / (2 * sigma^2) )      (8)
```

`k = 1` means the same point in the cube; `k = 0` means unrelated. Here
`|x_i - x_j|` uses the same weighted metric as Step 2, applied to a *difference*
vector instead of to a deficit vector.

Why this is a legal kernel at all: `exp(-t^2 / 2 sigma^2)` is the characteristic
function of a Gaussian, and characteristic functions are positive definite, so
the matrix `K` with entries `k(i,j)` is positive semi-definite. The selection
below inherits that property rather than assuming it.

### 5.2 The objective: a determinant is a volume

```
L_ij = quality_i * quality_j * k(i, j)               (9)
P(S) proportional to det(L_S)                        (10)
```

For any chosen subset `S`, `det(L_S)` is (a constant times) the **squared volume
of the parallelepiped spanned by the rows of `K` for `S`**. If two rows are
identical, the volume is zero. So:

```
two identical articles together have probability zero
  - not because either is bad, but because the second adds no volume.
```

**(check)** For a pair, exactly:

```
det(L_2) = q_1^2 * q_2^2 * (1 - k_12^2)   ->   both sides = 0.09680641
```

Three useful readings of (9)-(10):

- `quality^2`, not `quality`: squaring again, so a mediocre article gets a
  quarter of the influence of a good one, not half.
- `k` multiplies *off-diagonal* entries only. With `sigma -> 0`, `k -> 0` for any
  distinct pair and the objective degenerates to "take the best `k` by quality" -
  i.e. the diversity term switches off continuously.
- `det` is maximised by sets that are individually good **and** spread out.

### 5.3 Why the code is not doing a search

Maximising `det` exactly is NP-hard. The code runs a **greedy** pass, which
carries the standard `(1 - 1/e)` guarantee for monotone submodular objectives.
That is worth knowing: this stage is an approximation, unlike Steps 1-4.

The greedy step is made cheap by keeping, for every unchosen article, its
"remaining value", which is exactly the ratio of determinants:

```
remaining_i  =  det(L_{S + i}) / det(L_S)  =  L_ii - ||c_i||^2             (11)
pick the unchosen i with the largest remaining_i
```

and `c_i` is the Cholesky residual against everything already picked. Read (11)
plainly: **an article's value is its own quality minus the part of it that is
already spanned by what has been chosen.** **(check)** Against brute-force
determinants, the coded `remaining_i` matched the true determinant ratio at every
step: `0.810810`, `0.490403`, `0.360226`.

**(check)** The bandwidth, calibrated exactly: `k = 1/2` at
`|x_i - x_j| = sigma * sqrt(2 ln 2) = 1.177 * sigma`. With `sigma = 0.15`:
half-similarity at distance `0.177`, `k(0.2) = 0.41`, `k(0.4) = 0.029`,
`k(1.0) = 2e-10`. So the neighbourhood the rule treats as "redundant" is roughly
`+/- 0.18` wide in cube units.

### 5.4 The candidate pool, and why there is one

If the ball held exactly `k` articles, "pick `k` of `k`" is no choice at all and
the whole stage would be a no-op. So the ball is first filled to
`candidates = k * pool` (live: `24 * 3 = 72`), and the greedy pass picks `k` of
those 72. Diversity needs slack in order to have anything to choose.

**(live)** 72 candidates, 24 chosen, `r* = 0.456`, 48 set aside.

### 5.5 The honest result from that live run

The set-aside articles were measured two ways: cube similarity `k` (what the rule
uses) and title-word overlap (what a reader can check). Result:

| | |
|---|---|
| candidates set aside | 48 of 72 |
| cube similarity to the nearest chosen article | max `1.00`, min `0.80` |
| title-word overlap | max `0.12`, **median `0.00`** |
| rows that were the same story in wording | **0** |

So this stage is **not** removing reworded headlines - `deduplicate` (a
title-token Jaccard rule at `0.52`) does that before Step 1 even runs. What the
determinant removes is articles with the same *evidence profile*: two
uncorroborated tier-2 preprints of the same age with the same missing summary are
interchangeable to `k`, however different their subjects. Content enters the cube
only through `x_i4` (relevance), which carries about 0.19 of the weight in a live
fit.

That is not a bug in the formula. It is `sigma` being small relative to how dense
the candidate pool is: the pool's diameter is at most `2 * 0.456 = 0.912`, while
`sigma = 0.15` puts half-similarity at `0.177`. The pick is therefore clustering a
small, dense region, not filtering fine-grained duplication. The report now names
everything it set aside, so this is visible on every run instead of hidden.

---

## Step 6. The hard caps, which are constraints and not geometry

After Step 5, the brief is trimmed per topic:

```
at most `--per-topic` (8) articles per topic
at most `--per-source` (2) from any one outlet
```

The scan keeps going, so a lower-ranked lead from a different outlet takes the
slot; the count of articles bumped this way is reported as "displaced". Note what
this means: the final brief is `caps(diversity_pick(ball(...)))`. The caps can
shrink or reorder, never add. They are the crude safety net for a feed that
publishes hourly.

---

## Step 7. Where the weights come from each day

Steps 1-4 used fixed weights `w_j` = 0.30, 0.24, 0.20, 0.14, 0.12 for age,
authority, corroboration, relevance, substance. A fixed budget is fine as a
*prior* but wrong as a final answer: on real feeds one axis can sit at the same
value for almost every article, in which case it absorbs weight while moving
nothing. So the weights are refit on every run.

### 7.1 `variance` mode (the default)

```
s_j = population standard deviation of axis j over the batch
w_j proportional to  prior_j * (s_j^2 + ridge)        (12)
renormalise so sum_j w_j = 1
```

Reading (12): `s_j^2` is the axis's **discriminating power** - an axis that never
varies ranks nothing. `prior_j` is the value judgement. `ridge` (`0.05`) is a
variance floor: it says "an axis with no observed spread is probably quiet by
accident, not by nature", so it stops a quiet-but-important axis from vanishing
entirely. Without it, `corroboration` on a dead-constant batch would get weight
exactly 0 and disappear from the report forever.

**(live)** 669 articles:

| axis | prior | observed sd | variance | fitted weight | vs prior | ridge/variance |
|---|---|---|---|---|---|---|
| age | 0.300 | 0.394 | 0.155 | **0.423** | 1.41x | 0.32 |
| authority | 0.240 | 0.235 | 0.055 | **0.174** | 0.73x | 0.90 |
| corroboration | 0.200 | 0.051 | 0.0026 | **0.072** | 0.36x | **19.0** |
| relevance | 0.140 | 0.381 | 0.145 | **0.188** | 1.34x | 0.35 |
| substance | 0.120 | 0.349 | 0.122 | **0.142** | 1.18x | 0.41 |

Two things to take from that table. First, on your feed **age (0.423) outweighs
authority (0.174) by 2.4x**, although the prior said 0.30 against 0.24 - because
your catalog is mostly institutional, so recency varies far more than tier does.
Second, **corroboration's observed variance is 19x smaller than the ridge**, so
its weight collapses to just above the floor. That is the model discovering that
your sources rarely corroborate each other inside one run.

### 7.2 `whitened` mode (available, not default)

```
V = average of x_i x_i^T over the batch            (uncentered, deliberately)
M = (V + ridge I)^-1
q_i = x_i^T M x_i      ;   r_i = sqrt(q_i / scale)
scale = max of the form over the 32 cube corners   (so r_i <= 1 on the cube)
```

The identity that explains it:

```
average of q over the batch = trace( (V + ridge I)^-1 V ) = sum_l  L_l / (L_l + ridge)
```

where `L_l` are the eigenvalues of `V`. With `ridge = 0` this is exactly the
number of axes. **(check)** `trace = 5.000000` at `ridge = 0`, and `3.505614` at
`ridge = 0.05`. So uncentered whitening means: *"make the batch's own average
squared radius equal to the dimension count"* - radius 1 stops meaning "a corner
of the cube" and starts meaning "average badness for this feed set".

### 7.3 Why the moment is **uncentered** - your most thoughtful line of code

Ordinary whitening is `(x - mean)^T * covariance^-1 * (x - mean)`. Its minimiser is
the mean, which would mean: **the ideal article becomes the average of today's
material.** That contradicts Step 0. The cleanest proof is at the perfect article:

```
centered form:  radius at x = 0  is  sqrt( mean^T covariance^-1 mean )  >  0
```

so a story that is fresh, tier-1, corroborated, on-topic and fully summarised
would carry **nonzero** radius, and "0 on every axis" would stop meaning anything.
Using the uncentered moment keeps `q(0) = 0`, so the origin stays the fixed point
and Step 0 survives. This is correct, and it is the reason the code comments make
a point of it.

### 7.4 What uncentered coherence costs

The matrix being inverted is a second moment, not a covariance, so its
off-diagonals are dominated by the fact that all measurements are positive.
**(live)** centered versus uncentered:

| pair | uncentered | centered |
|---|---|---|
| age / authority | +0.461 | **-0.669** |
| age / corroboration | +0.821 | +0.052 |
| corroboration / relevance | +0.855 | +0.013 |
| relevance / substance | +0.769 | +0.305 |

So the docstring's motivating example is true *in the centered sense* - freshness
and authority really are strongly anti-correlated on your feeds (-0.669), exactly
as claimed, because institutions publish rarely. But **that is not what the code
inverts.** The uncentered moment reads that pair as `+0.461`, and inverting turns
large positive off-diagonals into large negative ones: **(check)** the live `M`
has `authority/corroboration = -4.36` and `age/corroboration = -3.52`, against
positive diagonal entries of 5.1 to 10.2.

A negative `M_ij` means the cross-term subtracts cost, so the form actively
*rewards* being simultaneously bad on two axes that often co-occur in your
corpus. `M` is still positive definite, so `|d|_M` is still a norm, but "each axis
is a cost" no longer holds and per-axis contributions can go negative. The default
mode is `variance`, which is diagonal and has no cross terms, so your shipped
brief is unaffected. This matters only if you switch to `--metric whitened`.

### 7.5 The trap in `quality`

Because the metric is refit every run, and `whitened` also rescales by a cube
corner the batch never approaches, **`quality` is relative to the day and to the
mode**. **(live)** the same best article:

```
prior weights:      radius 0.377  ->  quality 62
whitened:           radius 0.240  ->  quality 76
```

Only the *rank* is invariant. `quality 62` today is not `quality 62` tomorrow, and
`--radius` means something different in each mode. Compare reports only with the
same `--metric` and `--ridge`.

---

## Step 8. The map

The chart is classical (Torgerson) multidimensional scaling. With `D` the matrix
of metric distances and `J = I - 11^T/n`:

```
B = -1/2 * J * D^2 * J          (13)
```

`B` equals `X X^T` whenever `D` really is a Euclidean distance matrix, so the top
`dims` eigenpairs of `B` (power iteration with deflation, deterministic start)
give the best `dims`-dimensional embedding of `D`. It is a **projection**:
distances in the chart are approximations, and two dots that look close need not
be close.

---

## Step 9. Your sketch, line by line

| you wrote | what the code does |
|---|---|
| "x1, x2, x3 ... represent the articles" | yes, but each `x_i` is five numbers, not one |
| "we apply the weights that was set, to get a value of each article" | the weights act **inside** one article (equation 1). Nothing about any other article is involved |
| "then we compare that with eachother" | the ranking compares each article to the **ideal** (Step 3). The article-to-article comparison is Step 5, the determinant |
| "then we scrutinize again to only get the minimum of allowed briefs which i believe is 8" | the cut is the `k`-th smallest radius (equation 6). `8` is the **per-topic** cap; `3 topics x 8 = 24` is the default total quota |
| "if there is none that fits the criteria it could be less" | not by default - `r*` is *defined* by the quota, so the ball contains it by construction. It comes out short only if you set `--radius` (a real ceiling), if fewer than `k` items were collected, if the per-topic cap bites, or via the Step 5 pick |
| "and also more if there are the same measurement of two articles we add those, it doesnt cut it off" | exactly right - Step 4.2. Boundary ties are kept, so the brief can exceed the quota |

---

## Step 10. The knobs

| flag | default | what it actually is |
|---|---|---|
| `--quota` | 24 | `k` in equation (6): how many articles the ball must hold |
| `--radius` | none | hard ceiling on `r*`; the only thing that can make the brief genuinely short |
| `--pool` | 3 | candidate pool = `quota * pool` (72); diversity needs slack |
| `--sigma` | 0.15 | kernel width in (8); half-similarity at distance `0.177` |
| `--per-topic` | 8 | hard cap per topic, applied after the pick |
| `--per-source` | 2 | hard cap per outlet per topic |
| `--metric` | `variance` | how `w_j` is derived: `prior`, `variance`, or `whitened` |
| `--ridge` | 0.05 | variance floor in (12); in `whitened` mode it also lowers the average radius |
| `--selection` | `diverse` | `ball` switches Step 5 off and fills the ball by radius alone |

---

## Step 11. Theorem, choice, or heuristic

| statement | status |
|---|---|
| deficit coordinates put the ideal at the origin | **definitional choice** (load-bearing) |
| `q = sum w_j x_j^2` is the unique separable, homogeneous, positive form | theorem |
| `sum(w) = 1` gives `r <= 1` on the cube | theorem (check) |
| squaring punishes uneven failure, `q >= S^2`, equality iff equal | theorem (weighted Cauchy-Schwarz, check) |
| `sum` of per-axis contributions equals `q` exactly | theorem (Euler, check) |
| uncentered whitening makes the batch mean of `q` equal the dimension count | theorem (check: 5.000000) |
| centering would put the ideal at the batch mean | theorem (check) |
| `r*` is the `k`-th order statistic; boundary ties are kept | theorem, given the rule |
| `det(L_2) = q_1^2 q_2^2 (1 - k_12^2)`; `det` is a squared volume | theorem (check) |
| `remaining_i` equals the exact determinant ratio | theorem (check) |
| greedy is within `(1 - 1/e)` of the best subset | standard bound on a submodular objective |
| 48h, 6h grace, 3 outlets, 5 hits, 280 chars | **choices** |
| the authority table's values | **choices** |
| `0.70` for a missing date | **choice** |
| `ridge = 0.05`, `sigma = 0.15`, `pool = 3`, `quota = 24`, caps 8/2 | **choices** |
| default metric = `variance` | **choice** (the safe one) |

---

## Step 12. Where the mathematics does not hold yet

1. **`sigma` is mis-scaled against the candidate pool.** Pool diameter `<= 0.912`
   against a half-similarity radius of `0.177`. Measured effect: 48 of 72
   candidates read as redundant while sharing no wording. Either `sigma` wants to
   be smaller, or the cube is the wrong space for content-level redundancy.
2. **The kernel measures the evidence cube, not the subject.** Content enters only
   through the relevance axis (`~0.19` of the weight, and it is a keyword count).
3. **Nothing is evaluated.** All 137 tests are algebraic invariants: they prove
   the maths is self-consistent, not that the brief is better than a simple
   recency-plus-source-count rule. There is no baseline to race.
4. **Quality is relative to the day and the mode** (Step 7.5), and the report does
   not say so.
5. **Small artifacts.** The dotted ring on the salience map is drawn by pushing
   directions until the *trust form* reaches `r*`, which corresponds to
   `trust_radius = r* / sqrt(0.74) = 1.16 * r*` - a 16% mismatch against the
   `trust |d|` the rows print - and directions that leave the unit cube are
   omitted rather than clamped, so the ring can be open. Also `Metric.distance`
   uses signed differences while `form` clamps to the cube, so "distance between
   two points" and "difference of two radii" are not the same function.
