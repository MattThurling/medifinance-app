"""Unit tests for `crm.stats` — the deal statistics behind the dashboard and
Reports page. No HTTP; every test builds a `StatsParams` with a frozen `now`
so period windows are deterministic."""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from crm import stats
from crm.models import Deal, Stage
from crm.stats import DealStats, StatsParams

from .factories import make_associate, make_deal, make_participation

# A fixed "now" mid-month, so ±N-day offsets never straddle a month boundary
# in ways that make the series assertions fiddly.
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=dt_timezone.utc)


def params(**kw):
    kw.setdefault("now", NOW)
    return StatsParams(**kw)


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def stage(deal, name, days_ago: float) -> Stage:
    return Stage.objects.create(deal=deal, name=name, occurred_at=at(days_ago))


def created(deal, days_ago: float) -> Deal:
    """Backdate creation. `created_at` is auto_now_add, so it has to be an
    UPDATE after the fact. Also moves the seeded Application stage so
    "created" and "application" agree, as they do for real deals."""
    Deal.objects.filter(pk=deal.pk).update(created_at=at(days_ago))
    deal.stage_events.filter(name=Stage.Name.APPLICATION).update(occurred_at=at(days_ago))
    deal.refresh_from_db()
    return deal


def live_deal(*, owner=None, days_ago: float, funded="1000", commission=None, created_days_ago=None, **extra):
    """A deal that went live `days_ago` days ago with one participation."""
    d = make_deal(owner=owner, commission=commission and Decimal(commission), **extra)
    created(d, created_days_ago if created_days_ago is not None else days_ago + 10)
    make_participation(d, amount=funded)
    stage(d, Stage.Name.DEAL_LIVE, days_ago)
    return d


class WindowTests(TestCase):
    def test_presets(self):
        self.assertEqual(params(period="30d").window, (NOW - timedelta(days=30), NOW))
        self.assertEqual(params(period="90d").window, (NOW - timedelta(days=90), NOW))
        start, end = params(period="12m").window
        self.assertEqual(timezone.localtime(start).date().isoformat(), "2025-06-15")
        self.assertEqual(end, NOW)
        start, _ = params(period="ytd").window
        self.assertEqual(timezone.localtime(start).date().isoformat(), "2026-01-01")
        self.assertEqual(params(period="all").window, (None, NOW))

    def test_previous_window_is_adjacent_and_equal_length(self):
        p = params(period="90d")
        start, end = p.window
        pstart, pend = p.previous_window
        self.assertEqual(pend, start)
        self.assertEqual(end - start, pend - pstart)
        self.assertIsNone(params(period="all").previous_window)

    def test_from_request_whitelists(self):
        rf = RequestFactory()
        user = make_associate()

        def build(qs):
            req = rf.get("/reports/", qs)
            req.user = user
            return StatsParams.from_request(req)

        p = build({"period": "30d", "type": "asset_finance", "source": "introducer", "owner": "me"})
        self.assertEqual((p.period, p.type, p.source, p.owner), ("30d", "asset_finance", "introducer", "me"))
        self.assertEqual(p.user_id, user.pk)
        p = build({"period": "bogus", "type": "bogus", "source": "bogus", "owner": "bogus"})
        self.assertEqual((p.period, p.type, p.source, p.owner), (stats.DEFAULT_PERIOD, "", "", ""))
        p = build({"type": "none", "source": "none", "owner": "none"})
        self.assertEqual((p.type, p.source, p.owner), ("none", "none", "none"))
        self.assertEqual(build({"owner": "42"}).owner, "42")

    def test_list_query_drops_empty(self):
        self.assertEqual(params(type="asset_finance").list_query(stage="lost"),
                         {"type": "asset_finance", "stage": "lost"})


class DeltaTests(TestCase):
    def test_delta(self):
        self.assertEqual(stats.delta(10, 5), {"abs": 5, "pct": 100.0})
        self.assertEqual(stats.delta(Decimal("150"), Decimal("200")), {"abs": Decimal("-50"), "pct": -25.0})
        self.assertEqual(stats.delta(3, 0), {"abs": 3, "pct": None})
        self.assertEqual(stats.delta(0, 0), {"abs": 0, "pct": None})
        self.assertEqual(stats.delta(3, None), {"abs": None, "pct": None})


class PipelineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_associate()
        # Backdated so the seeded Application stage predates the frozen NOW.
        cls.app = created(make_deal(owner=cls.owner), 500)  # application only
        cls.live1 = created(make_deal(owner=cls.owner), 500)
        make_participation(cls.live1, amount="1000")
        stage(cls.live1, Stage.Name.DEAL_LIVE, 5)
        cls.live2 = created(make_deal(owner=cls.owner, type=Deal.Type.ASSET_FINANCE), 500)
        make_participation(cls.live2, amount="2500")
        stage(cls.live2, Stage.Name.DEAL_LIVE, 400)  # long ago — period must not matter
        stage(cls.live2, Stage.Name.DEAL_LIVE, 399)  # repeated stage name

    def rows(self, **kw):
        return {b["stage"]: b for b in DealStats(params(**kw)).pipeline()}

    def test_counts_and_funded_by_current_stage(self):
        rows = self.rows()
        self.assertEqual([b["stage"] for b in DealStats(params()).pipeline()], stats.STAGE_ORDER)
        self.assertEqual(rows["application"]["count"], 1)
        self.assertEqual(rows["application"]["funded"], Decimal("0"))
        self.assertEqual(rows["deal_live"]["count"], 2)
        self.assertEqual(rows["deal_live"]["funded"], Decimal("3500"))
        self.assertEqual(rows["lost"]["count"], 0)

    def test_period_is_ignored(self):
        self.assertEqual(self.rows(period="30d")["deal_live"]["count"], 2)

    def test_lost_after_live_sits_in_lost(self):
        stage(self.live1, Stage.Name.LOST, 1)
        rows = self.rows()
        self.assertEqual(rows["deal_live"]["count"], 1)
        self.assertEqual(rows["lost"]["count"], 1)
        self.assertEqual(rows["lost"]["funded"], Decimal("1000"))

    def test_owner_type_and_source_filters(self):
        other = make_associate()
        stray = created(make_deal(owner=other, source=Deal.Source.WEBSITE), 10)
        stage(stray, Stage.Name.DEAL_LIVE, 1)
        self.assertEqual(self.rows()["deal_live"]["count"], 3)
        self.assertEqual(self.rows(owner=str(self.owner.pk))["deal_live"]["count"], 2)
        self.assertEqual(self.rows(owner="me", user_id=other.pk)["deal_live"]["count"], 1)
        self.assertEqual(self.rows(type=Deal.Type.ASSET_FINANCE)["deal_live"]["count"], 1)
        self.assertEqual(self.rows(type="none")["deal_live"]["count"], 2)
        self.assertEqual(self.rows(source=Deal.Source.WEBSITE)["deal_live"]["count"], 1)
        self.assertEqual(self.rows(source="none")["deal_live"]["count"], 2)


