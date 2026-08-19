from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.db import Store
from bot.faceit import FaceitClient, FaceitError, player_row_from_match_stats

logger = logging.getLogger(__name__)


@dataclass
class MapStats:
    map_name: str
    won: bool | None
    kills: int | None = None
    deaths: int | None = None
    kd: float | None = None
    adr: float | None = None
    hs_percent: float | None = None
    kpr: float | None = None
    rating: float | None = None
    utility_damage: float | None = None
    flashes_thrown: int | None = None
    enemies_blinded: int | None = None
    finished_at: int | None = None
    match_id: str | None = None


@dataclass
class PlayerMaps:
    nickname: str
    maps: list[MapStats]
    error: str | None = None


def _stat(stats: dict, *names: str) -> str | None:
    lower = {str(key).lower(): value for key, value in stats.items()}
    for name in names:
        value = lower.get(name.lower())
        if value is not None and str(value) != "":
            return str(value)
    return None


def _as_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(float(raw.replace("%", "")))
    except ValueError:
        return None


def _as_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace("%", ""))
    except ValueError:
        return None


def parse_map_stats(item: dict, *, won: bool | None = None, match_id: str | None = None) -> MapStats:
    stats = item.get("stats") or item
    map_name = _stat(stats, "Map", "Map Name") or "Unknown"
    map_name = map_name.replace("de_", "").replace("_", " ").title()
    kills = _as_int(_stat(stats, "Kills"))
    deaths = _as_int(_stat(stats, "Deaths"))
    kd = _as_float(_stat(stats, "K/D Ratio", "K/D", "KD"))
    if kd is None and kills is not None and deaths is not None:
        kd = round(kills / deaths, 2) if deaths else float(kills)
    result = _stat(stats, "Result")
    if won is None and result is not None:
        won = result in {"1", "true", "Win", "win", "W"}
    return MapStats(
        map_name=map_name,
        won=won,
        kills=kills,
        deaths=deaths,
        kd=kd,
        adr=_as_float(_stat(stats, "ADR", "Average Damage per Round")),
        hs_percent=_as_float(_stat(stats, "Headshots %", "Headshots%", "HS %", "HS%")),
        kpr=_as_float(_stat(stats, "K/R Ratio", "K/R", "Kills per Round")),
        rating=_as_float(_stat(stats, "Rating", "HLTV Rating", "HLTV", "Player Rating")),
        utility_damage=_as_float(_stat(stats, "Utility Damage", "Total Utility Damage", "UD")),
        flashes_thrown=_as_int(
            _stat(stats, "Flash Count", "Flashes", "Flashes Thrown", "Total Flash Count", "Utility Flashes")
        ),
        enemies_blinded=_as_int(
            _stat(stats, "Enemies Flashed", "Enemies Blinded", "Flash Successes", "Total Enemies Flashed")
        ),
        finished_at=_as_int(_stat(stats, "Match Finished At", "Finished At")),
        match_id=match_id or _stat(stats, "Match Id", "Match ID", "match_id"),
    )


def format_map_block(stats: MapStats) -> str:
    result = "W" if stats.won else "L" if stats.won is False else "?"
    kd_part = "K/D —"
    if stats.kills is not None and stats.deaths is not None:
        kd_str = f"{stats.kd:.2f}" if stats.kd is not None else "—"
        kd_part = f"K/D {stats.kills}/{stats.deaths} ({kd_str})"
    bits = [kd_part]
    if stats.adr is not None:
        bits.append(f"ADR {stats.adr:.0f}")
    if stats.hs_percent is not None:
        bits.append(f"HS {stats.hs_percent:.0f}%")
    if stats.kpr is not None:
        bits.append(f"KPR {stats.kpr:.2f}")
    line2 = "  ·  ".join(bits)
    extra: list[str] = []
    if stats.rating is not None:
        extra.append(f"Rating {stats.rating:.2f}")
    if stats.utility_damage is not None:
        extra.append(f"Util dmg {stats.utility_damage:.0f}")
    if stats.flashes_thrown is not None and stats.enemies_blinded is not None:
        extra.append(f"Flashes {stats.flashes_thrown} thrown / {stats.enemies_blinded} blinded")
    elif stats.flashes_thrown is not None:
        extra.append(f"Flashes {stats.flashes_thrown} thrown")
    lines = [f"{stats.map_name}  {result}", line2]
    if extra:
        lines.append("  ·  ".join(extra))
    return "\n".join(lines)


