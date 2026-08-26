from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.db import Store
from bot.faceit import FaceitClient, FaceitError

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


def _expand_match_ids(ids: set[str]) -> set[str]:
    out = set(ids)
    for match_id in ids:
        if match_id.startswith("1-"):
            out.add(match_id[2:])
        else:
            out.add(f"1-{match_id}")
    return out


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
        baseline = 0
    else:
        from_ts = recorded.checked_at
        baseline = recorded.peak_elo

    if from_ts >= now_ts:
        peak = max(baseline, live_elo)
    else:
        scanned = await scan_peak(faceit, player_id, from_ts=from_ts, to_ts=now_ts, live_elo=live_elo)
        peak = max(baseline, scanned, live_elo)

    if tracked:
        await store.set_recorded_peak(player_id, peak, now_ts, replace=full)
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
    """Highest matchmaking ELO FACEIT recorded in [from_ts, to_ts)."""
    peak = live_elo
    try:
        matches = await faceit.matchmaking_matches(player_id, from_ts, to_ts)
    except FaceitError as exc:
        logger.warning("Peak scan history failed for %s: %s", player_id, exc)
        return peak

    season_ids = _expand_match_ids({match.match_id for match in matches if match.match_id})
    try:
        rows = await faceit.player_match_elos(player_id)
    except FaceitError as exc:
        logger.warning("Peak scan elo history failed for %s: %s", player_id, exc)
        return peak

    for row in rows:
        if row.match_id not in season_ids:
            continue
        peak = max(peak, row.elo)
        if row.elo_delta is not None:
            peak = max(peak, row.elo - row.elo_delta)
    return peak
