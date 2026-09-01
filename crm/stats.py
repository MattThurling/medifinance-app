"""Deal statistics for the dashboard and the Reports page.

Everything here is a pure function of a :class:`StatsParams` (period, owner,
type, source) — no request objects — so it can be unit-tested directly and
reused by any view.

Design notes
------------
* ``Deal`` has no stage/funded/closed columns. Current stage is the latest
  ``Stage`` event, funded £ is ``Sum(Participation.amount)``, and "went
  live"/"lost" dates are ``Stage.occurred_at`` rows. The subqueries below
  mirror ``crm.views._deal_summaries`` (same ``-pk`` tie-break) so these
  numbers agree with the Deals list.
* Dev/CI runs on SQLite and prod on Postgres, so the ORM is only asked to
  GROUP BY real columns. Whenever the group key or the summed value is a
  Subquery annotation, a flat ``values_list`` is fetched and grouped in
  Python. At this data scale (low thousands of deals) that is a handful of
  small tuples per deal and one query per row set.
* ``Deal.Meta.ordering`` is ``-created_at``; every aggregate queryset calls
  ``.order_by()`` first so Django doesn't add it to the GROUP BY.
* Money is carried as ``Decimal`` so templates can format it exactly;
  :func:`to_jsonable` converts chart payloads for Chart.js.

Date anchors (documented on the Reports page):
* Pipeline — current stage; a snapshot, the period does not apply.
* New deals / conversion / by-owner & by-source deal counts — ``created_at``.
* Funded £ and commission — the deal's first ``deal_live`` event ("recognised
  in the month the deal goes live").
* Velocity — deals whose first ``deal_live`` / ``lost`` falls in the period.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import mean, median

from django.contrib.auth import get_user_model
from django.db.models import OuterRef, Q, Subquery, Sum
from django.utils import timezone

from accounts.models import Role

from .models import Deal, Participation, Stage
from .pricing import add_months

PERIODS = {  # code -> label; declaration order == UI order
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "12m": "Last 12 months",
    "ytd": "Year to date",
    "all": "All time",
}
DEFAULT_PERIOD = "12m"

ZERO = Decimal("0")
STAGE_ORDER = [code for code, _label in Stage.Name.choices]

# (label, from_stage, to_stage) — first occurrence of each per deal.
VELOCITY_LEGS = [
    ("Application → Deal live", Stage.Name.APPLICATION, Stage.Name.DEAL_LIVE),
    ("Application → Lost", Stage.Name.APPLICATION, Stage.Name.LOST),
    ("Application → Proposal submitted", Stage.Name.APPLICATION, Stage.Name.PROPOSAL_SUBMITTED),
    ("Proposal submitted → Deal live", Stage.Name.PROPOSAL_SUBMITTED, Stage.Name.DEAL_LIVE),
]


# --------------------------------------------------------------------------
# Params
# --------------------------------------------------------------------------

def _aware_midnight(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


@dataclass(frozen=True)
class StatsParams:
    """Whitelisted filters. ``owner``/``type``/``source`` use the same
    vocabulary as the Deals list (``me``/``none``/pk, ``none``/code) so report
    rows can deep-link into the filtered list."""

    period: str = DEFAULT_PERIOD
    owner: str = ""      # "", "me", "none", or a user pk as a string
    type: str = ""       # "", "none", or a Deal.Type value
    source: str = ""     # "", "none", or a Deal.Source value
    user_id: int | None = None  # resolves owner == "me"
    now: datetime = field(default_factory=timezone.now)

    @classmethod
    def from_request(cls, request, **overrides) -> "StatsParams":
        g = request.GET
        period = g.get("period", "")
        owner = g.get("owner", "")
        deal_type = g.get("type", "")
        source = g.get("source", "")
        kwargs = dict(
            period=period if period in PERIODS else DEFAULT_PERIOD,
            owner=owner if (owner in ("me", "none") or owner.isdigit()) else "",
            type=deal_type if (deal_type == "none" or deal_type in Deal.Type.values) else "",
            source=source if (source == "none" or source in Deal.Source.values) else "",
            user_id=request.user.pk,
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def period_label(self) -> str:
        return PERIODS[self.period]

    @property
    def window(self) -> tuple[datetime | None, datetime]:
        """``(start, end]`` — ``start`` is ``None`` for "all time"."""
        end = self.now
        today = timezone.localdate(end)
        if self.period == "30d":
            return end - timedelta(days=30), end
        if self.period == "90d":
            return end - timedelta(days=90), end
        if self.period == "12m":
            return _aware_midnight(add_months(today, -12)), end
        if self.period == "ytd":
            return _aware_midnight(date(today.year, 1, 1)), end
        return None, end

    @property
    def previous_window(self) -> tuple[datetime, datetime] | None:
        """The window of equal length immediately before :attr:`window`;
        ``None`` for "all time" (there is no previous period)."""
        start, end = self.window
        if start is None:
            return None
        return start - (end - start), start

    def base_queryset(self):
        """Owner / type / source applied. The period is *not* applied here —
        each stat anchors it to a different date."""
        qs = Deal.objects.all()
        if self.owner == "me":
            qs = qs.filter(owner_id=self.user_id)
        elif self.owner == "none":
            qs = qs.filter(owner__isnull=True)
        elif self.owner:
            qs = qs.filter(owner_id=int(self.owner))
        if self.type == "none":
            qs = qs.filter(Q(type__isnull=True) | Q(type=""))
        elif self.type:
            qs = qs.filter(type=self.type)
        if self.source == "none":
            qs = qs.filter(Q(source__isnull=True) | Q(source=""))
        elif self.source:
            qs = qs.filter(source=self.source)
        return qs.order_by()

    def list_query(self, **extra) -> dict:
        """Query-string values for linking into the Deals list with the same
        owner/type/source filters (plus e.g. ``stage=``)."""
        q = {"owner": self.owner, "type": self.type, "source": self.source}
        q.update(extra)
        return {k: v for k, v in q.items() if v}


# --------------------------------------------------------------------------
# Shared subqueries / helpers
# --------------------------------------------------------------------------

def _latest_stage_name():
    return Subquery(
        Stage.objects.filter(deal=OuterRef("pk"))
        .order_by("-occurred_at", "-pk")
        .values("name")[:1]
    )


def _first_stage_at(name: str):
    return Subquery(
        Stage.objects.filter(deal=OuterRef("pk"), name=name)
        .order_by("occurred_at", "pk")
        .values("occurred_at")[:1]
    )


def _funded_total():
    return Subquery(
        Participation.objects.filter(deal=OuterRef("pk"))
        .values("deal")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )


def _between(field_name: str, start: datetime | None, end: datetime) -> Q:
    q = Q(**{f"{field_name}__lte": end})
    if start is not None:
        q &= Q(**{f"{field_name}__gt": start})
    return q


def _pct(numerator, denominator) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) * 100 / float(denominator), 1)


def delta(current, previous) -> dict:
    """``{"abs": current - previous, "pct": % change}``. ``pct`` is ``None``
    when there is no previous value to compare against (avoids "∞ growth")."""
    if previous is None:
        return {"abs": None, "pct": None}
    diff = current - previous
    return {"abs": diff, "pct": _pct(diff, previous)}


def _month_key(dt: datetime) -> str:
    return timezone.localtime(dt).strftime("%Y-%m")


def _month_label(key: str) -> str:
    y, m = key.split("-")
    return date(int(y), int(m), 1).strftime("%b %Y")


def _month_keys(start: datetime, end: datetime) -> list[str]:
    """Contiguous ``YYYY-MM`` keys from ``start``'s month to ``end``'s month."""
    first = timezone.localtime(start).date().replace(day=1)
    last = timezone.localtime(end).date().replace(day=1)
    keys = []
    cur = first
    while cur <= last:
        keys.append(cur.strftime("%Y-%m"))
        cur = add_months(cur, 1)
    return keys


def to_jsonable(obj):
    """Recursively convert to JSON-safe primitives for ``json_script`` /
    Chart.js: ``Decimal`` → float, dates → ISO strings, dataclasses → dicts."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


