"""Merkle trees over SHA-256, as specified for Certificate Transparency (RFC 6962 / RFC 9162).

A compliance report commits to its alerts through the root of such a tree.
Anchoring the root lets anyone later prove that one particular alert was part
of the report, by showing a handful of hashes, without disclosing the others.

The construction is the standard one, chosen because it is specified to the
byte, needs nothing beyond SHA-256 (which the EVM provides as a precompile),
and cannot be fooled by presenting an interior node as a leaf: leaves and
nodes are hashed with different prefixes.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


def leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(LEAF_PREFIX + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def _split(n: int) -> int:
    """The largest power of two strictly less than ``n``."""
    return 1 << ((n - 1).bit_length() - 1)


def root_of(leaves: Sequence[bytes]) -> bytes:
    """Merkle tree hash of already-hashed leaves; the hash of the empty string for an empty tree."""
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaves[0]
    k = _split(n)
    return node_hash(root_of(leaves[:k]), root_of(leaves[k:]))


def merkle_root(items: Sequence[bytes]) -> bytes:
    """Merkle tree hash of raw items."""
    return root_of([leaf_hash(item) for item in items])


def inclusion_proof(leaves: Sequence[bytes], index: int) -> list[bytes]:
    """The audit path for the leaf at ``index``: sibling hashes from the leaf up to the root."""
    n = len(leaves)
    if not 0 <= index < n:
        raise IndexError("leaf index out of range")
    if n == 1:
        return []
    k = _split(n)
    if index < k:
        return inclusion_proof(leaves[:k], index) + [root_of(leaves[k:])]
    return inclusion_proof(leaves[k:], index - k) + [root_of(leaves[:k])]


def verify_inclusion(leaf: bytes, index: int, size: int, proof: Sequence[bytes], root: bytes) -> bool:
    """Check an audit path (RFC 9162, section 2.1.3.2). ``leaf`` is the leaf hash.

    The smart contract runs the same steps on the same kinds of value, 32-byte
    hashes and unsigned integers, so a proof accepted here is accepted on
    chain. Anything the contract could not even be asked is refused.
    """
    if not 0 <= index < size:
        return False
    if any(len(value) != 32 for value in (leaf, root, *proof)):
        return False
    node, last, current = index, size - 1, leaf
    for sibling in proof:
        if last == 0:
            return False
        if node & 1 or node == last:
            current = node_hash(sibling, current)
            if not node & 1:
                while node and not node & 1:
                    node >>= 1
                    last >>= 1
        else:
            current = node_hash(current, sibling)
        node >>= 1
        last >>= 1
    return last == 0 and current == root
