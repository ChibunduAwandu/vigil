#!/usr/bin/env bash
# Run a single-node Kafka broker (KRaft mode, no ZooKeeper) out of .tools/, for development.
#
#   scripts/kafka_local.sh start     # format storage on first use, start, wait until it answers
#   scripts/kafka_local.sh status
#   scripts/kafka_local.sh stop
#   scripts/kafka_local.sh reset     # stop and delete every topic and offset
#
# The broker listens on 127.0.0.1 only. Ports: VIGIL_KAFKA_PORT (default 9092) for clients and
# the next one up for the controller.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="${VIGIL_TOOLS:-$ROOT/.tools}"
KAFKA="$TOOLS/kafka"
DATA="$TOOLS/kafka-data"
CONFIG="$DATA/server.properties"
PIDFILE="$DATA/kafka.pid"
PORT="${VIGIL_KAFKA_PORT:-9092}"
CONTROLLER_PORT="$((PORT + 1))"

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

answering() { "$KAFKA/bin/kafka-broker-api-versions.sh" --bootstrap-server "127.0.0.1:$PORT" >/dev/null 2>&1; }

start() {
    if [[ ! -x "$KAFKA/bin/kafka-server-start.sh" ]]; then
        echo "Kafka is not installed; run scripts/bootstrap_tools.sh kafka" >&2; exit 1
    fi
    if running; then echo "kafka already running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"; return; fi
    mkdir -p "$DATA"
    cat > "$CONFIG" <<EOF
process.roles=broker,controller
node.id=1
controller.quorum.bootstrap.servers=127.0.0.1:$CONTROLLER_PORT
listeners=PLAINTEXT://127.0.0.1:$PORT,CONTROLLER://127.0.0.1:$CONTROLLER_PORT
advertised.listeners=PLAINTEXT://127.0.0.1:$PORT,CONTROLLER://127.0.0.1:$CONTROLLER_PORT
inter.broker.listener.name=PLAINTEXT
controller.listener.names=CONTROLLER
listener.security.protocol.map=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
log.dirs=$DATA/logs
num.partitions=1
offsets.topic.replication.factor=1
share.coordinator.state.topic.replication.factor=1
share.coordinator.state.topic.min.isr=1
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
group.initial.rebalance.delay.ms=0
log.retention.hours=168
EOF
    if [[ ! -f "$DATA/logs/meta.properties" ]]; then
        "$KAFKA/bin/kafka-storage.sh" format --standalone -t "$("$KAFKA/bin/kafka-storage.sh" random-uuid)" \
            -c "$CONFIG" >/dev/null
    fi
    # Not "-daemon": started this way the script execs the JVM, so \$! is the broker's own pid.
    LOG_DIR="$DATA/server-logs" KAFKA_HEAP_OPTS="${KAFKA_HEAP_OPTS:--Xms256m -Xmx768m}" \
        nohup "$KAFKA/bin/kafka-server-start.sh" "$CONFIG" > "$DATA/server.out" 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 60); do
        if ! running; then echo "kafka exited during start-up; see $DATA/server.out" >&2; exit 1; fi
        if answering; then echo "kafka running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"; return; fi
        sleep 1
    done
    echo "kafka did not answer within 60 s; see $DATA/server.out" >&2; exit 1
}

stop() {
    if ! running; then echo "kafka is not running"; rm -f "$PIDFILE"; return; fi
    local pid; pid="$(cat "$PIDFILE")"
    kill "$pid"
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid"; fi
    rm -f "$PIDFILE"
    echo "kafka stopped"
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    status)
        if running && answering; then echo "kafka running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT"
        elif running; then echo "kafka process $(cat "$PIDFILE") is up but not answering on 127.0.0.1:$PORT"; exit 1
        else echo "kafka is not running"; exit 1; fi ;;
    reset) stop; rm -rf "$DATA"; echo "kafka data deleted" ;;
    *) echo "usage: $0 start|stop|status|reset" >&2; exit 2 ;;
esac