# --------------------------------------------------------------------------
# Row sets
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveRow:
    """A deal that first went live inside a window."""
    pk: int
    owner_id: int | None
    source: str | None
    live_at: datetime
    funded: Decimal | None
    commission: Decimal | None


@dataclass(frozen=True)
class CohortRow:
    """A deal created inside a window, with its outcome so far."""
    pk: int
    owner_id: int | None
    source: str | None
    stage: str | None        # current stage
    live_at: datetime | None
    settled_at: datetime | None

    @property
    def won(self) -> bool:
        return self.live_at is not None or self.settled_at is not None


@dataclass
class Series:
    labels: list[str]                 # "Sep 2025", ...
    keys: list[str]                   # "2025-09", ...
    funded: list[Decimal]
    commission: list[Decimal]
    total_funded: Decimal
    total_commission: Decimal
    prev_total_funded: Decimal | None
    prev_total_commission: Decimal | None


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

class DealStats:
    """All the statistics for one :class:`StatsParams`. Row sets are fetched
    once per instance and shared between stats, so a full report is a fixed
    handful of queries regardless of how many sections use them."""

    def __init__(self, params: StatsParams):
        self.p = params
        self._live: dict[tuple, list[LiveRow]] = {}
        self._cohort: dict[tuple, list[CohortRow]] = {}
        self._pipeline_rows: list[tuple] | None = None

    # -- row sets ----------------------------------------------------------

    def live_rows(self, window=None) -> list[LiveRow]:
        window = window or self.p.window
        if window not in self._live:
            start, end = window
            qs = (
                self.p.base_queryset()
                .annotate(live_at=_first_stage_at(Stage.Name.DEAL_LIVE), funded=_funded_total())
                .filter(live_at__isnull=False)
                .filter(_between("live_at", start, end))
                .values_list("pk", "owner_id", "source", "live_at", "funded", "commission")
            )
            self._live[window] = [LiveRow(*row) for row in qs]
        return self._live[window]

    def cohort_rows(self, window=None) -> list[CohortRow]:
        window = window or self.p.window
        if window not in self._cohort:
            start, end = window
            qs = (
                self.p.base_queryset()
                .filter(_between("created_at", start, end))
                .annotate(
                    stage=_latest_stage_name(),
                    live_at=_first_stage_at(Stage.Name.DEAL_LIVE),
                    settled_at=_first_stage_at(Stage.Name.SETTLED),
                )
                .values_list("pk", "owner_id", "source", "stage", "live_at", "settled_at")
            )
            self._cohort[window] = [CohortRow(*row) for row in qs]
        return self._cohort[window]

    def pipeline_rows(self) -> list[tuple]:
        if self._pipeline_rows is None:
            self._pipeline_rows = list(
                self.p.base_queryset()
                .annotate(stage=_latest_stage_name(), funded=_funded_total())
                .values_list("stage", "funded")
            )
        return self._pipeline_rows

    # -- pipeline ----------------------------------------------------------

    def pipeline(self) -> list[dict]:
        """Count + funded £ of deals currently in each stage, in pipeline
        order, zero rows included so chart axes stay stable. Snapshot — the
        period filter does not apply."""
        buckets = {
            code: {"stage": code, "label": Stage.Name(code).label, "count": 0, "funded": ZERO}
            for code in STAGE_ORDER
        }
        for stage, funded in self.pipeline_rows():
            b = buckets.get(stage)  # None only if the seed signal was bypassed
            if b is None:
                continue
            b["count"] += 1
            b["funded"] += funded or ZERO
        return [buckets[c] for c in STAGE_ORDER]

    # -- headline tiles ----------------------------------------------------

    def headline(self) -> dict:
        prev = self.p.previous_window
        cur_cohort = self.cohort_rows()
        cur_live = self.live_rows()
        prev_new = prev_funded = prev_commission = None
        if prev is not None:
            prev_new = (
                self.p.base_queryset().filter(_between("created_at", *prev)).count()
            )
            prev_funded, prev_commission = _live_totals(self.live_rows(prev))
        funded, commission = _live_totals(cur_live)
        live_count = sum(
            1 for stage, _f in self.pipeline_rows() if stage == Stage.Name.DEAL_LIVE
        )
        return {
            "new_deals": _tile(len(cur_cohort), prev_new),
            "went_live": _tile(len(cur_live), len(self.live_rows(prev)) if prev else None),
            "funded": _tile(funded, prev_funded),
            "commission": _tile(commission, prev_commission),
            "live_count": _tile(live_count, None),
        }

    # -- over time ---------------------------------------------------------

    def series(self) -> Series:
        start, end = self.p.window
        rows = self.live_rows()
        if start is None:
            # All time: axis starts at the earliest live deal (or this month).
            start = min((r.live_at for r in rows), default=end)
        keys = _month_keys(start, end)
        funded = {k: ZERO for k in keys}
        commission = {k: ZERO for k in keys}
        for r in rows:
            k = _month_key(r.live_at)
            if k not in funded:  # a live_at exactly at the window edge
                continue
            funded[k] += r.funded or ZERO
            commission[k] += r.commission or ZERO
        prev = self.p.previous_window
        prev_f = prev_c = None
        if prev is not None:
            prev_f, prev_c = _live_totals(self.live_rows(prev))
        total_f, total_c = _live_totals(rows)
        return Series(
            labels=[_month_label(k) for k in keys],
            keys=keys,
            funded=[funded[k] for k in keys],
            commission=[commission[k] for k in keys],
            total_funded=total_f,
            total_commission=total_c,
            prev_total_funded=prev_f,
            prev_total_commission=prev_c,
        )

    # -- by owner ----------------------------------------------------------

    def by_owner(self) -> list[dict]:
        """One row per staff user (plus "Unowned"), sorted by funded £ desc.
        Deal counts are by created date; live/funded/commission by the
        first-live date."""
        User = get_user_model()
        rows: dict[int | None, dict] = {}

        def row(owner_id):
            if owner_id not in rows:
                rows[owner_id] = {
                    "owner_id": owner_id, "name": "Unowned", "deals": 0, "won": 0,
                    "live": 0, "funded": ZERO, "commission": ZERO,
                    "is_me": owner_id is not None and owner_id == self.p.user_id,
                }
            return rows[owner_id]

        staff = User.objects.filter(
            is_active=True, role__in=[Role.ADMIN, Role.ASSOCIATE]
        ).order_by("first_name", "last_name")
        for u in staff:
            row(u.pk)["name"] = u.full_name
        for c in self.cohort_rows():
            r = row(c.owner_id)
            r["deals"] += 1
            r["won"] += c.won
        for lr in self.live_rows():
            r = row(lr.owner_id)
            r["live"] += 1
            r["funded"] += lr.funded or ZERO
            r["commission"] += lr.commission or ZERO
        # Owners who are no longer active staff but still have data in range.
        unnamed = [oid for oid, r in rows.items() if oid is not None and r["name"] == "Unowned"]
        if unnamed:
            for u in User.objects.filter(pk__in=unnamed):
                rows[u.pk]["name"] = u.full_name
        for r in rows.values():
            r["win_rate"] = _pct(r["won"], r["deals"])
        return sorted(
            rows.values(),
            key=lambda r: (r["owner_id"] is None, -r["funded"], -r["deals"], r["name"]),
        )

    # -- by source ---------------------------------------------------------

    def by_source(self) -> list[dict]:
        """One row per ``Deal.Source`` (plus "No source"), declaration order.
        Deals/won are the created-in-period cohort; funded/commission are
        deals that went live in the period."""
        rows = {
            code: {"source": code, "label": label, "deals": 0, "won": 0,
                   "live": 0, "funded": ZERO, "commission": ZERO}
            for code, label in Deal.Source.choices
        }
        rows[None] = {"source": "none", "label": "No source", "deals": 0, "won": 0,
                      "live": 0, "funded": ZERO, "commission": ZERO}

        def key(source):
            return source if source in rows else None

        for c in self.cohort_rows():
            r = rows[key(c.source)]
            r["deals"] += 1
            r["won"] += c.won
        for lr in self.live_rows():
            r = rows[key(lr.source)]
            r["live"] += 1
            r["funded"] += lr.funded or ZERO
            r["commission"] += lr.commission or ZERO
        for r in rows.values():
            r["win_rate"] = _pct(r["won"], r["deals"])
        return list(rows.values())

    # -- conversion --------------------------------------------------------

    def conversion(self) -> dict:
        """Outcomes of the deals *created* in the period. Won = ever reached
        deal_live/settled; lost/dormant = current stage (so a revived deal
        isn't double-counted)."""
        rows = self.cohort_rows()
        total = len(rows)
        won = sum(1 for r in rows if r.won)
        lost = sum(1 for r in rows if not r.won and r.stage == Stage.Name.LOST)
        dormant = sum(1 for r in rows if not r.won and r.stage == Stage.Name.DORMANT)
        decided = won + lost
        return {
            "total": total,
            "won": won,
            "lost": lost,
            "dormant": dormant,
            "open": total - won - lost - dormant,
            "win_rate": _pct(won, total),
            "loss_rate": _pct(lost, total),
            "win_rate_of_decided": _pct(won, decided),
        }

    # -- velocity ----------------------------------------------------------

    def velocity(self) -> list[dict]:
        """Days between the first occurrences of stage pairs, over deals whose
        first deal_live or lost event falls in the period. Computed in Python
        over one ordered scan of the stage log: repeated stage names are
        handled naturally (first wins) and it is one query instead of a
        correlated subquery per leg."""
        start, end = self.p.window
        qs = self.p.base_queryset().annotate(
            live_at=_first_stage_at(Stage.Name.DEAL_LIVE),
            lost_at=_first_stage_at(Stage.Name.LOST),
        )
        qs = qs.filter(_between("live_at", start, end) | _between("lost_at", start, end))
        deal_ids = list(qs.values_list("pk", flat=True))
        first: dict[tuple[int, str], datetime] = {}
        if deal_ids:
            events = (
                Stage.objects.filter(deal_id__in=deal_ids)
                .order_by("deal_id", "occurred_at", "pk")
                .values_list("deal_id", "name", "occurred_at")
            )
            for d, n, t in events:
                first.setdefault((d, n), t)
        out = []
        for label, a, b in VELOCITY_LEGS:
            days = []
            for d in deal_ids:
                ta, tb = first.get((d, a)), first.get((d, b))
                if ta is not None and tb is not None and tb >= ta:
                    days.append((tb - ta).total_seconds() / 86400)
            out.append({
                "label": label,
                "n": len(days),
                "avg_days": round(mean(days), 1) if days else None,
                "median_days": round(median(days), 1) if days else None,
            })
        return out


