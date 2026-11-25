"""Reproducible review commands, separate from the live trading path."""
from __future__ import annotations

import hashlib
import json
import statistics
import time
import tracemalloc
from pathlib import Path

from vigil.pipeline import audit_csv
from vigil.provenance import environment, file_hash, software
from vigil.reports.builder import ComplianceReport
from vigil.reports.canonical import canonical_json
from vigil.rtg.events import MatchHeader, read_match_events
from vigil.storage import atomic_write
from vigil.surveillance.engine import SurveillanceEngine
from vigil.surveillance.rulebook import load_rulebook


def report_header(report: ComplianceReport) -> MatchHeader:
    match = report.document["match"]
    return MatchHeader(report.report_id, match["source"], match["started_at"] or "",
                       **{k: match[k] for k in ("teams", "limits", "fees", "instrument", "speed")})


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))] if values else None


def benchmark(events: Path, repeat: int = 3) -> dict:
    if not 1 <= repeat <= 100:
        raise ValueError("repeat must be between 1 and 100")
    records = list(read_match_events(events))
    if not records:
        raise ValueError("benchmark needs at least one event")
    report = audit_csv(events, match_id="benchmark").report
    rulebook, header = load_rulebook(), report_header(report)
    runs = []
    for _ in range(repeat):
        engine, latencies = SurveillanceEngine(header, rulebook), []
        start = time.perf_counter()
        for record in records:
            before = time.perf_counter_ns()
            engine.process(record)
            latencies.append((time.perf_counter_ns() - before) / 1000)
        engine.finalize()
        elapsed = time.perf_counter() - start
        runs.append({"seconds": elapsed, "events_per_second": len(records) / elapsed,
                     "handler_microseconds": {f"p{p}": percentile(latencies, p / 100) for p in (50, 95, 99)}})
    tracemalloc.start()
    engine = SurveillanceEngine(header, rulebook)
    for record in records:
        engine.process(record)
    engine.finalize()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    timings = {}
    raw = report.canonical
    start = time.perf_counter()
    for _ in range(1000):
        hashlib.sha256(raw).digest()
    timings["sha256_microseconds"] = (time.perf_counter() - start) * 1000
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        payload = b"vigil.signature/1\0" + hashlib.sha256(raw).digest()
        start = time.perf_counter()
        for _ in range(1000):
            signature = key.sign(payload)
        timings["ed25519_sign_microseconds"] = (time.perf_counter() - start) * 1000
        public = key.public_key()
        start = time.perf_counter()
        for _ in range(1000):
            public.verify(signature, payload)
        timings["ed25519_verify_microseconds"] = (time.perf_counter() - start) * 1000
    except ImportError:
        timings["ed25519"] = "install .[signing] to measure"
    return {"schema": "vigil.benchmark/1", "events": len(records), "input_sha256": file_hash(events),
            "software": software(), "environment": environment(), "runs": runs,
            "median_events_per_second": statistics.median(r["events_per_second"] for r in runs),
            "peak_traced_engine_bytes": peak, "attestation": timings,
            "method": "In-process replay, input preloaded, handler timing includes clock overhead; finalization included "
                      "in throughput. Separate tracemalloc pass excludes preloaded input and native allocations. "
                      "Signing excludes file IO. These are local microbenchmarks, not Kafka capacity estimates."}


def reproduce(output: Path) -> dict:
    from vigil.paths import REPO_ROOT
    root = REPO_ROOT
    events = root / "tests/fixtures/live_capture/match_events.csv"
    report = audit_csv(events, match_id="captured-review",
                       exchange_json=root / "tests/fixtures/live_capture/exchange.json",
                       score_board=root / "tests/fixtures/live_capture/score_board.csv").report
    report.save(output / "report.json")
    manifest = {"schema": "vigil.reproduction/1", "software": software(), "environment": environment(),
                "report_sha256": report.sha256, "alerts_merkle_root": report.alerts_root,
                "inputs": {str(p.relative_to(root)): file_hash(p) for p in (
                    events, root / "tests/fixtures/live_capture/exchange.json",
                    root / "tests/fixtures/live_capture/score_board.csv", root / "config/rulebook.yaml",
                    root / "config/rtg-source.json", root / "uv.lock")}}
    atomic_write(output / "manifest.json", canonical_json(manifest))
    return manifest


def register_commands(add):
    def save_result(args, function):
        result = function(args)
        atomic_write(Path(args.out), (json.dumps(result, indent=2) + "\n").encode())
        print(f"Wrote {args.out}")
        return 0

    def run_soak(args):
        from vigil.soak import soak
        return save_result(args, lambda a: soak(a.matches))
    sub = add("soak", run_soak, "Measure memory retention across repeated monitor lifecycles.")
    sub.add_argument("--matches", type=int, default=1000)
    sub.add_argument("--out", default="artifacts/soak.json")

    def run_benchmark(args):
        return save_result(args, lambda a: benchmark(Path(a.events), a.repeat))
    sub = add("benchmark", run_benchmark, "Measure local replay latency, throughput, memory and attestation costs.")
    sub.add_argument("events")
    sub.add_argument("--repeat", type=int, default=3)
    sub.add_argument("--out", default="artifacts/benchmark.json")

    def run_reproduce(args):
        manifest = reproduce(Path(args.out))
        if args.check and json.loads(Path(args.check).read_text())["report_sha256"] != manifest["report_sha256"]:
            raise ValueError("reproduced report differs from the checked manifest")
        print(f"Report SHA-256: {manifest['report_sha256']}")
        return 0
    sub = add("reproduce", run_reproduce, "Reproduce the captured-match report and record all input hashes.")
    sub.add_argument("--out", default="artifacts/reproduction")
    sub.add_argument("--check", help="compare report hash with this recorded manifest")

    def run_sign(args):
        from vigil.reports.signing import keygen, sign
        if args.action == "keygen":
            private, public = keygen(Path(args.path))
            print(f"Created {private} and {public}; keep the private key secret.")
        else:
            if not args.key:
                raise ValueError("signing requires --key private.pem")
            result = sign(Path(args.path), Path(args.key), Path(args.out))
            print(f"Signed {result['report_sha256']}; wrote {args.out}")
        return 0
    sub = add("attest", run_sign, "Create an Ed25519 key pair or sign a report (requires .[signing]).")
    sub.add_argument("action", choices=["keygen", "sign"])
    sub.add_argument("path", help="key directory for keygen, report.json for sign")
    sub.add_argument("--key")
    sub.add_argument("--out", default="artifacts/signature.json")

    def run_view(args):
        from vigil.reports.viewer import build_viewer
        build_viewer(Path(args.report), Path(args.events) if args.events else None, Path(args.out))
        print(f"Open {Path(args.out).resolve()} in a browser")
        return 0
    sub = add("view", run_view, "Create an offline alert evidence viewer.")
    sub.add_argument("report")
    sub.add_argument("--events", help="matching match_events.csv, required for book reconstruction")
    sub.add_argument("--out", default="artifacts/evidence.html")

    def run_evaluate(args):
        from vigil.evaluation import evaluate
        return save_result(args, lambda a: evaluate(Path(a.protocol)))
    sub = add("evaluate", run_evaluate, "Run the frozen synthetic challenge protocol and sensitivity analysis.")
    sub.add_argument("--protocol", default="config/evaluation.json")
    sub.add_argument("--out", default="artifacts/evaluation.json")
