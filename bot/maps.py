from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.db import Store
from bot.faceit import FaceitClient, FaceitError, MatchResult, player_row_from_match_stats

logger = logging.getLogger(__name__)

PRIOR_MATCH_LOOKBACK = 14 * 24 * 3600


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
    elo: int | None = None
    elo_delta: int | None = None
    score: str | None = None
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


def _unix_seconds(raw: int | None) -> int | None:
    if raw is None:
        return None
    if raw > 10_000_000_000:
        return raw // 1000
    return raw


def _in_window(stats: MapStats, from_ts: int, to_ts: int) -> bool:
    ts = stats.finished_at or 0
    return from_ts <= ts < to_ts


def _normalize_score(raw: str | None) -> str | None:
    if raw is None:
        return None
    match = re.match(r"^(\d+)\s*[/:-]\s*(\d+)$", raw.strip())
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


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
        elo=_as_int(_stat(stats, "Elo", "ELO", "Faceit Elo", "FACEIT Elo"))
        or _as_int(_stat(item, "Elo", "elo", "faceit_elo")),
        score=_normalize_score(_stat(stats, "Score", "Final Score", "Match Score")),
        finished_at=_unix_seconds(_as_int(_stat(stats, "Match Finished At", "Finished At"))),
        match_id=match_id or _stat(stats, "Match Id", "Match ID", "match_id"),
    )


def format_map_block(stats: MapStats) -> str:
    if stats.won is True:
        result = "Win"
    elif stats.won is False:
        result = "Loss"
    else:
        result = "?"
    lines = [f"{stats.map_name} - {result}"]
    if stats.score:
        lines.append(f"Score: {stats.score}")
    if stats.elo_delta is not None:
        delta = stats.elo_delta
        sign = "+" if delta > 0 else ""
        lines.append(f"ELO {sign}{delta}")
    if stats.kills is not None and stats.deaths is not None:
        kd_str = f"{stats.kd:.2f}" if stats.kd is not None else "—"
        lines.append(f"K/D {stats.kills}/{stats.deaths} ({kd_str})")
    if stats.adr is not None:
        lines.append(f"ADR {stats.adr:.0f}")
    if stats.hs_percent is not None:
        lines.append(f"HS {stats.hs_percent:.0f}%")
    if stats.kpr is not None:
        lines.append(f"KPR {stats.kpr:.2f}")
    if stats.rating is not None:
        lines.append(f"Rating {stats.rating:.2f}")
    if stats.utility_damage is not None:
        lines.append(f"Util dmg {stats.utility_damage:.0f}")
    if stats.flashes_thrown is not None and stats.enemies_blinded is not None:
        lines.append(f"Flashes {stats.flashes_thrown} thrown / {stats.enemies_blinded} blinded")
    elif stats.flashes_thrown is not None:
        lines.append(f"Flashes {stats.flashes_thrown} thrown")
    elif stats.enemies_blinded is not None:
        lines.append(f"Flashes {stats.enemies_blinded} blinded")
    return "\n".join(lines)


def _apply_elo_deltas(maps: list[MapStats]) -> None:
    previous_elo: int | None = None
    for stats in maps:
        if previous_elo is not None and stats.elo is not None:
            stats.elo_delta = stats.elo - previous_elo
        if stats.elo is not None:
            previous_elo = stats.elo


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
    current_elo: int | None = None
    try:
        profile = await faceit.get_player(player_id)
        current_elo = profile.elo or None
    except FaceitError:
        logger.warning("Could not fetch live ELO for last map %s", nickname)
    return await _player_maps(
        faceit, player_id, nickname, from_ts, now, limit=1, current_elo=current_elo
    )


