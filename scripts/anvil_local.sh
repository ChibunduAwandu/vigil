#!/usr/bin/env bash
# Run a local Ethereum node (Foundry's anvil) out of .tools/, keeping its chain between runs.
#
#   scripts/anvil_local.sh start     # listens on 127.0.0.1:8545, chain id 31337
#   scripts/anvil_local.sh status
#   scripts/anvil_local.sh stop      # saves the chain to .tools/anvil-data/state.json
#   scripts/anvil_local.sh reset     # stop and forget the chain
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="${VIGIL_TOOLS:-$ROOT/.tools}"
ANVIL="$TOOLS/foundry/anvil"
DATA="$TOOLS/anvil-data"
PIDFILE="$DATA/anvil.pid"
PORT="${VIGIL_ETH_PORT:-8545}"

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

answering() {
    curl --silent --max-time 2 -H 'Content-Type: application/json' \
        --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' "http://127.0.0.1:$PORT" 2>/dev/null \
        | grep -q result
}

case "${1:-}" in
    start)
        if [[ ! -x "$ANVIL" ]]; then echo "anvil is not installed; run scripts/bootstrap_tools.sh foundry" >&2; exit 1; fi
        if running; then echo "anvil already running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"; exit 0; fi
        mkdir -p "$DATA"
        # --state: load the chain from the file if it exists, write it back on exit and every 5 s.
        nohup "$ANVIL" --host 127.0.0.1 --port "$PORT" --state "$DATA/state.json" --state-interval 5 --silent \
            > "$DATA/anvil.out" 2>&1 &
        echo $! > "$PIDFILE"
        for _ in $(seq 1 50); do
            if ! running; then echo "anvil exited during start-up; see $DATA/anvil.out" >&2; exit 1; fi
            if answering; then echo "anvil running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"; exit 0; fi
            sleep 0.2
        done
        echo "anvil did not answer; see $DATA/anvil.out" >&2; exit 1 ;;
    status)
        if running && answering; then echo "anvil running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"
        else echo "anvil is not running"; exit 1; fi ;;
    stop)
        if running; then
            pid="$(cat "$PIDFILE")"; kill "$pid"
            for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
            echo "anvil stopped"
        else echo "anvil is not running"; fi
        rm -f "$PIDFILE" ;;
    reset)
        "$0" stop; rm -rf "$DATA"; echo "anvil chain deleted" ;;
    *) echo "usage: $0 start|stop|status|reset" >&2; exit 2 ;;
esac