def _item_match_id(item: dict) -> str | None:
    stats = item.get("stats") or item
    return _stat(stats, "Match Id", "Match ID", "matchId", "match_id") or _stat(
        item, "Match Id", "Match ID", "matchId", "match_id"
    )


async def collect_day_maps(
    store: Store,
    faceit: FaceitClient,
    timezone_name: str,
    day_start: datetime,
) -> list[PlayerMaps]:
    tz = ZoneInfo(timezone_name)
    if day_start.tzinfo is None:
        day_start = day_start.replace(tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    from_ts = int(day_start.timestamp())
    to_ts = int(min(datetime.now(tz), day_end).timestamp())
    players = await store.list_players()
    results: list[PlayerMaps] = []
    for index, player in enumerate(players):
        if index:
            await asyncio.sleep(0.2)
        results.append(await _player_maps(faceit, player.player_id, player.nickname, from_ts, to_ts))
    return results


async def last_map_for(
    faceit: FaceitClient, player_id: str, nickname: str
) -> PlayerMaps:
    now = int(datetime.now(timezone.utc).timestamp())
    from_ts = now - 60 * 60 * 24 * 60
    return await _player_maps(faceit, player_id, nickname, from_ts, now, limit=1)


async def _player_maps(
    faceit: FaceitClient,
    player_id: str,
    nickname: str,
    from_ts: int,
    to_ts: int,
    *,
    limit: int | None = None,
) -> PlayerMaps:
    try:
        matches = await faceit.matchmaking_matches(player_id, from_ts, to_ts)
    except FaceitError as exc:
        logger.warning("Match history failed for %s: %s", nickname, exc)
        return PlayerMaps(nickname=nickname, maps=[], error="Could not fetch matches")

    if not matches:
        return PlayerMaps(nickname=nickname, maps=[])

    matches.sort(key=lambda item: item.finished_at, reverse=True)
    if limit:
        matches = matches[:limit]

        try:
            raw_stats = await faceit.player_match_stats(player_id, from_ts - 120, to_ts + 120)
        except FaceitError as exc:
            logger.warning("Match stats failed for %s: %s", nickname, exc)
            raw_stats = []

    by_id = {}
    for item in raw_stats:
        mid = _item_match_id(item)
        if mid:
            by_id[mid] = item

    maps: list[MapStats] = []
    for match in matches:
        item = by_id.get(match.match_id or "")
        if item is None and match.match_id:
            item = await _match_stats_row(faceit, match.match_id, player_id)
        if item is None:
            maps.append(
                MapStats(
                    map_name="Unknown",
                    won=match.won,
                    finished_at=match.finished_at,
                    match_id=match.match_id,
                )
            )
            continue
        parsed = parse_map_stats(item, won=match.won, match_id=match.match_id)
        if parsed.finished_at is None:
            parsed.finished_at = match.finished_at
        maps.append(parsed)

    maps.sort(key=lambda item: item.finished_at or 0)
    return PlayerMaps(nickname=nickname, maps=maps)


async def _match_stats_row(
    faceit: FaceitClient, match_id: str, player_id: str
) -> dict | None:
    try:
        data = await faceit.match_stats(match_id)
    except FaceitError as exc:
        logger.warning("Match stats fallback failed for %s: %s", match_id, exc)
        return None
    return player_row_from_match_stats(data, player_id)
