#!/usr/bin/env bash
# The whole pipeline, end to end, on this machine. About three minutes.
#
#   1. start a Kafka broker and an Ethereum node (both local, both out of .tools/)
#   2. scrape the Federal Register into the regulatory register
#   3. simulate a match in which six of nine teams misbehave; audit it; anchor the report
#   4. verify the report against the chain, then tamper with a copy and watch that fail
#   5. run a match on the REAL exchange, streaming its events through Kafka to a monitor
#      process that raises alerts, writes the report and anchors it
#
# Needs: scripts/bootstrap_tools.sh to have been run, and the package installed in .venv.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
VIGIL=".venv/bin/vigil"
STAMP="$(date +%H%M%S)"
# The demo files its reports on the local chain it starts, whatever network this shell is set up for.
export VIGIL_ETH_RPC="http://127.0.0.1:8545"
unset VIGIL_ETH_PRIVATE_KEY VIGIL_REGISTRY

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "1. local services"
scripts/kafka_local.sh start
scripts/anvil_local.sh start

step "2. regulatory scrape (Federal Register, via the API listed on ApiVault)"
$VIGIL regulatory scrape | tail -12

step "3. a simulated match with six rogue teams, audited and anchored"
SIM="demo-sim-$STAMP"
$VIGIL simulate --session 2 --scenario rogues --match-id "$SIM" --anchor
REPORT="artifacts/reports/$SIM/report.json"

step "4a. anyone can check the fingerprint with standard tools"
shasum -a 256 "$REPORT"
$VIGIL chain verify "$REPORT"

step "4b. a doctored copy (one alert downgraded) does not verify"
DOCTORED="$(mktemp -d)/report.json"
.venv/bin/python - "$REPORT" "$DOCTORED" <<'EOF'
import json, sys
document = json.load(open(sys.argv[1]))
alert = next(a for a in document["alerts"] if a["severity"] == "HIGH")
alert["severity"], alert["description"] = "LOW", "Nothing to see here."
json.dump(document, open(sys.argv[2], "w"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
print(f"edited alert {alert['alert_id']} ({alert['title']}, {alert['competitor']})")
EOF
$VIGIL chain verify "$DOCTORED" || echo "(exit status $?: rejected, as it should be)"

step "4c. one alert, proved against the anchored Merkle root by the contract itself"
ALERT="$(.venv/bin/python -c "import json,sys; print(json.load(open('$REPORT'))['alerts'][0]['alert_id'])")"
$VIGIL chain proof "$REPORT" "$ALERT" | tail -3

step "5. a match on the real exchange, through Kafka, to a monitor in another process"
LIVE="demo-live-$STAMP"
GROUP="demo-$STAMP"
# The monitor's group starts at the end of the topic: it is here for this match, not the backlog.
.tools/kafka/bin/kafka-consumer-groups.sh --bootstrap-server 127.0.0.1:9092 --group "$GROUP" \
    --topic rtg.match-events --reset-offsets --to-latest --execute >/dev/null 2>&1 || true
$VIGIL monitor --group "$GROUP" --once --idle 150 --anchor > "artifacts/monitor-$STAMP.log" 2>&1 &
MONITOR=$!
sleep 4
$VIGIL live --session 1 --seconds 75 --match-id "$LIVE" --publish --no-regulatory \
    --team VigilFull=preset:full --team Example=example --team Spoofer=actor:spoofer \
    --team Stuffer=actor:stuffer --team Reckless=actor:reckless
wait "$MONITOR" || true
echo
echo "the monitor process, which saw only what came over Kafka, reported:"
cat "artifacts/monitor-$STAMP.log"

step "the registry now holds"
$VIGIL chain history
echo
echo "reports are in artifacts/reports/ (report.json, report.md, anchor.json)"
echo "stop the services with: scripts/kafka_local.sh stop; scripts/anvil_local.sh stop"
