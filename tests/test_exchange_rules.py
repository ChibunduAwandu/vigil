"""The exchange's own rules and the integrity of its record, on cases the sample sessions never produce.

Most of these are scripted matches played on the exchange's own classes (see
``exchange_kit``); the rest doctor a record the real exchange wrote.
"""
from __future__ import annotations

import dataclasses

import pytest
from exchange_kit import BUY, ETF, FAK, FUTURE, SELL, audit, cancel, insert, play, quiet_market, scripted

from vigil.pipeline import audit_records
from vigil.reports.builder import outcomes_from_score_board
from vigil.rtg.events import MatchRecord, read_match_events
from vigil.surveillance.alerts import Severity
from vigil.surveillance.engine import SurveillanceEngine, header_for


def of(audited, control: str, severity: Severity | None = None) -> list:
    return [alert for alert in audited.alerts
            if alert.control == control and (severity is None or alert.severity == severity)]


# -- what the exchange does to a team it throws out --------------------------------------------------

def test_orders_cancelled_at_a_disconnection_are_not_the_participants():
    def overreach(trader):
        for level in range(9):
            trader.buy(99800 - 100 * level, 10)  # nine bids resting
        trader.buy(100000, 101, FAK)  # and 101 lots bought: one past the position limit

    result, _ = play(quiet_market(), {"C": scripted({8: [overreach]})})
    assert result["C"].status == "BREACH" and result["C"].cancels == 9  # the exchange swept its book
    audited = audit(result, check_score=True)
    metrics = audited.result.participants["C"]
    assert metrics["cut_off"]["reason"] == "position limit"
    assert metrics["cut_off"]["orders_cancelled_by_exchange"] == 9 and metrics["cancels"] == 0
    # Ten orders went in and one traded; nine cancellations it never sent are no part of its ratio.
    assert metrics["order_messages"] == 10 and metrics["order_to_trade_by_number"] == 9.0
    assert {(alert.control, alert.severity) for alert in audited.alerts} == {("LIM-01", Severity.CRITICAL)}
    assert audited.result.integrity["discrepancies"] == 0


def test_a_message_rate_breach_shows_as_the_limit_reached_and_the_exchange_cancelling():
    def churn(trader):
        for _ in range(6):  # 120 messages at once; the 51st arrives with orders resting
            orders = [trader.buy(99800 - 100 * level, 1) for level in range(10)]
            for order in orders:
                trader.send_cancel_order(order)

    result, _ = play(quiet_market(), {"C": scripted({8: [churn]})})
    assert result["C"].status == "BREACH" and "frequency" in result["C"].breach_reason
    audited = audit(result)
    (breach,) = of(audited, "LIM-03", Severity.CRITICAL)
    assert breach.metrics["messages"] == 50 and breach.metrics["disconnected"] is True
    metrics = audited.result.participants["C"]
    assert metrics["cut_off"]["reason"] == "message limit"
    # The fifty the exchange acted on were the participant's; what it then cancelled was not.
    assert metrics["orders"] + metrics["cancels"] == 50 and metrics["cut_off"]["orders_cancelled_by_exchange"] > 0


def test_a_message_rate_breach_with_nothing_resting_shows_as_the_silence_after_it():
    def hedges(count):
        def send(trader):
            for k in range(count):
                trader.hedge(BUY if k % 2 == 0 else SELL, 100000 if k % 2 == 0 else 99900, 1)
        return send

    # (A second team, doing nothing: a match with nobody left in it ends at once.)
    result, _ = play(quiet_market(), {"C": scripted({8: [hedges(60)]}), "Bystander": scripted({})})
    assert result["C"].status == "BREACH" and result["C"].hedges == 50  # the 51st was never acted on
    audited = audit(result)
    (breach,) = of(audited, "LIM-03", Severity.CRITICAL)
    assert breach.competitor == "C" and breach.metrics["messages"] == 50 and breach.metrics["silent_seconds"] > 5
    assert audited.result.integrity["discrepancies"] == 0

    # Stopping short of the limit, and then trading no more, is not a breach.
    result, _ = play(quiet_market(), {"C": scripted({8: [hedges(48)]}), "Bystander": scripted({})})
    assert result["C"].status == "OK"
    audited = audit(result)
    assert of(audited, "LIM-03", Severity.CRITICAL) == [] and len(of(audited, "LIM-03", Severity.LOW)) == 1


