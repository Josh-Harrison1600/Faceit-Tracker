from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

FACEIT_API_BASE = "https://open.faceit.com/data/v4"


class FaceitError(Exception):
    pass


class FaceitNotFound(FaceitError):
    pass


@dataclass(frozen=True)
class PlayerProfile:
    player_id: str
    nickname: str
    elo: int
    level: int


@dataclass(frozen=True)
class MatchResult:
    finished_at: int
    won: bool
    match_id: str | None = None


class FaceitClient:
    def __init__(self, api_key: str, game_id: str = "cs2") -> None:
        self.game_id = game_id
        self._client = httpx.AsyncClient(
            base_url=FACEIT_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        self._public = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; CSProgressTracker/1.0)",
            },
        )

    async def close(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()
        if not self._public.is_closed:
            await self._public.aclose()

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> dict:
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("FACEIT request failed (%s): %s", path, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay
                logger.warning("FACEIT rate limited; retrying in %.1fs", wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 30)
                continue

            if response.status_code == 404:
                raise FaceitNotFound(f"FACEIT resource not found: {path}")

            if response.status_code >= 400:
                raise FaceitError(
                    f"FACEIT {response.status_code} for {path}: {response.text[:200]}"
                )

            return response.json()

        raise FaceitError(f"FACEIT request failed after retries: {path}") from last_error

    async def get_player_by_nickname(self, nickname: str) -> PlayerProfile:
        data = await self._get("/players", params={"nickname": nickname})
        return self._profile_from_payload(data)

    async def get_player(self, player_id: str) -> PlayerProfile:
        data = await self._get(f"/players/{player_id}")
        return self._profile_from_payload(data)

    def _profile_from_payload(self, data: dict) -> PlayerProfile:
        nickname = data.get("nickname") or "unknown"
        player_id = data.get("player_id")
        if not player_id:
            raise FaceitError(f"FACEIT player payload missing player_id for {nickname}")

        games = data.get("games") or {}
        game = games.get(self.game_id)
        if not game:
            raise FaceitError(f"{nickname} has no {self.game_id} FACEIT profile")

        return PlayerProfile(
            player_id=player_id,
            nickname=nickname,
            elo=int(game.get("faceit_elo") or 0),
            level=int(game.get("skill_level") or 0),
        )

    async def matchmaking_matches(
        self, player_id: str, from_ts: int, to_ts: int
    ) -> list[MatchResult]:
        matches: list[MatchResult] = []
        offset = 0
        limit = 100

        while offset <= 1000:
            data = await self._get(
                f"/players/{player_id}/history",
                params={
                    "game": self.game_id,
                    "from": from_ts,
                    "to": to_ts,
                    "offset": offset,
                    "limit": limit,
                },
            )
            items = data.get("items") or []
            if not items:
                break

            for match in items:
                finished_at = int(match.get("finished_at") or 0)
                if finished_at and (finished_at < from_ts or finished_at >= to_ts):
                    continue
                result = _matchmaking_result(player_id, match)
                if result is None:
                    continue
                matches.append(
                    MatchResult(
                        finished_at=finished_at,
                        won=result,
                        match_id=match.get("match_id"),
                    )
                )

            if len(items) < limit:
                break
            offset += limit

        return matches

    async def player_match_stats(
        self, player_id: str, from_ts: int, to_ts: int, *, limit: int = 100, max_offset: int = 200
    ) -> list[dict]:
        """Per-match CS2 stats. from/to are unix seconds; the API wants milliseconds."""
        from_ms = from_ts * 1000
        to_ms = to_ts * 1000
        try:
            return await self._player_match_stats_pages(
                player_id, limit, extra={"from": from_ms, "to": to_ms}, max_offset=max_offset
            )
        except FaceitError:
            logger.info("Retrying player CS2 stats without from/to for %s", player_id)
            return await self._player_match_stats_pages(player_id, limit, max_offset=max_offset)

    async def _player_match_stats_pages(
        self,
        player_id: str,
        limit: int,
        extra: dict[str, str | int] | None = None,
        max_offset: int = 200,
    ) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while offset <= max_offset:
            params: dict[str, str | int] = {"offset": offset, "limit": limit}
            if extra:
                params.update(extra)
            data = await self._get(
                f"/players/{player_id}/games/{self.game_id}/stats",
                params=params,
            )
            page = data.get("items") or []
            if not page:
                break
            items.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return items

    async def match_stats(self, match_id: str) -> dict:
        return await self._get(f"/matches/{match_id}/stats")

    async def get_match(self, match_id: str) -> dict:
        return await self._get(f"/matches/{match_id}")

    async def player_elo_by_match(self, player_id: str) -> dict[str, int]:
        """Per-match Elo from FACEIT's public stats time series (not on the Data API)."""
        by_id: dict[str, int] = {}
        for page in range(0, 20):
            items = await self._elo_history_page(player_id, page)
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                match_id = item.get("matchId") or item.get("match_id") or item.get("Match Id")
                elo = item.get("elo") or item.get("gameElo") or item.get("Elo")
                if not match_id or elo is None or elo == "":
                    continue
                try:
                    by_id[str(match_id)] = int(float(elo))
                except (TypeError, ValueError):
                    continue
            if len(items) < 100:
                break
        return by_id

    async def _elo_history_page(self, player_id: str, page: int) -> list:
        url = f"https://api.faceit.com/stats/v1/stats/time/users/{player_id}/games/{self.game_id}"
        try:
            response = await self._public.get(url, params={"size": 100, "page": page})
        except httpx.HTTPError as exc:
            logger.warning("FACEIT elo history failed for %s: %s", player_id, exc)
            return []
        if response.status_code >= 400:
            if page == 0:
                logger.info("FACEIT elo history %s for %s", response.status_code, player_id)
            return []
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("items") or []
        return items if isinstance(items, list) else []


def player_row_from_match_stats(data: dict, player_id: str) -> dict | None:
    """Pull one player's CS2 row out of GET /matches/{id}/stats."""
    for rnd in data.get("rounds") or []:
        round_stats = rnd.get("round_stats") or {}
        map_name = round_stats.get("Map")
        match_id = rnd.get("match_id") or round_stats.get("Match Id")
        teams = rnd.get("teams") or []
        player_team = None
        player_stats: dict | None = None
        for team in teams:
            for player in team.get("players") or []:
                if player.get("player_id") != player_id:
                    continue
                player_team = team
                player_stats = dict(player.get("player_stats") or {})
                break
            if player_stats is not None:
                break
        if player_stats is None:
            continue
        if map_name and not player_stats.get("Map"):
            player_stats["Map"] = map_name
        if match_id and not player_stats.get("Match Id"):
            player_stats["Match Id"] = match_id
        score = _team_score_for_player(player_team, teams) or round_stats.get("Score")
        if score and not player_stats.get("Score"):
            player_stats["Score"] = score
        return {"stats": player_stats}
    return None


def _team_score_for_player(player_team: dict | None, teams: list) -> str | None:
    if player_team is None:
        return None
    ours = _team_final_score(player_team)
    theirs = None
    for team in teams:
        if team is player_team:
            continue
        theirs = _team_final_score(team)
        if theirs is not None:
            break
    if ours is None or theirs is None:
        return None
    return f"{ours}-{theirs}"


def _team_final_score(team: dict) -> str | None:
    stats = team.get("team_stats") or {}
    for key in ("Final Score", "Score", "Team Score"):
        value = stats.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _matchmaking_result(player_id: str, match: dict) -> bool | None:
    competition = str(match.get("competition_type") or "").lower()
    if competition != "matchmaking":
        return None

    status = str(match.get("status") or "").upper()
    if status and status not in {"FINISHED", "FINISHED_CLOSED"}:
        return None

    winner = (match.get("results") or {}).get("winner")
    if not winner:
        return None

    teams = match.get("teams") or {}
    player_team: str | None = None
    for team_key, team in teams.items():
        players = (team or {}).get("players") or []
        if any(player.get("player_id") == player_id for player in players):
            player_team = team_key
            break

    if player_team is None:
        return None
    if player_team == winner:
        return True
    team = teams.get(player_team) or {}
    return team.get("team_id") == winner


def elo_delta_from_match(data: dict, player_id: str, won: bool | None) -> int | None:
    """Estimate FACEIT ELO change from pre-match win probability. K=50."""
    if won is None or not data.get("calculate_elo"):
        return None
    player_team: dict | None = None
    for team in (data.get("teams") or {}).values():
        roster = (team or {}).get("roster") or []
        if any(player.get("player_id") == player_id for player in roster):
            player_team = team
            break
    if player_team is None:
        return None
    raw = (player_team.get("stats") or {}).get("winProbability")
    if raw is None:
        return None
    try:
        expected = float(raw)
    except (TypeError, ValueError):
        return None
    actual = 1.0 if won else 0.0
    return int(round(50 * (actual - expected)))
