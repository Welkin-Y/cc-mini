"""Deterministic companion generation from user ID.

Port of claude-code-main/src/buddy/companion.ts

The key invariant: same userId always produces the same CompanionBones.
Bones are never persisted — they're regenerated from hash(userId) on every
read, so species renames can't break stored companions and users can't
edit their way to a legendary.
"""
from __future__ import annotations

import getpass
import math
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Sequence

from .types import (
    EYES,
    HATS,
    RARITIES,
    RARITY_FLOOR,
    RARITY_WEIGHTS,
    SPECIES,
    STAT_NAMES,
    Companion,
    CompanionBones,
    StoredCompanion,
)

_MASK = 0xFFFFFFFF  # 32-bit unsigned mask


# ---------------------------------------------------------------------------
# Mulberry32 — tiny seeded PRNG, good enough for picking ducks
# Exact port of companion.ts lines 16-25
# ---------------------------------------------------------------------------

def mulberry32(seed: int) -> Callable[[], float]:
    a = seed & _MASK

    def _next() -> float:
        nonlocal a
        a = (a | 0) & _MASK
        a = (a + 0x6D2B79F5) & _MASK
        t = ((a ^ (a >> 15)) * (1 | a)) & _MASK
        t = (t + (((t ^ (t >> 7)) * (61 | t)) & _MASK)) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296

    return _next


# ---------------------------------------------------------------------------
# FNV-1a hash (non-Bun branch of companion.ts lines 27-37)
# ---------------------------------------------------------------------------

def hash_string(s: str) -> int:
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        # Math.imul emulation: multiply then mask to 32-bit signed, then unsigned
        h = (h * 16777619) & _MASK
    return h & _MASK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pick(rng: Callable[[], float], arr: Sequence) -> object:
    return arr[int(rng() * len(arr))]


def roll_rarity(rng: Callable[[], float]) -> str:
    total = sum(RARITY_WEIGHTS.values())
    r = rng() * total
    for rarity in RARITIES:
        r -= RARITY_WEIGHTS[rarity]
        if r < 0:
            return rarity
    return 'common'


def roll_stats(rng: Callable[[], float], rarity: str) -> dict[str, int]:
    """One peak stat, one dump stat, rest scattered. Rarity bumps the floor."""
    floor = RARITY_FLOOR[rarity]
    peak = pick(rng, STAT_NAMES)
    dump = pick(rng, STAT_NAMES)
    while dump == peak:
        dump = pick(rng, STAT_NAMES)

    stats: dict[str, int] = {}
    for name in STAT_NAMES:
        if name == peak:
            stats[name] = min(100, floor + 50 + int(rng() * 30))
        elif name == dump:
            stats[name] = max(1, floor - 10 + int(rng() * 15))
        else:
            stats[name] = floor + int(rng() * 40)
    return stats


# ---------------------------------------------------------------------------
# Roll
# ---------------------------------------------------------------------------

SALT = 'friend-2026-401'


@dataclass(frozen=True)
class Roll:
    bones: CompanionBones
    inspiration_seed: int


def _roll_from(rng: Callable[[], float]) -> Roll:
    rarity = roll_rarity(rng)
    bones = CompanionBones(
        rarity=rarity,
        species=pick(rng, SPECIES),
        eye=pick(rng, EYES),
        hat='none' if rarity == 'common' else pick(rng, HATS),
        shiny=rng() < 0.01,
        stats=roll_stats(rng, rarity),
    )
    return Roll(bones=bones, inspiration_seed=int(rng() * 1e9))


@lru_cache(maxsize=1)
def roll(user_id: str) -> Roll:
    key = user_id + SALT
    return _roll_from(mulberry32(hash_string(key)))


def roll_with_seed(seed: str) -> Roll:
    return _roll_from(mulberry32(hash_string(seed)))


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------

def companion_user_id() -> str:
    """Derive a stable user identity for companion generation.

    Since mini-claude has no OAuth, use username@hostname as the seed.
    Same user on same machine always gets the same companion.

    Set CC_MINI_BUDDY_SEED env var to override (useful for testing).
    """
    import os
    override = os.environ.get('CC_MINI_BUDDY_SEED')
    if override:
        return override
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return 'anon'


# ---------------------------------------------------------------------------
# Get companion (merge stored soul + regenerated bones)
# ---------------------------------------------------------------------------

def get_companion() -> Companion | None:
    """Get the full companion if one has been hatched, or None."""
    from .storage import load_stored_companion

    stored = load_stored_companion()
    if stored is None:
        return None
    bones = roll(companion_user_id()).bones
    return Companion(
        rarity=bones.rarity,
        species=bones.species,
        eye=bones.eye,
        hat=bones.hat,
        shiny=bones.shiny,
        stats=bones.stats,
        name=stored.name,
        personality=stored.personality,
        hatched_at=stored.hatched_at,
    )