class SeriesTests(TestCase):
    def test_buckets_by_first_live_month(self):
        live_deal(days_ago=40, funded="5000", commission="500")   # May 2026
        live_deal(days_ago=3, funded="2000")                      # Jun 2026
        live_deal(days_ago=100, funded="7000")                    # Mar 2026 — previous period for 90d
        never = make_deal()
        make_participation(never, amount="99999")
        s = DealStats(params(period="90d")).series()
        self.assertEqual(s.keys, ["2026-03", "2026-04", "2026-05", "2026-06"])
        self.assertEqual(s.labels[0], "Mar 2026")
        self.assertEqual(s.funded, [Decimal("0"), Decimal("0"), Decimal("5000"), Decimal("2000")])
        self.assertEqual(s.commission, [Decimal("0"), Decimal("0"), Decimal("500"), Decimal("0")])
        self.assertEqual(s.total_funded, Decimal("7000"))
        self.assertEqual(s.total_commission, Decimal("500"))
        self.assertEqual(s.prev_total_funded, Decimal("7000"))
        self.assertEqual(s.prev_total_commission, Decimal("0"))

    def test_twelve_months_is_thirteen_contiguous_keys(self):
        s = DealStats(params(period="12m")).series()
        self.assertEqual(len(s.keys), 13)
        self.assertEqual(s.keys[0], "2025-06")
        self.assertEqual(s.keys[-1], "2026-06")
        # A previous window exists for 12m (it's only "all" that has none), so
        # an empty previous year is a zero total rather than None.
        self.assertEqual(s.prev_total_funded, Decimal("0"))

    def test_all_time_starts_at_earliest_live(self):
        live_deal(days_ago=500, funded="10")  # Jan 2025
        s = DealStats(params(period="all")).series()
        self.assertEqual(s.keys[0], "2025-01")
        self.assertEqual(s.keys[-1], "2026-06")
        self.assertIsNone(s.prev_total_funded)

    def test_empty(self):
        s = DealStats(params(period="30d")).series()
        self.assertEqual(s.keys, ["2026-05", "2026-06"])
        self.assertEqual(s.total_funded, Decimal("0"))


class HeadlineTests(TestCase):
    def test_tiles_and_deltas(self):
        owner = make_associate()
        # Created in window (30d) vs previous window.
        created(make_deal(owner=owner), 5)
        created(make_deal(owner=owner), 10)
        created(make_deal(owner=owner), 45)
        # Live in window vs previous window.
        live_deal(owner=owner, days_ago=2, funded="3000", commission="300", created_days_ago=200)
        live_deal(owner=owner, days_ago=40, funded="1000", commission="100", created_days_ago=200)
        h = DealStats(params(period="30d")).headline()
        self.assertEqual(h["new_deals"]["value"], 2)   # 5 and 10 days ago
        self.assertEqual(h["new_deals"]["previous"], 1)  # 45 days ago
        self.assertEqual(h["new_deals"]["delta_pct"], 100.0)
        self.assertEqual(h["funded"]["value"], Decimal("3000"))
        self.assertEqual(h["funded"]["delta_pct"], 200.0)
        self.assertEqual(h["commission"]["value"], Decimal("300"))
        self.assertEqual(h["went_live"]["value"], 1)
        self.assertEqual(h["live_count"]["value"], 2)
        self.assertIsNone(h["live_count"]["delta_pct"])

    def test_all_time_has_no_deltas(self):
        live_deal(days_ago=2)
        h = DealStats(params(period="all")).headline()
        self.assertEqual(h["funded"]["value"], Decimal("1000"))
        self.assertIsNone(h["funded"]["previous"])
        self.assertIsNone(h["funded"]["delta_pct"])


class ConversionTests(TestCase):
    def test_outcomes_of_created_cohort(self):
        won = created(make_deal(), 20)
        stage(won, Stage.Name.DEAL_LIVE, 10)
        settled = created(make_deal(), 20)
        stage(settled, Stage.Name.SETTLED, 10)
        lost = created(make_deal(), 20)
        stage(lost, Stage.Name.LOST, 10)
        revived = created(make_deal(), 20)      # lost, then went live → won, not lost
        stage(revived, Stage.Name.LOST, 15)
        stage(revived, Stage.Name.DEAL_LIVE, 5)
        dormant = created(make_deal(), 20)
        stage(dormant, Stage.Name.DORMANT, 10)
        created(make_deal(), 20)                # still open
        old = created(make_deal(), 400)         # outside the cohort
        stage(old, Stage.Name.LOST, 1)
        c = DealStats(params(period="90d")).conversion()
        self.assertEqual(c, {
            "total": 6, "won": 3, "lost": 1, "dormant": 1, "open": 1,
            "win_rate": 50.0, "loss_rate": round(100 / 6, 1), "win_rate_of_decided": 75.0,
        })

    def test_empty_cohort(self):
        c = DealStats(params(period="30d")).conversion()
        self.assertEqual(c["total"], 0)
        self.assertIsNone(c["win_rate"])
        self.assertIsNone(c["win_rate_of_decided"])


