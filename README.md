# CS Progress Tracker

Discord bot that tracks friends' CS2 FACEIT profiles. It posts a Sunday–Saturday recap (maps, wins/losses, daily ELO) and a per-map daily breakdown.

## What you need first

The bot cannot run until these exist. They are not created by this repo.

### 1. FACEIT API key

1. Sign in at [developers.faceit.com](https://developers.faceit.com/).
2. Create an app in App Studio.
3. Generate a **server-side** Data API key.
4. Put it in `.env` as `FACEIT_API_KEY`.

FACEIT only returns **current** ELO. The bot snapshots ELO at local midnight so it can show daily up/down. Maps and W/L come from matchmaking history. Per-map K/D, ADR, HS%, KPR, and (when FACEIT sends them) rating, utility damage, and flashes come from CS2 match stats.

**Swing is not available** from the FACEIT Data API (that is a Leetify/third-party stat). Daily posts and `/last-map` omit it. Rating is shown only if FACEIT includes a Rating field; otherwise KPR is still shown. Flashes thrown vs enemies blinded appear only when those keys exist on the match.

### 2. Discord bot

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Add a bot and copy the token into `.env` as `DISCORD_TOKEN`.
3. Invite the bot with scopes `bot` and `applications.commands`.
4. Grant **Send Messages**, **Embed Links**, and **View Channel** on `#weekly-report` and `#daily-breakdown`.

Invite URL (replace `YOUR_CLIENT_ID`):

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=18432&scope=bot%20applications.commands
```

`18432` is View Channel + Send Messages + Embed Links.

### 3. Keep the process running

Scheduled posts and midnight ELO snapshots only run if the bot is online. A sleeping PC skips those jobs. Maps/W/L still fill in from FACEIT; a missed midnight snapshot shows `—` for that day's ELO.

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env
copy players.example.yaml players.yaml
```

Fill in `.env`, then edit `players.yaml` with FACEIT nicknames (optional — you can also use `/addplayer` later).

Create two text channels in your server: **`weekly-report`** and **`daily-breakdown`**. Set `DISCORD_GUILD_ID` so the bot can find them by name (and so slash commands appear immediately while testing). You can also set `WEEKLY_CHANNEL_ID` / `DAILY_CHANNEL_ID`, or run `/setweekly` and `/setdaily` in those channels.

```bash
python -m bot.main
```

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/addplayer nickname` | Anyone | Resolve a FACEIT nick, add them, snapshot current ELO/level |
| `/removeplayer nickname` | Anyone | Stop tracking a player |
| `/listplayers` | Anyone | Show the roster |
| `/setweekly` | Manage Server | Save this channel for Sunday weekly recaps |
| `/setdaily` | Manage Server | Save this channel for daily map breakdowns |
| `/report` | Anyone | Post **this** Sunday–Saturday week so far. Future days are `N/A` |
| `/player-report nickname` | Anyone | Same week recap as `/report`, for one player (roster nick, or any FACEIT nick) |
| `/last-map nickname` | Anyone | Per-map stats for that player's most recent matchmaking map (roster nick, or any FACEIT nick) |

`/report`, `/player-report`, and `/last-map` post in whatever channel you run them. Scheduled recaps go to the saved channels.

## Channels and schedule

Times are `TIMEZONE` (default **America/Halifax**).

| When | Channel | What |
| --- | --- | --- |
| Sunday 11:59 PM | `#weekly-report` | Completed last Sun–Sat week (same embed as `/report`, finished week) |
| Mon–Fri 10:45 PM | `#daily-breakdown` | Today's matchmaking maps per roster player |
| Sat–Sun 11:59 PM | `#daily-breakdown` | Today's matchmaking maps per roster player |
| Every day 00:00 | (no post) | Midnight ELO snapshot |

On Sunday at 11:59 PM both the weekly recap and the daily breakdown post, to their own channels.

## How the week works

- Days are **Sun–Sat** in `TIMEZONE`.
- **`/report`:** current week. Today can be partial. Days after today are `N/A`.
- **Sunday 11:59 PM:** posts the week that just **finished** (last Sunday through Saturday). This Sunday’s games go into next week’s recap.
- Each player is one section in a **single** Discord message: Peak Elo, Peak Level, Current Elo, Current Level, then daily maps / W-L / ELO, then a weekly total.
- W/L is CS2 **matchmaking** only. One matchmaking match counts as one map.
- Daily ELO needs a midnight snapshot. If the bot was asleep, maps/W/L still show and ELO is `—`.
- Players still calibrating have no ELO yet.

## Daily map breakdown

One Discord message, one field per player. Example:

```
NineOwl9
Ancient - Loss
Score: 7-13
ELO -12
K/D 18/12 (1.50)
ADR 92
HS 41%
KPR 0.75
Rating 1.12
Util dmg 180
Flashes 6 thrown / 4 blinded
```

Lines are omitted when FACEIT does not send that stat. **Swing is not available** from FACEIT (it is a Leetify stat). Rating is shown only if FACEIT includes a Rating field. ELO change is this match’s Elo minus the previous matchmaking match.

`/last-map` uses the same stats for a single recent map. It is not scheduled.

## Linux Mint (leave it running, screen off)

Scheduled posts and **midnight ELO snapshots** only fire if this process is up. The laptop must stay **powered on and not suspended**. The screen can be dark.

### 1. Copy the project

Copy the whole project folder to the laptop (USB, `scp`, etc.). Do **not** skip `.env` if you already filled it in on Windows — that file has your tokens. Also copy `data/tracker.db` if you want to keep the current roster.

On the Mint laptop:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
cd ~/csprogresstracker
chmod +x scripts/install-linux.sh
./scripts/install-linux.sh
```

If `.env` was empty, edit it (`nano .env`), then:

```bash
systemctl --user restart csprogresstracker
```

Check it:

```bash
systemctl --user status csprogresstracker
journalctl --user -u csprogresstracker -f
```

That systemd user service starts on login and restarts if the bot crashes. `loginctl enable-linger` (the install script tries this) keeps it running after you log out.

### 2. Screen off, machine still awake

Plug the laptop in. Then:

**Cinnamon power settings** (Menu → Power Management, plugged-in column):

- Turn off the screen: 5–15 minutes is fine
- Suspend when inactive: **Never**
- When the lid is closed: **Do nothing** or **Blank screen** (not Suspend)

**Lid close (more reliable)** — this stops Mint from sleeping when you shut the lid:

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
echo '[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore' | sudo tee /etc/systemd/logind.conf.d/lid.conf
sudo systemctl restart systemd-logind
```

You can close the lid; the Discord bot stays online. Leave it plugged in so it does not die on battery.

### 3. Scheduled posts

No extra cron job. The bot posts the weekly recap and daily breakdowns on the schedule above. `/report` and `/last-map` still work anytime.

If the laptop was off or suspended at that moment, that scheduled post is skipped. The next `/report` or `/last-map` still works.

## Docker

```bash
docker build -t csprogresstracker .
docker run --env-file .env -v "%cd%/data:/app/data" -v "%cd%/players.yaml:/app/players.yaml" csprogresstracker
```

On Linux/macOS use `$(pwd)` instead of `%cd%`. Persist `/app/data` so SQLite snapshots survive restarts.
