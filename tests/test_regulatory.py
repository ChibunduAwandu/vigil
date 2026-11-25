from __future__ import annotations

import datetime as dt

import httpx
import pytest

from vigil import net
from vigil.bus import MemoryBus, topics
from vigil.chain.cost import cost_of, eth_usd
from vigil.regulatory import apivault
from vigil.regulatory.federal_register import Document, FederalRegister
from vigil.regulatory.scraper import scrape
from vigil.regulatory.store import Register


def api_document(number: str, title: str, *, abstract: str = "", kind: str = "Rule", date: str = "2026-05-01"):
    return {
        "document_number": number, "title": title, "type": kind, "publication_date": date,
        "agencies": [{"name": "Securities and Exchange Commission", "slug": "securities-and-exchange-commission"}],
        "abstract": abstract, "html_url": f"https://www.federalregister.gov/d/{number}",
        "cfr_references": [{"title": 17, "part": "240"}, {"title": 17, "part": "240"}, {"title": 17, "part": None}],
        "effective_on": None, "comments_close_on": "2026-07-01", "citation": "91 FR 1234", "action": "Final rule.",
        "docket_ids": ["File No. S7-01-26"],
    }


class FakeRegister:
    """Stands in for the Federal Register's API: canned results per search term, one request recorded per page."""

    def __init__(self, results: dict[str, list[dict]], page_size: int = 100):
        self.results = results
        self.page_size = page_size
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        self.requests.append({"term": params["conditions[term]"], "agencies": params.get_list("conditions[agencies][]"),
                              "types": params.get_list("conditions[type][]"),
                              "since": params.get("conditions[publication_date][gte]"),
                              "page": int(params.get("page", 1))})
        term = params["conditions[term]"].strip('"')
        page = int(params.get("page", 1))
        hits = self.results.get(term, [])
        chunk = hits[(page - 1) * self.page_size: page * self.page_size]
        body = {"count": len(hits), "results": chunk}
        if page * self.page_size < len(hits):
            body["next_page_url"] = str(request.url.copy_set_param("page", page + 1))
        return httpx.Response(200, json=body)

    def client(self) -> FederalRegister:
        return FederalRegister(net.client(transport=httpx.MockTransport(self)), pause=0)


# -- the client --------------------------------------------------------------------------------------

def test_document_normalisation():
    document = Document.from_api(api_document("2026-1", "  Market   Access\nRule ", abstract="About spoofing."))
    assert document.title == "Market Access Rule"
    assert document.cfr_references == ("17 CFR 240",)  # de-duplicated, and the one without a part dropped
    assert document.agencies == ("Securities and Exchange Commission",)
    assert document.dockets == ("File No. S7-01-26",)
    assert Document.from_dict(document.to_dict()) == document
    assert document.mentions("market access") == 3 and document.mentions("spoofing") == 2
    assert document.mentions("wash sale") == 1
    changed = Document.from_api(api_document("2026-1", "Market Access Rule", abstract="About spoofing!"))
    assert changed.content_hash() != document.content_hash()


def test_search_builds_the_query_and_follows_pages():
    fake = FakeRegister({"spoofing": [api_document(f"2026-{i}", f"Doc {i}") for i in range(5)]}, page_size=2)
    found = list(fake.client().search("spoofing", agencies=["commodity-futures-trading-commission"],
                                      since=dt.date(2024, 1, 1)))
    assert [d.document_number for d in found] == [f"2026-{i}" for i in range(5)]
    assert [r["page"] for r in fake.requests] == [1, 2, 3]
    first = fake.requests[0]
    assert first["term"] == '"spoofing"'  # searched as a phrase
    assert first["agencies"] == ["commodity-futures-trading-commission"]
    assert first["types"] == ["RULE", "PRORULE"] and first["since"] == "2024-01-01"
    # max_pages caps a runaway result set
    capped = list(fake.client().search("spoofing", max_pages=1))
    assert len(capped) == 2