def _live_totals(rows: list[LiveRow]) -> tuple[Decimal, Decimal]:
    funded = sum((r.funded or ZERO for r in rows), ZERO)
    commission = sum((r.commission or ZERO for r in rows), ZERO)
    return funded, commission


def _tile(value, previous) -> dict:
    d = delta(value, previous)
    return {"value": value, "previous": previous, "delta_abs": d["abs"], "delta_pct": d["pct"]}


# --------------------------------------------------------------------------
# Chart payloads — {"labels": [...], "datasets": [{label, data, format, ...}]}
# --------------------------------------------------------------------------

def pipeline_chart(pipeline: list[dict], *, include_funded: bool = False) -> dict:
    datasets = [{"label": "Deals", "data": [b["count"] for b in pipeline], "format": "int"}]
    if include_funded:
        datasets.append({
            "label": "Funded", "data": [b["funded"] for b in pipeline], "format": "money",
            "type": "line", "yAxisID": "y2",
        })
    return {"labels": [b["label"] for b in pipeline], "datasets": datasets}


def series_chart(series: Series) -> dict:
    return {
        "labels": series.labels,
        "datasets": [
            {"label": "Funded", "data": series.funded, "format": "money"},
            {"label": "Commission", "data": series.commission, "format": "money",
             "type": "line", "yAxisID": "y2"},
        ],
    }