class VelocityTests(TestCase):
    def legs(self, **kw):
        return {v["label"]: v for v in DealStats(params(**kw)).velocity()}

    def test_days_between_first_occurrences(self):
        d = created(make_deal(), 30)
        stage(d, Stage.Name.PROPOSAL_SUBMITTED, 27)
        stage(d, Stage.Name.PROPOSAL_SUBMITTED, 22)   # repeat — first wins
        stage(d, Stage.Name.DEAL_LIVE, 20)
        d2 = created(make_deal(), 50)
        stage(d2, Stage.Name.DEAL_LIVE, 10)           # 40 days
        legs = self.legs(period="90d")
        live = legs["Application → Deal live"]
        self.assertEqual(live["n"], 2)
        self.assertEqual(live["avg_days"], 25.0)
        self.assertEqual(live["median_days"], 25.0)
        self.assertEqual(legs["Application → Proposal submitted"]["avg_days"], 3.0)
        self.assertEqual(legs["Proposal submitted → Deal live"]["avg_days"], 7.0)
        lost = legs["Application → Lost"]
        self.assertEqual((lost["n"], lost["avg_days"], lost["median_days"]), (0, None, None))

    def test_only_deals_resolved_in_window(self):
        d = created(make_deal(), 400)
        stage(d, Stage.Name.LOST, 300)                # lost outside the window
        d2 = created(make_deal(), 400)
        stage(d2, Stage.Name.LOST, 5)                 # lost inside → 395 days
        legs = self.legs(period="30d")
        self.assertEqual(legs["Application → Lost"]["n"], 1)
        self.assertEqual(legs["Application → Lost"]["avg_days"], 395.0)
        self.assertEqual(self.legs(period="all")["Application → Lost"]["n"], 2)

    def test_empty(self):
        for leg in DealStats(params()).velocity():
            self.assertEqual(leg["n"], 0)
            self.assertIsNone(leg["avg_days"])


class OwnerBreakdownTests(TestCase):
    def test_rows(self):
        a = make_associate(first_name="Ann", last_name="A")
        b = make_associate(first_name="Bob", last_name="B")
        idle = make_associate(first_name="Ida", last_name="I")
        created(make_deal(owner=a), 5)
        created(make_deal(owner=a), 6)
        live_deal(owner=a, days_ago=3, funded="4000", commission="400", created_days_ago=8)
        live_deal(owner=b, days_ago=3, funded="9000", created_days_ago=8)
        unowned = created(make_deal(owner=None), 5)
        unowned.owner = None
        unowned.save()
        rows = {r["name"]: r for r in DealStats(params(period="30d", user_id=b.pk)).by_owner()}
        self.assertEqual(rows["Ann A"]["deals"], 3)
        self.assertEqual(rows["Ann A"]["won"], 1)
        self.assertEqual(rows["Ann A"]["win_rate"], round(100 / 3, 1))
        self.assertEqual(rows["Ann A"]["funded"], Decimal("4000"))
        self.assertEqual(rows["Ann A"]["commission"], Decimal("400"))
        self.assertEqual(rows["Bob B"]["funded"], Decimal("9000"))
        self.assertTrue(rows["Bob B"]["is_me"])
        self.assertFalse(rows["Ann A"]["is_me"])
        self.assertEqual(rows["Ida I"]["deals"], 0)  # zero-deal staff still listed
        self.assertEqual(rows["Unowned"]["deals"], 1)
        self.assertIsNone(rows["Unowned"]["owner_id"])
        ordered = [r["name"] for r in DealStats(params(period="30d")).by_owner()]
        self.assertEqual(ordered[:2], ["Bob B", "Ann A"])   # funded desc
        self.assertEqual(ordered[-1], "Unowned")            # always last

    def test_me_filter_narrows(self):
        a = make_associate()
        b = make_associate()
        live_deal(owner=a, days_ago=3, funded="1")
        live_deal(owner=b, days_ago=3, funded="2")
        rows = DealStats(params(period="30d", owner="me", user_id=a.pk)).by_owner()
        self.assertEqual(sum(r["funded"] for r in rows), Decimal("1"))