def test_http_helper_retries_then_gives_up():
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    http = net.client(transport=httpx.MockTransport(flaky))
    assert net.get_json(http, "https://example.test/x", backoff=0) == {"ok": True} and len(calls) == 3
    down = net.client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(net.ApiError, match="gave up after 3 attempts"):
        net.get_json(down, "https://example.test/x", retries=2, backoff=0)
    missing = net.client(transport=httpx.MockTransport(lambda request: httpx.Response(404, text="nope")))
    with pytest.raises(net.ApiError, match="HTTP 404"):  # not worth retrying
        net.get_json(missing, "https://example.test/x", backoff=0)


# -- the register ------------------------------------------------------------------------------------

def test_register_tracks_new_changed_and_unchanged(tmp_path):
    register = Register(tmp_path / "reg.db")
    document = Document.from_api(api_document("2026-1", "Rule about spoofing"))
    assert register.upsert("fr", document, "t0") == "new"
    assert register.upsert("fr", document, "t1") == "same"
    before = register.fingerprint()
    amended = Document.from_api(api_document("2026-1", "Rule about spoofing", abstract="Corrected."))
    assert register.upsert("fr", amended, "t2") == "changed"
    assert register.count() == 1 and register.fingerprint() != before
    register.relate("fr", "2026-1", "CEA-4c(a)(5)", "spoofing", 3)
    register.relate("fr", "2026-1", "MAR-12", "spoofing", 3)
    (entry,) = register.relevant()
    assert entry["regulations"] == ["CEA-4c(a)(5)", "MAR-12"] and entry["title"] == "Rule about spoofing"
    assert register.relevant(["SEC-613"]) == []
    assert register.relevant(min_score=4) == []


def test_empty_register_has_no_snapshot(tmp_path):
    assert Register(tmp_path / "reg.db").snapshot() is None


# -- the scraper -------------------------------------------------------------------------------------

def test_scrape_fills_the_register_and_announces_what_is_new(rulebook, tmp_path):
    fake = FakeRegister({
        "spoofing": [api_document("2026-10", "Spoofing in Futures Markets", date="2026-06-01"),
                     api_document("2026-11", "Unrelated omnibus rule", date="2026-05-01")],
        "market access rule": [api_document("2026-12", "Amendments to the Market Access Rule", date="2026-07-01"),
                               api_document("2026-10", "Spoofing in Futures Markets", date="2026-06-01")],
    })
    register = Register(tmp_path / "reg.db")
    bus = MemoryBus()
    result = scrape(rulebook, register, source=fake.client(), since=dt.date(2021, 1, 1), bus=bus)
    watched = sum(len(r.watch.get("terms", ())) for r in rulebook.regulations.values())
    assert result.requests == watched == len(fake.requests)
    assert {d.document_number for d in result.added} == {"2026-10", "2026-11", "2026-12"}
    assert result.seen == 4 and not result.errors and register.count() == 3
    assert {m.key for m in bus.log(topics.REGULATORY)} == {"2026-10", "2026-11", "2026-12"}
    assert all(m.value["status"] == "new" for m in bus.log(topics.REGULATORY))

    snapshot = register.snapshot()
    assert snapshot["documents"] == 3 and snapshot["sha256"] == register.fingerprint()
    recent = {item["document_number"]: item for item in snapshot["recent"]}
    assert "2026-11" not in recent  # it only mentions the term in passing
    # Found under two watches, but it is only *about* one of them: the other merely matched its text.
    assert recent["2026-10"]["regulations"] == ["CEA-4c(a)(5)"]
    in_passing = {item["document_number"]: item["regulations"] for item in register.relevant(min_score=1)}
    assert in_passing["2026-10"] == ["CEA-4c(a)(5)", "SEC-15c3-5"] and "2026-11" in in_passing
    assert [item["document_number"] for item in snapshot["recent"]] == ["2026-12", "2026-10"]  # newest first

    # A second run finds nothing new and asks only for what is recent.
    fake.requests.clear()
    again = scrape(rulebook, register, source=fake.client(), bus=bus)
    assert not again.added and not again.changed and len(bus.log(topics.REGULATORY)) == 3
    assert all(r["since"] == "2026-05-31" for r in fake.requests)  # a month before the newest held