def source_chart(rows: list[dict], *, measure: str = "funded") -> dict:
    fmt = "money" if measure in ("funded", "commission") else "int"
    used = [r for r in rows if r[measure]]
    return {
        "labels": [r["label"] for r in used],
        "datasets": [{"label": measure.title(), "data": [r[measure] for r in used], "format": fmt}],
    }


def conversion_chart(conv: dict) -> dict:
    return {
        "labels": ["Won", "Lost", "Dormant", "Open"],
        "datasets": [{
            "label": "Deals",
            "data": [conv["won"], conv["lost"], conv["dormant"], conv["open"]],
            "format": "int",
            # Semantic slice colours, resolved from the DaisyUI theme by charts.js.
            "themeColors": ["--color-success", "--color-error", "--color-warning", "--color-info"],
        }],
    }


# --------------------------------------------------------------------------
# Facades
# --------------------------------------------------------------------------

def dashboard_stats(params: StatsParams) -> dict:
    """Headline tiles + pipeline + monthly series — what the dashboard shows."""
    s = DealStats(params)
    pipeline = s.pipeline()
    series = s.series()
    return {
        "params": params,
        "headline": s.headline(),
        "pipeline": pipeline,
        "series": series,
        "charts": {
            "pipeline": to_jsonable(pipeline_chart(pipeline)),
            "series": to_jsonable(series_chart(series)),
        },
    }


def report_stats(params: StatsParams) -> dict:
    """Everything, for the Reports page."""
    s = DealStats(params)
    pipeline = s.pipeline()
    series = s.series()
    owners = s.by_owner()
    sources = s.by_source()
    conv = s.conversion()
    return {
        "params": params,
        "headline": s.headline(),
        "pipeline": pipeline,
        "series": series,
        "owners": owners,
        "sources": sources,
        "conversion": conv,
        "velocity": s.velocity(),
        "charts": {
            "pipeline": to_jsonable(pipeline_chart(pipeline, include_funded=True)),
            "series": to_jsonable(series_chart(series)),
            "sources": to_jsonable(source_chart(sources)),
            "conversion": to_jsonable(conversion_chart(conv)),
        },
    }