def test_a_team_thrown_out_for_its_exposure_is_marked_even_with_nothing_resting():
    market = [*quiet_market(), insert(50.0, FUTURE, BUY, 1, 1)]  # something to look at the clock by, late on
    result, _ = play(market, {"C": scripted({8: [lambda trader: trader.buy(100000, 30, FAK)]}),
                              "Bystander": scripted({})}, until=80.0)
    assert result["C"].status == "BREACH" and "unhedged" in result["C"].breach_reason
    audited = audit(result, check_score=True)
    cut_off = audited.result.participants["C"]["cut_off"]
    assert cut_off["reason"] == "un-hedged time limit" and cut_off["time"] == pytest.approx(result["C"].breach_time)
    (breach,) = of(audited, "LIM-02", Severity.CRITICAL)
    assert breach.time_end == pytest.approx(result["C"].breach_time) and breach.metrics["seconds"] == 60.0
    assert len(of(audited, "LIM-02", Severity.MEDIUM)) == 1  # the warning came first, once


# -- limits the exchange keeps on the wall clock -----------------------------------------------------

def _carry(unhedged_from: float, hedged_at: float | None, last: float) -> list[MatchRecord]:
    """A team that buys 20 ETF lots, and hedges them (or not) some time later."""
    fee = round(0.0002 * 100000 * 20)
    records = [
        MatchRecord(0, 0.01, "", "Insert", 1, 0, "B", 1000, 99900, "G"),
        MatchRecord(1, 0.01, "", "Insert", 2, 0, "A", 1000, 100000, "G"),
        MatchRecord(2, 0.02, "", "Insert", 3, 1, "A", 1000, 100000, "G"),
        MatchRecord(3, unhedged_from, "T", "Insert", 1, 1, "B", 20, 100000, "F"),
        MatchRecord(4, unhedged_from, "T", "Trade", 1, 1, "B", 20, 100000, None, fee),
    ]
    if hedged_at is not None:
        records.append(MatchRecord(5, hedged_at, "T", "Hedge", 2, 0, "A", 20, 99900))
    # Something from the market to carry the clock to the end.
    records.append(MatchRecord(len(records), last, "", "Insert", 9, 0, "B", 1, 100, "G"))
    return records


def test_limits_kept_on_the_wall_clock_scale_with_the_speed_of_the_match():
    records = _carry(1.0, hedged_at=100.0, last=130.0)
    normal = audit_records(header_for("speed-1", teams=["T"]), records)
    assert normal.result.integrity["discrepancies"] == 0
    (breach,) = of(normal, "LIM-02", Severity.CRITICAL)
    assert breach.time_end == 61.0

    # At twice the speed sixty seconds on the wall are 120 of match time: a warning, no breach.
    doubled = audit_records(header_for("speed-2", teams=["T"], speed=2.0), records)
    assert of(doubled, "LIM-02", Severity.CRITICAL) == []
    (warning,) = of(doubled, "LIM-02")
    assert warning.severity == Severity.MEDIUM and warning.metrics["time_limit"] == 120.0

    # And the throttle's window is half as long: sixty orders in a second, thirty in each half.
    orders = [MatchRecord(i, 1.0 + 0.01 * i + (0.3 if i >= 30 else 0.0), "T", "Insert", i + 1, 1, "B", 1, 90000, "G")
              for i in range(60)]
    assert len(of(audit_records(header_for("rate-1", teams=["T"]), orders), "LIM-03", Severity.CRITICAL)) == 1
    assert of(audit_records(header_for("rate-2", teams=["T"], speed=2.0), orders), "LIM-03") == []


def test_an_unhedged_breach_is_reported_once_as_of_when_the_time_ran_out():
    # Nothing happens between the trade and ten seconds after the limit: the first look comes late.
    late = audit_records(header_for("late", teams=["T"]), _carry(1.0, hedged_at=None, last=71.0))
    (only,) = of(late, "LIM-02")
    assert only.severity == Severity.CRITICAL and (only.time_start, only.time_end) == (1.0, 61.0)

    # The limit runs out between the last look of the clock and the end of the stream.
    records = _carry(1.0, hedged_at=None, last=60.9)
    records.append(MatchRecord(len(records), 61.05, "", "Insert", 10, 0, "B", 1, 100, "G"))
    at_the_end = audit_records(header_for("end", teams=["T"]), records)
    assert [alert.severity for alert in of(at_the_end, "LIM-02")] == [Severity.MEDIUM, Severity.CRITICAL]
    assert at_the_end.result.participants["T"]["longest_unhedged_seconds"] == pytest.approx(60.05)


