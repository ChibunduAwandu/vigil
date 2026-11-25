"""Each control must catch the abuse it is there for, and nothing else."""
from __future__ import annotations

import csv
import dataclasses
from collections import Counter, deque

import pytest
from exchange_kit import ETF, FAK, FUTURE, SELL, audit, insert, play, scripted
from exchange_kit import cancel as market_cancel

from vigil.pipeline import HONEST, audit_csv, audit_records, field_of, simulate_and_audit
from vigil.rtg import CompetitorAccount, Instrument, Lifespan, Side
from vigil.rtg.events import MatchRecord, read_match_events
from vigil.strategies.badactors import TICK, CloseMarker, Layerer, Spoofer
from vigil.surveillance.alerts import Alert, Severity, alert_id
from vigil.surveillance.detectors.conduct import WashTrading, _PairTrade
from vigil.surveillance.engine import SurveillanceEngine, header_for
from vigil.surveillance.market import MarketReplica
from vigil.surveillance.rulebook import load_rulebook, parse_rulebook


def flagged(audit) -> Counter:
    return Counter((alert.competitor, alert.control) for alert in audit.alerts)


def honest_alerts(audit) -> list[Alert]:
    return [alert for alert in audit.alerts if alert.competitor in HONEST]


# -- a field of honest traders -----------------------------------------------------------------------

def test_honest_traders_raise_no_alerts(audits):
    audit = audits("clean", 300.0)
    assert audit.alerts == []
    assert audit.result.integrity["discrepancies"] == 0
    assert audit.result.integrity["trade_reports_checked"] > 500
    assert set(audit.result.controls_run) == set(load_rulebook().controls)


def test_surveillance_recomputes_the_exchanges_score(audits):
    """Positions and P&L are rebuilt from the event stream alone and must equal the exchange's books."""
    audit = audits("clean", 300.0)
    for name, team in audit.simulation.teams.items():
        metrics = audit.result.participants[name]
        assert metrics["profit_or_loss"] == pytest.approx(team.pnl, abs=0.005)
        assert (metrics["etf_position"], metrics["future_position"]) == (team.etf_position, team.future_position)
        assert metrics["fees"] == pytest.approx(team.total_fees, abs=0.005)
        assert metrics["orders"] == team.inserts and metrics["hedges"] == team.hedges


# -- one abuse at a time -----------------------------------------------------------------------------

@pytest.mark.parametrize("scenario, seconds, culprit, control, severity", [
    ("spoofer", 150.0, "Spoofer", "MKT-01", Severity.HIGH),
    ("layerer", 200.0, "Layerer", "MKT-01", Severity.HIGH),
    ("stuffer", 90.0, "Stuffer", "MKT-03", Severity.MEDIUM),
    ("stuffer", 90.0, "Stuffer", "MKT-05", Severity.MEDIUM),
    ("stuffer", 90.0, "Stuffer", "LIM-03", Severity.LOW),
    ("reckless", 80.0, "Reckless", "LIM-02", Severity.CRITICAL),
    ("limit_breaker", 30.0, "LimitBreaker", "LIM-01", Severity.CRITICAL),
    ("fat_finger", 110.0, "FatFinger", "MKT-06", Severity.MEDIUM),
])
def test_control_catches_its_abuse(audits, scenario, seconds, culprit, control, severity):
    audit = audits(scenario, seconds)
    own = [alert for alert in audit.alerts if alert.competitor == culprit and alert.control == control]
    assert own, f"{control} did not fire for {culprit}; raised: {dict(flagged(audit))}"
    assert max(alert.severity for alert in own) == severity
    assert honest_alerts(audit) == []
    assert audit.result.integrity["discrepancies"] == 0
    for alert in own:
        assert alert.regulations == load_rulebook().control(control).regulations
        assert alert.time_start <= alert.time_end <= seconds + 1


