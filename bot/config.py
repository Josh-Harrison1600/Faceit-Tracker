from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a numeric Discord ID (right-click the server or channel "
            f"→ Copy ID). Invite URLs do not belong here."
        ) from exc


@dataclass(frozen=True)
class Settings:
    discord_token: str
    faceit_api_key: str
    weekly_channel_id: int | None
    daily_channel_id: int | None
    discord_guild_id: int | None
    timezone: str
    game_id: str
    db_path: Path
    players_file: Path


def load_settings() -> Settings:
    weekly = _optional_int("WEEKLY_CHANNEL_ID") or _optional_int("DISCORD_CHANNEL_ID")
    return Settings(
        discord_token=_require("DISCORD_TOKEN"),
        faceit_api_key=_require("FACEIT_API_KEY"),
        weekly_channel_id=weekly,
        daily_channel_id=_optional_int("DAILY_CHANNEL_ID"),
        discord_guild_id=_optional_int("DISCORD_GUILD_ID"),
        timezone=os.getenv("TIMEZONE", "America/Halifax").strip() or "America/Halifax",
        game_id=os.getenv("GAME_ID", "cs2").strip() or "cs2",
        db_path=Path(os.getenv("DB_PATH", "data/tracker.db")),
        players_file=Path(os.getenv("PLAYERS_FILE", "players.yaml")),
    )