def test_one_failed_query_does_not_sink_the_run(rulebook, tmp_path):
    def handler(request):
        if "spoofing" in request.url.params["conditions[term]"]:
            return httpx.Response(404, text="gone")
        return httpx.Response(200, json={"results": []})
    source = FederalRegister(net.client(transport=httpx.MockTransport(handler)), pause=0)
    result = scrape(rulebook, Register(tmp_path / "reg.db"), source=source, since=dt.date(2021, 1, 1))
    assert len(result.errors) == 1 and "spoofing" in result.errors[0]
    assert result.requests > 10


def test_report_carries_the_regulatory_watch(audits, rulebook, tmp_path):
    from vigil.pipeline import audit_records
    from vigil.reports.render import render_markdown
    fake = FakeRegister({"wash trading": [api_document("2026-20", "Wash Trading in Event Contracts")]})
    register = Register(tmp_path / "reg.db")
    scrape(rulebook, register, source=fake.client(), since=dt.date(2021, 1, 1))
    base = audits("wash", 200.0)
    audit = audit_records(base.header, base.simulation.records(), regulatory_watch=register.snapshot())
    watch = audit.report.document["regulatory_watch"]
    assert watch["documents"] == 1 and watch["recent"][0]["regulations"] == ["CEA-4c(a)(1)"]
    assert audit.report.sha256 != base.report.sha256  # the register's state is part of what was reported
    text = render_markdown(audit.report)
    assert "## Regulatory watch" in text and "Wash Trading in Event Contracts" in text


# -- ApiVault and CoinGecko --------------------------------------------------------------------------

def test_integrations_are_checked_against_the_catalogue():
    catalog = [{"name": i.name, "category": i.category, "auth": "apiKey" if i.needs_key else "", "url": "u"}
               for i in apivault.INTEGRATIONS if i.name != "CoinGecko"]
    rows = {row["name"]: row for row in apivault.check_integrations(catalog)}
    assert rows["Federal Register"]["listed"] and rows["Federal Register"]["auth"] == "none"
    assert rows["Etherscan"]["auth"] == "apiKey"
    assert rows["CoinGecko"]["listed"] is False
    assert not set(apivault.CONSIDERED) & {i.name for i in apivault.INTEGRATIONS}


def test_anchoring_cost():
    http = net.client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ethereum": {"usd": 2500.0}})))
    assert eth_usd(http) == 2500.0
    cost = cost_of(250_000, 2 * 10 ** 9, 2500.0)
    assert cost.ether == pytest.approx(0.0005) and cost.usd == pytest.approx(1.25)
    assert cost.describe() == "250,000 gas at 2 gwei = 0.000500 ETH = $1.25"
    assert cost_of(250_000, 10 ** 9).usd is None
    bad = net.client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "error"})))
    with pytest.raises(net.ApiError):
        eth_usd(bad)


# -- against the real services (opt in) --------------------------------------------------------------

@pytest.mark.network
def test_live_apivault_lists_every_integration():
    rows = apivault.check_integrations(apivault.fetch_catalog())
    assert all(row["listed"] for row in rows), [row["name"] for row in rows if not row["listed"]]
    assert any(entry["name"] == "Federal Register" for entry in apivault.search("federal"))


@pytest.mark.network
def test_live_federal_register_search():
    documents = list(FederalRegister().search("consolidated audit trail",
                                              agencies=["securities-and-exchange-commission"],
                                              since=dt.date(2021, 1, 1), max_pages=1))
    assert documents and all(d.document_number and d.url.startswith("https://") for d in documents)
    assert documents == sorted(documents, key=lambda d: d.publication_date, reverse=True)


@pytest.mark.network
def test_live_ether_price():
    assert 10 < eth_usd() < 1_000_000
