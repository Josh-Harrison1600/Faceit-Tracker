# Linux agent: pull peak-ELO fix and restart the bot

Instructions for a Cursor agent on the Linux Mint machine. Repo is usually `~/csprogresstracker`.

Do **not** commit, push, or edit `.env`. Do **not** copy or overwrite `data/` or secrets. Do **not** re-run `scripts/install-linux.sh` unless the systemd user service is missing.

## Goal

Pull `main` (commit `70fc9bb` or later: *Fetch match ELO with Chrome TLS impersonation so the bot does not depend on system curl.*), install `curl_cffi` into the **same** venv the service uses, restart `csprogresstracker`, and confirm it stays up.

## Steps

1. `cd` into the repo (find `bot/main.py` + `.venv` if it is not `~/csprogresstracker`).
2. `git status` and `git remote -v`. Remote should be `https://github.com/Josh-Harrison1600/Faceit-Tracker.git`.
3. Pull and install:

```bash
cd ~/csprogresstracker
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
```

4. Restart the user systemd service:

```bash
systemctl --user daemon-reload
systemctl --user restart csprogresstracker
systemctl --user status csprogresstracker --no-pager
```

5. If it crash-loops with `ModuleNotFoundError: curl_cffi`, the pip install did not use the service’s Python. Fix with:

```bash
~/csprogresstracker/.venv/bin/pip install -r ~/csprogresstracker/requirements.txt
systemctl --user restart csprogresstracker
```

6. Confirm `active (running)`. If not, capture:

```bash
journalctl --user -u csprogresstracker -n 40 --no-pager
```

## After this succeeds

On Discord, run `/get-peak-elo`. Expected season peaks:

- DXR5 **748**
- Gears-turnt **978**
- NineOwl9 **903**

## Reply with

- git `HEAD`
- that `curl_cffi` installed
- service `active (running)` or the journalctl error