async def _player_maps(
    faceit: FaceitClient,
    player_id: str,
    nickname: str,
    from_ts: int,
    to_ts: int,
    *,
    limit: int | None = None,
    current_elo: int | None = None,
) -> PlayerMaps:
    lookback_from = from_ts - PRIOR_MATCH_LOOKBACK
    try:
        matches = await faceit.matchmaking_matches(player_id, lookback_from, to_ts)
    except FaceitError as exc:
        logger.warning("Match history failed for %s: %s", nickname, exc)
        return PlayerMaps(nickname=nickname, maps=[], error="Could not fetch matches")

    if not matches:
        return PlayerMaps(nickname=nickname, maps=[])

    try:
        raw_stats = await faceit.player_match_stats(player_id, lookback_from - 120, to_ts + 120)
    except FaceitError as exc:
        logger.warning("Match stats failed for %s: %s", nickname, exc)
        raw_stats = []

    by_id = {}
    for item in raw_stats:
        mid = _item_match_id(item)
        if mid:
            by_id[mid] = item

    matches.sort(key=lambda item: item.finished_at)
    all_maps = [_map_from_match(match, by_id.get(match.match_id or "")) for match in matches]
    all_maps.sort(key=lambda item: item.finished_at or 0)

    maps = [item for item in all_maps if _in_window(item, from_ts, to_ts)]
    if limit:
        maps = maps[-limit:]
    needed = {item.match_id for item in maps if item.match_id}
    if maps:
        prior = next(
            (
                item
                for item in reversed(all_maps)
                if (item.finished_at or 0) < (maps[0].finished_at or 0)
            ),
            None,
        )
        if prior and prior.match_id:
            needed.add(prior.match_id)

    displayed_ids = {item.match_id for item in maps if item.match_id}
    for stats in all_maps:
        if not stats.match_id or stats.match_id not in needed:
            continue
        missing_core = stats.map_name == "Unknown"
        missing_score = stats.match_id in displayed_ids and not stats.score
        if not missing_core and not missing_score:
            continue
        item = await _match_stats_row(faceit, stats.match_id, player_id)
        if item is None:
            continue
        parsed = parse_map_stats(item, won=stats.won, match_id=stats.match_id)
        history_finished = stats.finished_at
        _merge_map_stats(stats, parsed)
        stats.finished_at = history_finished

    await _fill_match_elo(faceit, player_id, all_maps)
    if current_elo is not None and all_maps:
        newest = max(all_maps, key=lambda item: item.finished_at or 0)
        if newest.elo is None:
            newest.elo = current_elo
    _apply_elo_deltas(all_maps)
    maps = [item for item in all_maps if _in_window(item, from_ts, to_ts)]
    if limit:
        maps = maps[-limit:]
    return PlayerMaps(nickname=nickname, maps=maps)


def _map_from_match(match: MatchResult, item: dict | None) -> MapStats:
    if item is None:
        return MapStats(
            map_name="Unknown",
            won=match.won,
            finished_at=match.finished_at,
            match_id=match.match_id,
        )
    parsed = parse_map_stats(item, won=match.won, match_id=match.match_id)
    parsed.finished_at = match.finished_at
    return parsed


def _merge_map_stats(base: MapStats, extra: MapStats) -> None:
    for field in fields(MapStats):
        current = getattr(base, field.name)
        incoming = getattr(extra, field.name)
        if field.name in {"map_name", "finished_at"}:
            if field.name == "map_name" and current == "Unknown" and incoming and incoming != "Unknown":
                setattr(base, field.name, incoming)
            continue
        if current is None and incoming is not None:
            setattr(base, field.name, incoming)


async def _fill_match_elo(
    faceit: FaceitClient, player_id: str, maps: list[MapStats]
) -> None:
    if all(item.elo is not None or not item.match_id for item in maps):
        return
    series = await faceit.player_elo_by_match(player_id)
    if not series:
        return
    for stats in maps:
        if stats.elo is not None or not stats.match_id:
            continue
        stats.elo = series.get(stats.match_id)
        if stats.elo is None and stats.match_id.startswith("1-"):
            stats.elo = series.get(stats.match_id[2:])
        elif stats.elo is None:
            stats.elo = series.get(f"1-{stats.match_id}")


async def _match_stats_row(
    faceit: FaceitClient, match_id: str, player_id: str
) -> dict | None:
    try:
        data = await faceit.match_stats(match_id)
    except FaceitError as exc:
        logger.warning("Match stats fallback failed for %s: %s", match_id, exc)
        return None
    return player_row_from_match_stats(data, player_id)