class SourceBreakdownTests(TestCase):
    def test_rows(self):
        created(make_deal(source=Deal.Source.INTRODUCER), 5)
        won = created(make_deal(source=Deal.Source.INTRODUCER), 5)
        stage(won, Stage.Name.DEAL_LIVE, 1)
        make_participation(won, amount="1500")
        created(make_deal(source=None), 5)
        created(make_deal(source=""), 5)
        old = live_deal(days_ago=100, funded="8000", source=Deal.Source.WEBSITE)  # outside 30d
        rows = {r["source"]: r for r in DealStats(params(period="30d")).by_source()}
        self.assertEqual([r["source"] for r in DealStats(params()).by_source()],
                         [c for c, _ in Deal.Source.choices] + ["none"])
        intro = rows["introducer"]
        self.assertEqual((intro["deals"], intro["won"], intro["win_rate"]), (2, 1, 50.0))
        self.assertEqual(intro["live"], 1)
        self.assertEqual(intro["funded"], Decimal("1500"))
        self.assertEqual(rows["none"]["deals"], 2)   # None and "" merged
        self.assertEqual(rows["website"]["deals"], 0)
        self.assertEqual(rows["website"]["funded"], Decimal("0"))
        self.assertIsNone(rows["website"]["win_rate"])

    def test_source_filter_narrows_other_stats(self):
        live_deal(days_ago=3, funded="10", source=Deal.Source.SUPPLIER)
        live_deal(days_ago=3, funded="20", source=Deal.Source.REFERRAL)
        h = DealStats(params(period="30d", source=Deal.Source.SUPPLIER)).headline()
        self.assertEqual(h["funded"]["value"], Decimal("10"))
        self.assertEqual(h["live_count"]["value"], 1)


class FacadeTests(TestCase):
    def test_empty_database(self):
        for facade in (stats.dashboard_stats, stats.report_stats):
            data = facade(params())
            self.assertEqual(data["headline"]["new_deals"]["value"], 0)
            self.assertEqual(data["headline"]["funded"]["value"], Decimal("0"))
            self.assertEqual(sum(b["count"] for b in data["pipeline"]), 0)
            json.dumps(data["charts"])  # must already be JSON-safe

    def test_chart_payloads(self):
        live_deal(days_ago=3, funded="1234.50", commission="12", source=Deal.Source.REFERRAL)
        data = stats.report_stats(params(period="30d"))
        pipeline = data["charts"]["pipeline"]
        self.assertEqual(pipeline["labels"], [label for _c, label in Stage.Name.choices])
        self.assertEqual(pipeline["datasets"][0]["format"], "int")
        self.assertEqual(pipeline["datasets"][1]["yAxisID"], "y2")
        series = data["charts"]["series"]
        self.assertEqual(series["datasets"][0]["data"][-1], 1234.5)  # float, not Decimal
        self.assertEqual(data["charts"]["sources"], {
            "labels": ["Referral"], "datasets": [{"label": "Funded", "data": [1234.5], "format": "money"}],
        })
        self.assertEqual(data["charts"]["conversion"]["datasets"][0]["data"], [1, 0, 0, 0])
        json.dumps(data["charts"])

    def test_to_jsonable(self):
        out = stats.to_jsonable({"d": Decimal("1.5"), "t": NOW, "l": (Decimal("2"),), "n": None})
        self.assertEqual(out, {"d": 1.5, "t": NOW.isoformat(), "l": [2.0], "n": None})

    def test_query_count_is_flat(self):
        owners = [make_associate() for _ in range(3)]
        for i in range(30):
            live_deal(owner=owners[i % 3], days_ago=i + 1, funded="100", commission="5",
                      source=Deal.Source.INTRODUCER)
        with CaptureQueriesContext(connection) as ctx:
            stats.report_stats(params(period="90d", user_id=owners[0].pk))
        self.assertLessEqual(len(ctx), 12, [q["sql"][:80] for q in ctx])
