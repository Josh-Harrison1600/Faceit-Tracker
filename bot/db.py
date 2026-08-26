from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SETTING_CHANNEL_ID = "channel_id"
SETTING_WEEKLY_CHANNEL_ID = "weekly_channel_id"
SETTING_DAILY_CHANNEL_ID = "daily_channel_id"


@dataclass(frozen=True)
class TrackedPlayer:
    player_id: str
    nickname: str
    added_at: str


@dataclass(frozen=True)
class Snapshot:
    player_id: str
    taken_at: int
    elo: int
    level: int


@dataclass(frozen=True)
class EloPeak:
    player_id: str
    peak_elo: int
    checked_at: int


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._init_schema()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not connected")
        return self._db

    async def _init_schema(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                player_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id TEXT NOT NULL,
                taken_at INTEGER NOT NULL,
                elo INTEGER NOT NULL,
                level INTEGER NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(player_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_player_taken
                ON snapshots (player_id, taken_at DESC);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS elo_peaks (
                player_id TEXT PRIMARY KEY,
                peak_elo INTEGER NOT NULL,
                checked_at INTEGER NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(player_id) ON DELETE CASCADE
            );
            """
        )
        await self.db.commit()

    async def list_players(self) -> list[TrackedPlayer]:
        cursor = await self.db.execute(
            "SELECT player_id, nickname, added_at FROM players ORDER BY nickname COLLATE NOCASE"
        )
        rows = await cursor.fetchall()
        return [
            TrackedPlayer(row["player_id"], row["nickname"], row["added_at"]) for row in rows
        ]

    async def get_player(self, player_id: str) -> TrackedPlayer | None:
        cursor = await self.db.execute(
            "SELECT player_id, nickname, added_at FROM players WHERE player_id = ?",
            (player_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return TrackedPlayer(row["player_id"], row["nickname"], row["added_at"])

    async def find_player_by_nickname(self, nickname: str) -> TrackedPlayer | None:
        cursor = await self.db.execute(
            "SELECT player_id, nickname, added_at FROM players WHERE nickname = ? COLLATE NOCASE",
            (nickname,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return TrackedPlayer(row["player_id"], row["nickname"], row["added_at"])

    async def add_player(self, player_id: str, nickname: str) -> bool:
        """Insert a player. Returns False if they were already tracked."""
        added_at = datetime.now(timezone.utc).isoformat()
        try:
            await self.db.execute(
                "INSERT INTO players (player_id, nickname, added_at) VALUES (?, ?, ?)",
                (player_id, nickname, added_at),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_player(self, player_id: str) -> bool:
        cursor = await self.db.execute("DELETE FROM players WHERE player_id = ?", (player_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def update_nickname(self, player_id: str, nickname: str) -> None:
        await self.db.execute(
            "UPDATE players SET nickname = ? WHERE player_id = ?",
            (nickname, player_id),
        )
        await self.db.commit()

    async def add_snapshot(self, player_id: str, elo: int, level: int, taken_at: int | None = None) -> None:
        if taken_at is None:
            taken_at = int(datetime.now(timezone.utc).timestamp())
        await self.db.execute(
            "INSERT INTO snapshots (player_id, taken_at, elo, level) VALUES (?, ?, ?, ?)",
            (player_id, taken_at, elo, level),
        )
        await self.db.commit()

    async def latest_snapshot(self, player_id: str) -> Snapshot | None:
        cursor = await self.db.execute(
            """
            SELECT player_id, taken_at, elo, level
            FROM snapshots
            WHERE player_id = ?
            ORDER BY taken_at DESC, id DESC
            LIMIT 1
            """,
            (player_id,),
        )
        return self._snapshot_from_row(await cursor.fetchone())

    async def snapshot_at_or_before(self, player_id: str, ts: int) -> Snapshot | None:
        cursor = await self.db.execute(
            """
            SELECT player_id, taken_at, elo, level
            FROM snapshots
            WHERE player_id = ? AND taken_at <= ?
            ORDER BY taken_at DESC, id DESC
            LIMIT 1
            """,
            (player_id, ts),
        )
        return self._snapshot_from_row(await cursor.fetchone())

    async def snapshot_near(self, player_id: str, ts: int, within: int = 12 * 3600) -> Snapshot | None:
        """Earliest snapshot in [ts - 5m, ts + within)."""
        cursor = await self.db.execute(
            """
            SELECT player_id, taken_at, elo, level
            FROM snapshots
            WHERE player_id = ? AND taken_at >= ? AND taken_at < ?
            ORDER BY taken_at ASC, id ASC
            LIMIT 1
            """,
            (player_id, ts - 300, ts + within),
        )
        return self._snapshot_from_row(await cursor.fetchone())

    async def first_snapshot_after(self, player_id: str, ts: int) -> Snapshot | None:
        cursor = await self.db.execute(
            """
            SELECT player_id, taken_at, elo, level
            FROM snapshots
            WHERE player_id = ? AND taken_at >= ?
            ORDER BY taken_at ASC, id ASC
            LIMIT 1
            """,
            (player_id, ts),
        )
        return self._snapshot_from_row(await cursor.fetchone())

    async def peak_elo(self, player_id: str) -> int:
        cursor = await self.db.execute(
            "SELECT MAX(elo) FROM snapshots WHERE player_id = ?",
            (player_id,),
        )
        row = await cursor.fetchone()
        snap = 0 if row is None or row[0] is None else int(row[0])
        recorded = await self.get_recorded_peak(player_id)
        stored = recorded.peak_elo if recorded else 0
        return max(snap, stored)

    async def get_recorded_peak(self, player_id: str) -> EloPeak | None:
        cursor = await self.db.execute(
            "SELECT player_id, peak_elo, checked_at FROM elo_peaks WHERE player_id = ?",
            (player_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return EloPeak(row["player_id"], int(row["peak_elo"]), int(row["checked_at"]))

    async def set_recorded_peak(
        self, player_id: str, peak_elo: int, checked_at: int, *, replace: bool = False
    ) -> None:
        peak_sql = "excluded.peak_elo" if replace else "MAX(elo_peaks.peak_elo, excluded.peak_elo)"
        await self.db.execute(
            f"""
            INSERT INTO elo_peaks (player_id, peak_elo, checked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                peak_elo = {peak_sql},
                checked_at = excluded.checked_at
            """,
            (player_id, peak_elo, checked_at),
        )
        await self.db.commit()

    async def peak_level(self, player_id: str) -> int:
        cursor = await self.db.execute(
            "SELECT MAX(level) FROM snapshots WHERE player_id = ?",
            (player_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    def _snapshot_from_row(self, row: aiosqlite.Row | None) -> Snapshot | None:
        if row is None:
            return None
        return Snapshot(row["player_id"], row["taken_at"], row["elo"], row["level"])

    async def get_setting(self, key: str) -> str | None:
        cursor = await self.db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return None if row is None else row["value"]

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def get_channel_id(self, key: str = SETTING_WEEKLY_CHANNEL_ID) -> int | None:
        raw = await self.get_setting(key)
        if raw is None and key == SETTING_WEEKLY_CHANNEL_ID:
            raw = await self.get_setting(SETTING_CHANNEL_ID)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid %s in settings: %s", key, raw)
            return None

    async def set_channel_id(self, channel_id: int, key: str = SETTING_WEEKLY_CHANNEL_ID) -> None:
        await self.set_setting(key, str(channel_id))