def test_wash_trading_is_pinned_on_both_parties(audits):
    audit = audits("wash", 200.0)
    assert set(flagged(audit)) == {("WashA", "MKT-02"), ("WashB", "MKT-02")}
    by_name = {alert.competitor: alert for alert in audit.alerts}
    assert by_name["WashA"].counterparties == ("WashB",) and by_name["WashB"].counterparties == ("WashA",)
    metrics = by_name["WashA"].metrics
    assert metrics["pair_share_of_volume"] >= 0.5 and metrics["imbalance"] <= 0.35
    assert len(by_name["WashA"].evidence) == metrics["executions"]


def test_layering_is_named_as_such(audits):
    titles = {alert.title for alert in audits("layerer", 200.0).alerts}
    assert "Layering" in titles
    assert all(alert.metrics["levels_pulled_together"] >= 3 for alert in audits("layerer", 200.0).alerts
               if alert.title == "Layering")


def test_unhedged_exposure_warns_before_it_breaches(audits):
    audit = audits("reckless", 80.0)
    warning, breach = sorted((a for a in audit.alerts if a.control == "LIM-02"), key=lambda a: a.time_end)
    assert warning.severity == Severity.MEDIUM and warning.time_end == pytest.approx(55.0, abs=0.5)
    assert breach.severity == Severity.CRITICAL and breach.time_end == pytest.approx(70.0, abs=0.5)
    # The exchange reached the same verdict at the same moment.
    team = audit.simulation["Reckless"]
    assert team.status == "BREACH" and team.breach_time == pytest.approx(breach.time_end, abs=0.5)


def test_marking_the_close(session_one):
    from vigil.pipeline import simulate_and_audit

    class EarlyCloser(CloseMarker):
        SESSION = 200.0  # the match below is cut short; this is its close

    traders = {**field_of(session_one, "clean"), "Closer": EarlyCloser}
    audit = simulate_and_audit(session_one, traders, seed=7, end_time=200.0, match_id="test-close")
    own = [alert for alert in audit.alerts if alert.competitor == "Closer"]
    assert own and {alert.control for alert in own} == {"MKT-04"}
    metrics = own[0].metrics
    assert metrics["orders"] >= 1 and metrics["max_ticks_through_fair"] >= 1.5 and metrics["closing_position"] > 0
    assert honest_alerts(audit) == []


# -- the same abuses, done another way ----------------------------------------------------------------

class AmendSpoofer(Spoofer):
    """Takes its shown size out of the book by amending it to nothing instead of cancelling it."""

    def cancel(self, order_id):
        if order_id in self.shown and order_id in self.sides:
            self.send_amend_order(order_id, 0)
        else:
            super().cancel(order_id)


