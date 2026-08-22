from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.db import Store, TrackedPlayer
from bot.faceit import FaceitClient, FaceitError, FaceitNotFound, MatchResult

logger = logging.getLogger(__name__)

DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


@dataclass
class DayLine:
    name: str
    future: bool = False
    before_tracking: bool = False
    maps: int = 0
    wins: int = 0
    losses: int = 0
    elo_delta: int | None = None

    @property
    def na(self) -> bool:
        return self.future or self.before_tracking


@dataclass
class PlayerWeek:
    player_id: str
    nickname: str
    start_elo: int | None
    start_level: int | None
    end_elo: int | None
    end_level: int | None
    days: list[DayLine] = field(default_factory=list)
    started_on: str | None = None
    calibrating: bool = False
    peak_elo: int | None = None
    peak_level: int | None = None
    current_elo: int | None = None
    current_level: int | None = None
    error: str | None = None

    @property
    def total_wins(self) -> int:
        return sum(day.wins for day in self.days if not day.na)

    @property
    def total_losses(self) -> int:
        return sum(day.losses for day in self.days if not day.na)

    @property
    def total_elo_delta(self) -> int | None:
        if self.start_elo is None or self.end_elo is None:
            return None
        if self.calibrating:
            return None
        return self.end_elo - self.start_elo


def current_week_start(now: datetime) -> datetime:
    local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_sunday = (local.weekday() + 1) % 7
    return local - timedelta(days=days_since_sunday)


def completed_week_start(now: datetime) -> datetime:
    return current_week_start(now) - timedelta(days=7)


def _parse_added_at(raw: str, tz) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


async def snapshot_all_players(store: Store, faceit: FaceitClient) -> None:
    players = await store.list_players()
    for index, player in enumerate(players):
        if index:
            await asyncio.sleep(0.2)
        try:
            profile = await faceit.get_player(player.player_id)
        except FaceitError:
            logger.warning("Midnight snapshot skipped for %s", player.nickname)
            continue
        if profile.nickname != player.nickname:
            await store.update_nickname(player.player_id, profile.nickname)
        await store.add_snapshot(profile.player_id, profile.elo, profile.level)


async def collect_week_stats(
    store: Store,
    faceit: FaceitClient,
    timezone_name: str,
    week_start: datetime,
) -> list[PlayerWeek]:
    tz = ZoneInfo(timezone_name)
    if week_start.tzinfo is None:
        week_start = week_start.replace(tzinfo=tz)
    else:
        week_start = week_start.astimezone(tz)

    now = datetime.now(tz)
    week_end = week_start + timedelta(days=7)
    from_ts = int(week_start.timestamp())
    to_ts = int(min(now, week_end).timestamp())

    players = await store.list_players()
    results: list[PlayerWeek] = []
    for index, player in enumerate(players):
        if index:
            await asyncio.sleep(0.2)
        results.append(
            await _player_week(
                store,
                faceit,
                player,
                week_start=week_start,
                week_end=week_end,
                now=now,
                from_ts=from_ts,
                to_ts=to_ts,
            )
        )
    return results


async def collect_week_for_player(
    store: Store,
    faceit: FaceitClient,
    timezone_name: str,
    week_start: datetime,
    player: TrackedPlayer,
) -> PlayerWeek:
    tz = ZoneInfo(timezone_name)
    if week_start.tzinfo is None:
        week_start = week_start.replace(tzinfo=tz)
    else:
        week_start = week_start.astimezone(tz)
    now = datetime.now(tz)
    week_end = week_start + timedelta(days=7)
    return await _player_week(
        store,
        faceit,
        player,
        week_start=week_start,
        week_end=week_end,
        now=now,
        from_ts=int(week_start.timestamp()),
        to_ts=int(min(now, week_end).timestamp()),
    )


