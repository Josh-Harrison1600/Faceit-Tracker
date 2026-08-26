from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.db import Store
from bot.faceit import FaceitClient, FaceitError, MatchResult, elo_delta_from_match

logger = logging.getLogger(__name__)

# FACEIT CS2 Season 9 (Americas / EU CS2 ranked). Local midnight in the bot timezone.
SEASON_START = datetime(2026, 8, 5)


def season_start_ts(timezone_name: str) -> int:
    tz = ZoneInfo(timezone_name)
    return int(SEASON_START.replace(tzinfo=tz).timestamp())


def skill_level_from_elo(elo: int) -> int:
    if elo <= 0:
        return 0
    for level, floor in (
        (10, 2001),
        (9, 1751),
        (8, 1531),
        (7, 1351),
        (6, 1201),
        (5, 1051),
        (4, 901),
        (3, 751),
        (2, 501),
        (1, 100),
    ):
        if elo >= floor:
            return level
    return 1


def lookup_match_elo(known: dict[str, int], match_id: str | None) -> int | None:
    if not match_id:
        return None
    if match_id in known:
        return known[match_id]
    if match_id.startswith("1-") and match_id[2:] in known:
        return known[match_id[2:]]
    return known.get(f"1-{match_id}")


async def refresh_player_peak(
    store: Store,
    faceit: FaceitClient,
    player_id: str,
    timezone_name: str,
    *,
    live_elo: int,
    full: bool = False,
) -> int:
    """Scan matchmaking ELO (full season or since last check) and persist if the player is on the roster."""
    tracked = await store.get_player(player_id)
    recorded = await store.get_recorded_peak(player_id) if tracked else None
    now_ts = int(datetime.now(timezone.utc).timestamp())

    if full or recorded is None:
        from_ts = season_start_ts(timezone_name)
        baseline = recorded.peak_elo if recorded else 0
    else:
        from_ts = recorded.checked_at
        baseline = recorded.peak_elo

    if from_ts >= now_ts:
        peak = max(baseline, live_elo)
    else:
        scanned = await scan_peak(faceit, player_id, from_ts=from_ts, to_ts=now_ts, live_elo=live_elo)
        peak = max(baseline, scanned, live_elo)

    if tracked:
        await store.set_recorded_peak(player_id, peak, now_ts)
        return max(peak, await store.peak_elo(player_id))
    return peak


async def scan_peak(
    faceit: FaceitClient,
    player_id: str,
    *,
    from_ts: int,
    to_ts: int,
    live_elo: int,
) -> int:
    """Highest matchmaking ELO we can recover in [from_ts, to_ts)."""
    peak = live_elo
    try:
        matches = await faceit.matchmaking_matches(player_id, from_ts, to_ts)
    except FaceitError as exc:
        logger.warning("Peak scan history failed for %s: %s", player_id, exc)
        return peak

    known = await _known_match_elos(faceit, player_id, from_ts, to_ts)
    for match in matches:
        exact = lookup_match_elo(known, match.match_id)
        if exact is not None:
            peak = max(peak, exact)

    missing = [
        match
        for match in matches
        if match.match_id and lookup_match_elo(known, match.match_id) is None
    ]
    if not missing:
        return peak

    reconstructed = await _reconstruct_peak(faceit, player_id, matches, known, live_elo)
    return max(peak, reconstructed)


async def _known_match_elos(
    faceit: FaceitClient, player_id: str, from_ts: int, to_ts: int
) -> dict[str, int]:
    known: dict[str, int] = {}
    try:
        items = await faceit.player_match_stats(player_id, from_ts, to_ts, max_offset=2000)
    except FaceitError as exc:
        logger.info("Peak scan stats failed for %s: %s", player_id, exc)
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        match_id, elo = _elo_from_stats_item(item)
        if match_id and elo is not None:
            known[match_id] = elo

    series = await faceit.player_elo_by_match(player_id)
    known.update(series)
    return known


def _elo_from_stats_item(item: dict) -> tuple[str | None, int | None]:
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    match_id = (
        stats.get("Match Id")
        or stats.get("Match ID")
        or stats.get("match_id")
        or item.get("match_id")
        or item.get("Match Id")
    )
    elo = None
    for source in (stats, item):
        for key in ("Elo", "ELO", "Faceit Elo", "FACEIT Elo", "elo", "faceit_elo"):
            value = source.get(key)
            if value in (None, ""):
                continue
            try:
                elo = int(float(value))
            except (TypeError, ValueError):
                continue
            break
        if elo is not None:
            break
    return (str(match_id) if match_id else None, elo)


async def _reconstruct_peak(
    faceit: FaceitClient,
    player_id: str,
    matches: list[MatchResult],
    known: dict[str, int],
    live_elo: int,
) -> int:
    """Walk newest→oldest from live ELO, subtracting each match's estimated delta."""
    ordered = sorted(
        [match for match in matches if match.match_id],
        key=lambda match: match.finished_at,
        reverse=True,
    )
    peak = live_elo
    elo_after = live_elo
    for match in ordered:
        exact = lookup_match_elo(known, match.match_id)
        if exact is not None:
            peak = max(peak, exact)
            elo_after = exact
            delta = await _match_delta(faceit, player_id, match)
            if delta is not None:
                elo_after = exact - delta
            continue
        peak = max(peak, elo_after)
        delta = await _match_delta(faceit, player_id, match)
        if delta is None:
            continue
        elo_after -= delta
        peak = max(peak, elo_after)
    return peak


async def _match_delta(faceit: FaceitClient, player_id: str, match: MatchResult) -> int | None:
    if not match.match_id:
        return None
    try:
        data = await faceit.get_match(match.match_id)
    except FaceitError as exc:
        logger.warning("Peak scan match %s failed: %s", match.match_id, exc)
        return None
    await asyncio.sleep(0.2)
    return elo_delta_from_match(data, player_id, match.won)