# -- the record --------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def recorded(capture):
    """A match the real exchange played: its events, and an audit of them against its score board."""
    records = list(read_match_events(capture / "match_events.csv"))
    header = header_for("capture", teams=["VigilFull", "Example"])
    outcomes = outcomes_from_score_board(capture / "score_board.csv")
    return records, header, outcomes


def test_the_real_record_passes_every_check(recorded):
    records, header, outcomes = recorded
    integrity = audit_records(header, records, outcomes=outcomes).result.integrity
    assert integrity == {
        "trade_reports_checked": sum(r.op == "Trade" for r in records),
        "hedges_checked": sum(r.op == "Hedge" for r in records),
        "scores_checked": 2, "discrepancies": 0, "stopped_after_sequence": None}
    assert integrity["hedges_checked"] > 30


@pytest.mark.parametrize("change", [{"side": "A"}, {"instrument": 0}, {"volume": 1}, {"fee": 0}, {"order_id": 999}])
def test_a_trade_report_altered_in_any_respect_fails_reconciliation(recorded, change):
    records, header, _ = recorded
    index = next(i for i, r in enumerate(records) if r.op == "Trade" and r.competitor == "VigilFull" and r.side == "B")
    doctored = [*records]
    doctored[index] = dataclasses.replace(records[index], **change)
    audited = audit_records(header, doctored)
    kinds = [alert.metrics["kind"] for alert in of(audited, "REC-01")]
    assert kinds and set(kinds) <= {"mismatch", "unexpected", "missing"}
    assert any(records[index].seq in alert.evidence or alert.metrics["kind"] == "missing"
               for alert in of(audited, "REC-01"))


def test_a_missing_report_is_laid_at_the_door_of_the_order_it_was_for(recorded):
    records, header, _ = recorded
    # Two reports of one trade, one for each side of it.
    index = next(i for i in range(len(records) - 1) if records[i].op == records[i + 1].op == "Trade"
                 and records[i].competitor != records[i + 1].competitor)
    gone = records[index]
    audited = audit_records(header, records[:index] + records[index + 1:])
    assert audited.result.integrity["discrepancies"] == 1
    (alert,) = of(audited, "REC-01")
    assert alert.competitor == gone.competitor and alert.metrics["kind"] == "missing"
    assert alert.orders == (gone.order_id,) and alert.metrics["expected"]["volume"] == gone.volume

    # The same two the other way round: nothing is missing, but it is not how the exchange writes them.
    swapped = [*records]
    swapped[index], swapped[index + 1] = records[index + 1], records[index]
    audited = audit_records(header, swapped)
    assert audited.result.integrity["discrepancies"] == 1
    (alert,) = of(audited, "REC-01")
    assert alert.competitor == "(exchange)" and "different sequence" in alert.description


def test_a_hedge_at_a_price_the_book_would_not_give_fails_reconciliation(recorded):
    records, header, _ = recorded
    index = next(i for i, r in enumerate(records) if r.op == "Hedge")
    doctored = [*records]
    doctored[index] = dataclasses.replace(records[index], price=records[index].price + 5000)
    audited = audit_records(header, doctored)
    (alert,) = of(audited, "REC-01")
    assert alert.metrics["kind"] == "hedge" and alert.evidence == (records[index].seq,)
    assert records[index].price in alert.metrics["possible_prices"]


def test_a_hedge_removed_or_resized_shows_only_against_the_score_board(recorded):
    """A hedge leaves no mark on the book, so the stream alone cannot say one is missing."""
    records, header, outcomes = recorded
    index = next(i for i, r in enumerate(records) if r.op == "Hedge" and r.competitor == "Example")
    removed = records[:index] + records[index + 1:]
    resized = [*records]
    resized[index] = dataclasses.replace(records[index], volume=records[index].volume + 1)
    for doctored in (removed, resized):
        assert audit_records(header, doctored).result.integrity["discrepancies"] == 0
        audited = audit_records(header, doctored, outcomes=outcomes)
        (alert,) = of(audited, "REC-01")
        assert alert.competitor == "Example" and alert.metrics["kind"] == "score"
        assert alert.metrics["from_the_stream"] != alert.metrics["score_board"]


