#!/usr/bin/env bash
# Fetch the two toolchains Vigil needs that are not Python packages, into .tools/ inside the
# project: Apache Kafka (the broker; needs a Java 17+ runtime) and Foundry (forge to build and
# test the smart contract, anvil to run a local Ethereum node). Nothing is installed system-wide
# and everything is checked against the publisher's checksum before it is unpacked.
#
#   scripts/bootstrap_tools.sh            # both
#   scripts/bootstrap_tools.sh kafka      # just the broker
#   scripts/bootstrap_tools.sh foundry    # just forge and anvil
set -euo pipefail

KAFKA_VERSION="${KAFKA_VERSION:-4.3.1}"
SCALA_VERSION="2.13"
FOUNDRY_VERSION="${FOUNDRY_VERSION:-v1.8.3}"
FORGE_STD_VERSION="${FORGE_STD_VERSION:-v1.16.2}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="${VIGIL_TOOLS:-$ROOT/.tools}"
mkdir -p "$TOOLS"

sha() {  # sha <256|512> <file>
    if command -v shasum >/dev/null 2>&1; then shasum -a "$1" "$2" | awk '{print $1}'
    else "sha${1}sum" "$2" | awk '{print $1}'; fi
}

fetch() {  # fetch <url> <destination>
    echo "  downloading $1"
    curl --fail --location --silent --show-error --retry 3 --output "$2" "$1"
}

install_kafka() {
    local name="kafka_${SCALA_VERSION}-${KAFKA_VERSION}"
    local dest="$TOOLS/kafka"
    if [[ -x "$dest/bin/kafka-server-start.sh" && "$(cat "$dest/.version" 2>/dev/null)" == "$KAFKA_VERSION" ]]; then
        echo "kafka $KAFKA_VERSION already in $dest"; return
    fi
    if ! command -v java >/dev/null 2>&1; then
        echo "kafka needs a Java runtime (17 or newer) on the PATH; none found" >&2; exit 1
    fi
    echo "kafka $KAFKA_VERSION"
    local tmp; tmp="$(mktemp -d)"
    local base="https://downloads.apache.org/kafka/${KAFKA_VERSION}"
    fetch "$base/$name.tgz" "$tmp/$name.tgz" || {
        # Releases move to the archive once they are superseded.
        base="https://archive.apache.org/dist/kafka/${KAFKA_VERSION}"
        fetch "$base/$name.tgz" "$tmp/$name.tgz"
    }
    fetch "$base/$name.tgz.sha512" "$tmp/$name.tgz.sha512"
    # Apache publishes the digest in gpg's layout: "file: ABCD EF01 ..." over several lines.
    local expected actual
    expected="$(sed -e 's/^.*://' "$tmp/$name.tgz.sha512" | tr -d ' \n\r\t' | tr 'A-F' 'a-f')"
    actual="$(sha 512 "$tmp/$name.tgz")"
    if [[ "$expected" != "$actual" ]]; then
        echo "kafka: checksum mismatch (expected $expected, got $actual)" >&2; exit 1
    fi
    rm -rf "$dest"; mkdir -p "$dest"
    tar -xzf "$tmp/$name.tgz" -C "$dest" --strip-components 1
    echo "$KAFKA_VERSION" > "$dest/.version"
    rm -rf "$tmp"
    echo "  installed in $dest"
}

install_foundry() {
    local os arch
    case "$(uname -s)" in Darwin) os=darwin ;; Linux) os=linux ;; *) echo "foundry: unsupported OS" >&2; exit 1 ;; esac
    case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=amd64 ;; *) echo "foundry: unsupported CPU" >&2; exit 1 ;; esac
    local name="foundry_${FOUNDRY_VERSION}_${os}_${arch}"
    local dest="$TOOLS/foundry"
    if [[ -x "$dest/forge" && "$(cat "$dest/.version" 2>/dev/null)" == "$FOUNDRY_VERSION" ]]; then
        echo "foundry $FOUNDRY_VERSION already in $dest"
    else
        echo "foundry $FOUNDRY_VERSION"
        local tmp; tmp="$(mktemp -d)"
        local base="https://github.com/foundry-rs/foundry/releases/download/${FOUNDRY_VERSION}"
        fetch "$base/$name.tar.gz" "$tmp/$name.tar.gz"
        fetch "$base/$name.sha256" "$tmp/$name.sha256"
        local expected actual
        expected="$(awk '{print $1}' "$tmp/$name.sha256")"
        actual="$(sha 256 "$tmp/$name.tar.gz")"
        if [[ "$expected" != "$actual" ]]; then
            echo "foundry: checksum mismatch (expected $expected, got $actual)" >&2; exit 1
        fi
        rm -rf "$dest"; mkdir -p "$dest"
        tar -xzf "$tmp/$name.tar.gz" -C "$dest"
        echo "$FOUNDRY_VERSION" > "$dest/.version"
        rm -rf "$tmp"
        echo "  installed in $dest"
    fi

    # forge-std: the test library the contract's Solidity tests import. Vendored as plain files
    # (not a git submodule), so it does not touch whatever repository this project lives in.
    local lib="$ROOT/contracts/lib/forge-std"
    if [[ -f "$lib/src/Test.sol" && "$(cat "$lib/.version" 2>/dev/null)" == "$FORGE_STD_VERSION" ]]; then
        echo "forge-std $FORGE_STD_VERSION already in $lib"
    else
        echo "forge-std $FORGE_STD_VERSION"
        local tmp; tmp="$(mktemp -d)"
        fetch "https://github.com/foundry-rs/forge-std/archive/refs/tags/${FORGE_STD_VERSION}.tar.gz" "$tmp/forge-std.tar.gz"
        rm -rf "$lib"; mkdir -p "$lib"
        tar -xzf "$tmp/forge-std.tar.gz" -C "$lib" --strip-components 1
        echo "$FORGE_STD_VERSION" > "$lib/.version"
        rm -rf "$tmp"
        echo "  installed in $lib"
    fi
}

case "${1:-all}" in
    kafka) install_kafka ;;
    foundry) install_foundry ;;
    all) install_kafka; install_foundry ;;
    *) echo "usage: $0 [kafka|foundry|all]" >&2; exit 2 ;;
esac