class ThrowawaySpoofer(Spoofer):
    """After each pull, sends an order on the same side that looks like a re-quote and never rests."""

    def end(self):
        side = Side.BUY if self.genuine_side == Side.SELL else Side.SELL
        pulled, price = bool(self.shown), self.shown_price
        super().end()
        if pulled:
            away = price - 20 * TICK if side == Side.BUY else price + 20 * TICK
            self.insert(side, away, self.SHOWN_LOTS // 2, Lifespan.FILL_AND_KILL)


class DistantSpoofer(Spoofer):
    """After each pull, rests a token order on the same side far from the market, and clears it before the next."""

    def __init__(self, loop, team_name, secret):
        super().__init__(loop, team_name, secret)
        self.token = None

    def begin(self):
        self.cancel(self.token)
        self.token = None
        super().begin()

    def end(self):
        side = Side.BUY if self.genuine_side == Side.SELL else Side.SELL
        pulled, price = bool(self.shown), self.shown_price
        super().end()
        if pulled and self.token is None:
            self.token = self.insert(side, price - 20 * TICK if side == Side.BUY else price + 20 * TICK, 30)


class SlowLayerer(Layerer):
    """Pulls its layers one per book update, a quarter of a second apart, instead of all at once."""

    def __init__(self, loop, team_name, secret):
        super().__init__(loop, team_name, secret)
        self.leaving: list[int] = []

    def act(self):
        if self.leaving:
            self.cancel(self.leaving.pop(0))
            return
        super().act()

    def end(self):
        self.leaving, self.shown = list(self.shown), []
        super().end()


@pytest.mark.parametrize("trader, title", [(AmendSpoofer, "Spoofing and layering"),
                                           (ThrowawaySpoofer, "Spoofing and layering"),
                                           (DistantSpoofer, "Spoofing and layering"), (SlowLayerer, "Layering")])
def test_spoofing_is_caught_however_the_size_is_withdrawn(session_one, trader, title):
    audit_ = simulate_and_audit(session_one, {**field_of(session_one, "clean"), "Culprit": trader}, seed=7,
                                end_time=200.0, match_id=f"test-{trader.__name__}")
    assert audit_.simulation["Culprit"].status == "OK" and not audit_.simulation["Culprit"].errors
    own = [alert for alert in audit_.alerts if alert.competitor == "Culprit"]
    assert own and {alert.control for alert in own} == {"MKT-01"}
    assert {alert.title for alert in own} == {title}
    assert honest_alerts(audit_) == [] and audit_.result.integrity["discrepancies"] == 0


def test_spoofing_goes_on_being_reported_after_the_first_alert(session_one):
    """Every episode ends up in some alert: what piles up after one is reported by the next, or at the end."""
    audit_ = simulate_and_audit(session_one, field_of(session_one, "spoofer"), seed=7, end_time=300.0,
                                match_id="test-spoofer-300")
    own = [alert for alert in audit_.alerts if alert.competitor == "Spoofer"]
    assert len(own) >= 3
    reported = [order for alert in own for order in alert.orders]
    assert len(reported) == len(set(reported))  # no episode twice
    assert sum(alert.metrics["episodes"] for alert in own) >= 3 * len(own) - 2
    # The alerts partition the match: each starts after the one before it ended.
    assert all(a.time_end <= b.time_end for a, b in zip(own, own[1:], strict=False))


def _long_then_paying_up(late_orders):
    """A team that gets long early and pays through fair value near the end of a forty-second match."""
    return scripted({
        4: [lambda trader: trader.buy(100000, 50, FAK), lambda trader: trader.hedge(SELL, 100, 50)],
        **{150 + 2 * k: [lambda trader: trader.buy(100200, 1, FAK), lambda trader: trader.hedge(SELL, 100, 1)]
           for k in range(late_orders)},
    })


def test_the_close_belongs_to_whoever_traded_last_in_the_book():
    market = [
        insert(0.01, FUTURE, Side.BUY, 100000, 99900, order_id=1001),
        insert(0.01, FUTURE, SELL, 100000, 100000, order_id=1002),
        insert(0.02, FUTURE, Side.BUY, 1, 100000, FAK),
        insert(0.03, ETF, Side.BUY, 1000, 99900, order_id=1004),
        insert(0.03, ETF, SELL, 1000, 100000, order_id=1003),
        insert(0.04, ETF, SELL, 1000, 100200, order_id=1005),
        market_cancel(30.0, ETF, 1003),  # the offer at fair value goes: the best offer is now two ticks above it
    ]
    # One order, a lot bought 2.5 ticks through fair value with a long position behind it, and it is the last print.
    result, _ = play(market, {"C": _long_then_paying_up(1)}, until=39.6)
    audited = audit(result, check_score=True)
    (alert,) = audited.alerts
    assert alert.control == "MKT-04" and alert.severity.name == "HIGH"
    assert alert.metrics["set_the_closing_price"] and alert.metrics["closing_mark"] == 100200
    assert alert.metrics["mark_benefit"] == pytest.approx(51 * 200 / 100)

    # The same, but afterwards the market trades with itself lower down. That is the close, and
    # it is what the exchange marks the position at; nobody's trade set it.
    market.append(insert(39.3, ETF, SELL, 5, 99900, FAK))
    result, _ = play(market, {"C": _long_then_paying_up(1)}, until=39.6)
    audited = audit(result, check_score=True)
    assert audited.result.market["closing_etf_price"] == 99900
    assert audited.result.participants["C"]["profit_or_loss"] == pytest.approx(result["C"].pnl, abs=0.005)
    # What is left is the attempt: a print that would have paid, had it stood.
    (alert,) = audited.alerts
    assert alert.title == "Marking the close: attempt" and alert.severity.name == "MEDIUM"
    assert not alert.metrics["set_the_closing_price"] and alert.metrics["closing_mark"] == 99900
    assert alert.metrics["benefit_aimed_at"] == pytest.approx(51 * 200 / 100) and alert.metrics["mark_benefit"] < 0
    assert "the session's last trade was another" in alert.description


def test_an_attempt_on_the_close_is_counted_in_orders_and_described_as_it_was():
    market = [
        insert(0.01, FUTURE, Side.BUY, 100000, 99900), insert(0.01, FUTURE, SELL, 100000, 100000),
        insert(0.02, FUTURE, Side.BUY, 1, 100000, FAK),
        insert(0.03, ETF, Side.BUY, 1000, 99900), insert(0.03, ETF, SELL, 5, 100000),
        *(insert(0.04, ETF, SELL, 1, 100200) for _ in range(3)), insert(0.05, ETF, SELL, 1000, 100300),
    ]
    long_five = [lambda trader: trader.buy(100000, 5, FAK), lambda trader: trader.hedge(SELL, 100, 5)]
    # One order that lifts three resting offers is one order, not three trades.
    one = scripted({4: long_five, 150: [lambda trader: trader.buy(100200, 3, FAK),
                                        lambda trader: trader.hedge(SELL, 100, 3)]})
    result, _ = play(market, {"C": one}, until=39.6)
    assert result["C"].etf_position == 8
    assert audit(result).alerts == []  # the close was set, for sixteen dollars: below what is reported

    # Three orders are an attempt, and the description does not claim the close was missed when it was not.
    three = scripted({4: long_five, **{150 + 2 * k: [lambda trader: trader.buy(100200, 1, FAK),
                                                     lambda trader: trader.hedge(SELL, 100, 1)] for k in range(3)}})
    result, _ = play(market, {"C": three}, until=39.6)
    (alert,) = audit(result).alerts
    assert alert.title == "Marking the close: attempt" and alert.severity.name == "MEDIUM"
    assert alert.metrics["orders"] == 3 and len(alert.orders) == 3 and alert.metrics["set_the_closing_price"]
    assert "did set the closing price" in alert.description and "only $16.00" in alert.description


def test_lots_handed_back_are_found_behind_a_trade_that_was_not_their_return(rulebook):
    engine = SurveillanceEngine(header_for("wash", teams=["A", "B"]), rulebook)
    detector = next(d for d in engine._detectors if isinstance(d, WashTrading))
    trades = deque([
        _PairTrade(0.0, 1, "A", "B", 10, 0.0),  # B sells ten to A, hitting A's bid
        _PairTrade(1.0, 2, "B", "B", 10, 0.0),  # B buys ten from A, again as the aggressor: not a hand-back
        _PairTrade(2.0, 3, "B", "A", 10, 0.0),  # A sells ten to B, hitting B's bid: the first ten, handed back
    ])
    assert detector._returned(("A", "B"), trades) == 10
    trades[2] = _PairTrade(20.0, 3, "B", "A", 10, 0.0)  # too long after to be "straight back"
    assert detector._returned(("A", "B"), trades) == 0


def test_every_rogue_is_caught_in_one_match(audits):
    audit = audits("rogues", 120.0)
    caught = {name for name, _ in flagged(audit)}
    assert {"WashA", "WashB", "Spoofer", "Stuffer", "Reckless"} <= caught
    assert honest_alerts(audit) == []


# -- books and records -------------------------------------------------------------------------------

def _audit_stream(records, teams):
    return audit_records(header_for("tampered", teams=teams), records)


def test_an_altered_trade_report_fails_reconciliation(audits):
    audit = audits("clean", 60.0)
    records = list(audit.simulation.records())
    index = next(i for i, r in enumerate(records) if r.op == "Trade" and r.competitor == "VigilFull")
    records[index] = dataclasses.replace(records[index], price=records[index].price + 100)  # one tick better
    tampered = _audit_stream(records, audit.header.teams)
    assert tampered.result.integrity["discrepancies"] >= 1
    alert = next(a for a in tampered.alerts if a.control == "REC-01")
    assert alert.severity == Severity.HIGH and records[index].seq in alert.evidence
    assert alert.metrics["expected"]["price"] == records[index].price - 100


def test_a_deleted_trade_report_fails_reconciliation(audits):
    audit = audits("clean", 60.0)
    records = list(audit.simulation.records())
    index = next(i for i, r in enumerate(records) if r.op == "Trade" and r.competitor)
    del records[index]
    tampered = _audit_stream(records, audit.header.teams)
    kinds = {a.metrics["kind"] for a in tampered.alerts if a.control == "REC-01"}
    assert "missing" in kinds or "mismatch" in kinds
    assert tampered.result.integrity["discrepancies"] >= 1


def test_an_invented_trade_report_fails_reconciliation(audits):
    audit = audits("clean", 60.0)
    records = list(audit.simulation.records())
    index = next(i for i, r in enumerate(records) if r.op == "Trade" and r.competitor)
    records.insert(index + 40, dataclasses.replace(records[index], time=records[index + 40].time))
    tampered = _audit_stream(records, audit.header.teams)
    assert any(a.control == "REC-01" for a in tampered.alerts)


def test_the_real_exchanges_record_reconciles(capture):
    """The same checks, on files the real exchange wrote."""
    audit = audit_csv(capture / "match_events.csv", exchange_json=capture / "exchange.json",
                      score_board=capture / "score_board.csv", match_id="capture")
    assert audit.result.integrity["discrepancies"] == 0
    assert audit.result.integrity["trade_reports_checked"] > 30
    assert [a for a in audit.alerts if a.control == "REC-01"] == []
    with open(capture / "score_board.csv", newline="") as handle:
        last = {row["Team"]: row for row in csv.DictReader(handle) if row["Operation"] == "Tick"}
    for name, row in last.items():
        metrics = audit.result.participants[name]
        assert (metrics["etf_position"], metrics["future_position"]) == (int(row["EtfPosition"]),
                                                                      int(row["FuturePosition"]))
        assert round(metrics["fees"] * 100) == int(row["TotalFees"])
        # The exchange marked this final score a fraction of a second after its last event.
        assert metrics["profit_or_loss"] == pytest.approx(int(row["ProfitOrLoss"]) / 100, abs=150)
        assert audit.report.document["participants"][name]["exchange"]["status"] == "OK"


def test_order_to_trade_counts_messages_as_rts_9_does(capture):
    """An entry or a cancellation is one message, an amendment two, and a fill-and-kill order that
    the book cancels unfilled two: its entry and its cancellation."""
    records = list(read_match_events(capture / "match_events.csv"))
    engine = SurveillanceEngine(header_for("capture", teams=["VigilFull", "Example"]))
    result = engine.run(records)
    for name in ("VigilFull", "Example"):
        who = engine.participants[name]
        mine = [r for r in records if r.competitor == name]
        immediate = {r.order_id for r in mine if r.op == "Insert" and r.lifespan == "F"}
        killed = sum(r.op == "Cancel" and r.order_id in immediate for r in mine)
        assert who.automatic_cancels == killed
        assert who.cancels == sum(r.op == "Cancel" for r in mine) - killed
        messages = sum(r.op in ("Insert", "Cancel") for r in mine) + 2 * sum(r.op == "Amend" for r in mine)
        assert who.order_messages() == messages
        executed = {r.order_id for r in mine if r.op == "Trade"}
        assert who.order_to_trade()[0] == pytest.approx(messages / len(executed) - 1)
        assert result.participants[name]["order_messages"] == messages
    assert engine.participants["VigilFull"].automatic_cancels > 0  # the capture does exercise the case


def test_positions_are_marked_as_the_score_board_marks_them():
    """Until the future has traded the exchange's score board marks every position at zero."""
    header = header_for("early", teams=["T"])
    fee = round(0.0002 * 150000 * 10)
    records = [
        MatchRecord(0, 0.1, "", "Insert", 1, 1, "A", 10, 150000, "G", None),
        MatchRecord(1, 0.2, "T", "Insert", 1, 1, "B", 10, 150000, "F", None),
        MatchRecord(2, 0.2, "T", "Trade", 1, 1, "B", 10, 150000, None, fee),
    ]
    result = SurveillanceEngine(header).run(records)
    account = CompetitorAccount(1.0, 0.002)
    account.transact(Instrument.ETF, Side.BUY, 150000, 10, fee)
    account.update(0, 150000)  # what Competitor.on_timer_tick passes when the future has no last trade
    assert result.participants["T"]["profit_or_loss"] == account.profit_or_loss / 100 == -15003.0
    assert result.market["closing_future_price"] is None and result.market["closing_etf_mark"] == 0.0


# -- mechanics ---------------------------------------------------------------------------------------

def test_alert_ids_are_stable_and_alerts_round_trip(audits):
    audit = audits("wash", 200.0)
    alert = audit.alerts[0]
    assert alert.alert_id.startswith("MKT-02-")
    assert alert_id("m", "MKT-02", "A", "k") == alert_id("m", "MKT-02", "A", "k") != alert_id("m", "MKT-02", "B", "k")
    assert Alert.from_dict(alert.to_dict()) == alert
    assert len({a.alert_id for a in audit.alerts}) == len(audit.alerts)


def test_streaming_and_batch_processing_agree(audits):
    audit = audits("wash", 200.0)
    engine = SurveillanceEngine(audit.header)
    streamed = []
    for record in audit.simulation.records():
        streamed.extend(engine.process(record))
    result = engine.finalize()
    assert [a.alert_id for a in result.alerts] == [a.alert_id for a in audit.alerts]
    assert {a.alert_id for a in streamed} <= {a.alert_id for a in result.alerts}
    assert len(streamed) == len(result.alerts)  # nothing here waits for the end of the session


def test_replica_rebuilds_the_book_and_names_both_sides(capture):
    replica = MarketReplica()
    pairs = Counter()
    reports = 0
    for record in read_match_events(capture / "match_events.csv"):
        if record.op in ("Insert", "Cancel", "Amend"):
            for fill in replica.apply(record):
                pairs[(fill.aggressor, fill.passive)] += fill.volume
                assert fill.buyer != fill.seller or fill.buyer == ""
        elif record.op == "Trade":
            expected = replica.expected.popleft()
            assert (expected.competitor, expected.order_id, expected.price, expected.volume, expected.fee) == (
                record.competitor, record.order_id, record.price, record.volume, record.fee)
            reports += 1
    assert not replica.expected and reports > 30
    assert pairs[("", "VigilFull")] > 0  # customers trading with our quotes
    assert pairs[("VigilFull", "")] > 0  # and our taking of resting customer orders
    assert replica.best_bid(1) < replica.best_ask(1)


def test_rulebook_is_consistent(rulebook):
    assert len(rulebook.controls) == 10 and len(rulebook.sha256) == 64
    for control in rulebook.controls.values():
        assert control.regulations, control.key
        assert all(key in rulebook.regulations for key in control.regulations)
    for regulation in rulebook.regulations.values():
        assert regulation.url.startswith("https://") and regulation.citation and regulation.summary
        assert rulebook.controls_citing(regulation.key) or regulation.key in ("FINRA-3110", "MAR-16")
    with pytest.raises(ValueError):
        parse_rulebook({"regulations": {}, "controls": {"X": {"title": "t", "category": "c", "severity": "LOW",
                                                             "description": "d", "regulations": ["nope"]}}})


def test_thresholds_come_from_the_rulebook(audits, rulebook):
    """Loosen one threshold and the alert goes away: the engine reads the rulebook, not constants."""
    import copy

    import yaml

    from vigil.surveillance.rulebook import default_rulebook_path
    audit = audits("wash", 200.0)
    raw = yaml.safe_load(default_rulebook_path().read_text())
    lax = copy.deepcopy(raw)
    lax["controls"]["MKT-02"]["parameters"]["min_round_trip_lots"] = 100_000
    relaxed = audit_records(audit.header, audit.simulation.records(), parse_rulebook(lax))
    assert relaxed.alerts == []
    assert relaxed.report.document["rulebook"]["sha256"] != rulebook.sha256