def test_events_that_cannot_be_read_are_findings_not_crashes(recorded):
    records, header, _ = recorded
    good = records[300]
    rubbish = [
        MatchRecord(0, 1.0, "X", "Insert", 1, 1, None, 10, 100000, "G"),  # no side
        MatchRecord(0, 1.0, "X", "Insert", 1, 1, "B", 10, None, "G"),  # no price
        MatchRecord(0, 1.0, "X", "Insert", 1, 1, "B", 10, 100000, None),  # no lifespan
        MatchRecord(0, 1.0, "X", "Insert", 1, None, "B", 10, 100000, "G"),  # no instrument
        MatchRecord(0, 1.0, "X", "Insert", 1, 1, "B", 0, 100000, "G"),  # no volume
        MatchRecord(0, 1.0, "X", "Trade", 1, 1, "B", 10, None, None, 3),  # a trade at no price
        MatchRecord(0, 1.0, "X", "Hedge", 1, 1, "B", 10, 100000),  # a hedge in the ETF
        MatchRecord(0, 1.0, "X", "Settle", 1),  # not an operation
        MatchRecord(0, float("nan"), "X", "Cancel", 1, volume=-1),  # no time
        MatchRecord(0, 1.0, "X", "Cancel", "one", volume=-1),  # no order number
        MatchRecord(0, 1.0, "X", "Amend", 1, volume=5),  # an amendment that adds volume
    ]
    doctored = [*records]
    for offset, bad in enumerate(rubbish):
        doctored.insert(300 + 2 * offset, dataclasses.replace(bad, seq=good.seq, time=bad.time if bad.time != 1.0
                                                              else good.time))
    engine = SurveillanceEngine(header)
    result = engine.run(doctored)  # does not raise
    assert result.integrity["discrepancies"] == len(rubbish)
    assert {alert.metrics["kind"] for alert in result.alerts if alert.control == "REC-01"} == {"unreadable"}
    # The genuine events around them were all still followed.
    assert result.integrity["trade_reports_checked"] == sum(r.op == "Trade" for r in records)


def test_an_order_number_live_in_both_books_is_resolved_as_the_exchange_resolves_it():
    market = [
        *quiet_market(bid=99800, ask=100000),
        insert(0.05, FUTURE, SELL, 40, 100100, order_id=7),
        insert(0.06, ETF, SELL, 30, 99900, order_id=7),
        cancel(1.0, FUTURE, 7),  # the record of this cancellation does not say which book
    ]
    result, _ = play(market, {"C": scripted({8: [lambda trader: trader.buy(99900, 30, FAK)]})})
    assert result["C"].etf_position == 30  # ETF order 7 was still there to be bought
    audited = audit(result)
    assert audited.result.integrity["discrepancies"] == 0 and audited.alerts == []

    # If the two orders are the same size as well, the record really is ambiguous, and says nothing to settle it.
    market[-2] = insert(0.06, ETF, SELL, 40, 99900, order_id=7)
    result, _ = play(market, {"C": scripted({8: [lambda trader: trader.buy(99900, 30, FAK)]})})
    audited = audit(result)
    assert any("could have meant either" in alert.description for alert in of(audited, "REC-01"))


def test_alerts_raised_at_the_end_of_a_match_are_announced_once_each(audits):
    direct = audits("stuffer", 90.0)
    assert any(alert.control == "MKT-05" for alert in direct.alerts)  # raised by an end-of-session check
    announced = []
    again = audit_records(direct.header, direct.simulation.records(), on_alert=announced.append)
    assert sorted(alert.alert_id for alert in announced) == sorted(alert.alert_id for alert in again.alerts)
    assert len(announced) == len({alert.alert_id for alert in announced})


def test_a_hole_in_the_stream_stops_surveillance_rather_than_guessing(audits):
    direct = audits("wash", 200.0)
    records = list(direct.simulation.records())
    engine = SurveillanceEngine(direct.header)
    for record in records[:5000]:
        engine.process(record)
    before = len(engine.alerts)
    (gap,) = engine.gap(records[4999].seq, 300)
    assert gap.control == "REC-01" and gap.severity == Severity.CRITICAL and gap.metrics["kind"] == "gap"
    for record in records[5300:]:
        assert engine.process(record) == []
    result = engine.finalize()
    assert len(result.alerts) == before + 1  # nothing was concluded from a book that could not be rebuilt
    assert result.integrity["stopped_after_sequence"] == records[4999].seq
    assert result.market["events"] == len(records) - 300