async def _player_week(
    store: Store,
    faceit: FaceitClient,
    player: TrackedPlayer,
    *,
    week_start: datetime,
    week_end: datetime,
    now: datetime,
    from_ts: int,
    to_ts: int,
) -> PlayerWeek:
    added_at = _parse_added_at(player.added_at, week_start.tzinfo)
    try:
        profile = await faceit.get_player(player.player_id)
    except FaceitNotFound:
        return PlayerWeek(
            player_id=player.player_id,
            nickname=player.nickname,
            start_elo=None,
            start_level=None,
            end_elo=None,
            end_level=None,
            error="FACEIT profile not found",
        )
    except FaceitError as exc:
        logger.warning("Failed to fetch %s: %s", player.nickname, exc)
        return PlayerWeek(
            player_id=player.player_id,
            nickname=player.nickname,
            start_elo=None,
            start_level=None,
            end_elo=None,
            end_level=None,
            error="Could not fetch FACEIT data",
        )

    if profile.nickname != player.nickname and await store.get_player(player.player_id):
        await store.update_nickname(player.player_id, profile.nickname)

    matches: list[MatchResult] = []
    if to_ts > from_ts:
        try:
            matches = await faceit.matchmaking_matches(player.player_id, from_ts, to_ts)
        except FaceitError as exc:
            logger.warning("Failed to fetch match history for %s: %s", profile.nickname, exc)
            return PlayerWeek(
                player_id=player.player_id,
                nickname=profile.nickname,
                start_elo=None,
                start_level=None,
                end_elo=None,
                end_level=None,
                error="Could not fetch match history",
            )

    start_snap = await store.snapshot_near(player.player_id, int(week_start.timestamp()))
    if start_snap is None:
        start_snap = await store.first_snapshot_after(player.player_id, int(week_start.timestamp()))
    if start_snap is None:
        start_snap = await store.latest_snapshot(player.player_id)

    started_on: str | None = None
    if added_at is not None and added_at > week_start + timedelta(hours=1):
        started_on = DAY_NAMES[(added_at.weekday() + 1) % 7]

    calibrating = profile.elo == 0 and profile.level == 0
    week_complete = now >= week_end
    if week_complete:
        end_snap = await store.snapshot_near(player.player_id, int(week_end.timestamp()))
        end_elo = end_snap.elo if end_snap else profile.elo
        end_level = end_snap.level if end_snap else profile.level
    else:
        end_elo = profile.elo
        end_level = profile.level

    days: list[DayLine] = []
    for index, name in enumerate(DAY_NAMES):
        day_start = week_start + timedelta(days=index)
        day_end = day_start + timedelta(days=1)
        days.append(
            _build_day(
                name=name,
                day_start=day_start,
                day_end=day_end,
                now=now,
                added_at=added_at,
                matches=matches,
            )
        )

    # Resolve elo deltas after store lookups (async per day)
    resolved: list[DayLine] = []
    for index, day in enumerate(days):
        if day.na:
            resolved.append(day)
            continue
        day_start = week_start + timedelta(days=index)
        day_end = day_start + timedelta(days=1)
        elo_delta = await _day_elo_delta(
            store,
            player.player_id,
            day_start=day_start,
            day_end=day_end,
            now=now,
            current_elo=profile.elo,
            calibrating=calibrating,
        )
        resolved.append(
            DayLine(
                name=day.name,
                maps=day.maps,
                wins=day.wins,
                losses=day.losses,
                elo_delta=elo_delta,
            )
        )

    return PlayerWeek(
        player_id=player.player_id,
        nickname=profile.nickname,
        start_elo=None if calibrating else (start_snap.elo if start_snap else profile.elo),
        start_level=None if calibrating else (start_snap.level if start_snap else profile.level),
        end_elo=None if calibrating else end_elo,
        end_level=None if calibrating else end_level,
        days=resolved,
        started_on=started_on,
        calibrating=calibrating,
        peak_elo=max(profile.elo, await store.peak_elo(player.player_id)),
        peak_level=max(profile.level, await store.peak_level(player.player_id)),
        current_elo=None if calibrating else profile.elo,
        current_level=None if calibrating else profile.level,
    )


def _build_day(
    *,
    name: str,
    day_start: datetime,
    day_end: datetime,
    now: datetime,
    added_at: datetime | None,
    matches: list[MatchResult],
) -> DayLine:
    if day_start >= now:
        return DayLine(name=name, future=True)
    if added_at is not None and added_at >= day_end:
        return DayLine(name=name, before_tracking=True)

    wins = 0
    losses = 0
    start_ts = int(day_start.timestamp())
    end_ts = int(day_end.timestamp())
    for match in matches:
        if match.finished_at < start_ts or match.finished_at >= end_ts:
            continue
        if match.won:
            wins += 1
        else:
            losses += 1
    return DayLine(name=name, maps=wins + losses, wins=wins, losses=losses)


async def _day_elo_delta(
    store: Store,
    player_id: str,
    *,
    day_start: datetime,
    day_end: datetime,
    now: datetime,
    current_elo: int,
    calibrating: bool,
) -> int | None:
    if calibrating:
        return None
    start_snap = await store.snapshot_near(player_id, int(day_start.timestamp()))
    if start_snap is None:
        return None
    if now < day_end:
        return current_elo - start_snap.elo
    end_snap = await store.snapshot_near(player_id, int(day_end.timestamp()))
    if end_snap is None:
        return None
    return end_snap.elo - start_snap.elo
