# Deploying on Ubuntu 24.04 (2 vCPU, 2 GB RAM)

Every command below is meant to be copied as-is. Replace `YOUR_VPS_IP` with your server's
address. Commands marked **Windows** run in PowerShell on your PC; all others run on the VPS.

> These steps were checked for syntax and against the installer's logic. They have **not**
> been run on a real Ubuntu 24.04 machine yet (see `FINAL_AUDIT_REPORT.md`). Do the
> dry-run stage (step 5) first.

## 1. Copy the project to the VPS

**Windows** (PowerShell; `scp` is built into Windows 10/11):

```powershell
scp -r "C:\Users\user\Downloads\bot files\deriv-market-analyzer" root@YOUR_VPS_IP:/root/
```

Then log in:

```bash
ssh root@YOUR_VPS_IP
```

## 2. Install

```bash
cd /root/deriv-market-analyzer
```

```bash
sed -i 's/\r$//' *.sh systemd/*.service scripts/*.py
```

(The `sed` line only removes Windows line endings in case an editor added them. It is harmless
otherwise.)

```bash
chmod +x *.sh && sudo ./install.sh
```

The installer:

* installs `python3` (3.12 on 24.04), `python3-venv`, `sqlite3`, `rsync`, `tzdata`;
* creates the system user `deriv-analyzer` (no login shell);
* copies the app to `/opt/deriv-market-analyzer` (code owned by root and read-only to the
  service; `data/` and `logs/` owned by `deriv-analyzer`);
* builds a virtualenv and installs `requirements.txt`;
* creates `/opt/deriv-market-analyzer/.env` from `.env.example` (mode 640, root:deriv-analyzer);
* initialises the SQLite database in WAL mode and validates the 10 strategies;
* installs, enables (start at boot) and starts the `deriv-analyzer` systemd service.

Without Telegram credentials the service runs normally and writes every notification to the log
only.

## 3. Set the environment variables (secrets)

Only `.env` holds secrets. Edit it on the VPS; never put the token in `config.yaml`, a script,
or a chat.

```bash
sudo nano /opt/deriv-market-analyzer/.env
```

```
TELEGRAM_BOT_TOKEN=123456789:your-token-from-BotFather
TELEGRAM_CHAT_ID=
ANALYZER_DRY_RUN=true
```

`.env` is created from `.env.example`, which starts in dry run (`ANALYZER_DRY_RUN=true`):
notifications are logged, not sent, until you set it to `false` (step 6).

Format rules: one `KEY=value` per line, no spaces around `=`, no quotes.

Find your chat id without typing the token anywhere: send your bot any message in Telegram,
then run

```bash
sudo -u deriv-analyzer /opt/deriv-market-analyzer/.venv/bin/python /opt/deriv-market-analyzer/scripts/telegram_chat_id.py
```

Put the printed id into `TELEGRAM_CHAT_ID` (a channel id starts with `-100`).

| Variable | Required | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | for Telegram | bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | for Telegram | your chat / group / channel id |
| `ANALYZER_DRY_RUN` | no | `true` = log notifications, do not send them |
| `ANALYZER_CONFIG` | no | alternative path to `config.yaml` |

No Deriv token or password is used anywhere; market data comes from Deriv's public API.

## 4. Telegram test (safe)

```bash
sudo -u deriv-analyzer /opt/deriv-market-analyzer/.venv/bin/python /opt/deriv-market-analyzer/scripts/test_telegram.py
```

You should receive two messages that start with **🧪 TEST MESSAGE — NOT A REAL SIGNAL**: a
TEST START and a TEST STOP. The test never opens the database, so real signal states and the
notification queue are untouched; it can run while the service is running. To see the text
without sending, add `--print`. To use another bot/market in the text:
`--bot delta --symbol R_10`.

## 5. Stage 1: dry run on the VPS (recommended for the first day)

`ANALYZER_DRY_RUN=true` is already set in a fresh `.env`. Then:

```bash
sudo /opt/deriv-market-analyzer/restart.sh
```

```bash
sudo /opt/deriv-market-analyzer/status.sh
```

```bash
/opt/deriv-market-analyzer/logs.sh
```

What to expect:

* `event=discovery_done ... monitored=20 compatible_pairs=200` within a minute (the numbers follow
  whatever Deriv lists that day);
* history download for about an hour (`bootstrap_done` once per market);
* payout quotes (`payout_refresh_done requested=300 ok=300`, ~10 minutes at 30 quotes/min);
* after that, `status.sh` shows counts of WAIT / AVOID / INSUFFICIENT_DATA. On fair digits
  that is the normal steady state; a START is not expected.

Check memory once bootstrap is done:

```bash
systemctl status deriv-analyzer --no-pager | grep -i memory
```

## 6. Stage 2: live Telegram

Set `ANALYZER_DRY_RUN=false` in `.env`, then:

```bash
sudo /opt/deriv-market-analyzer/restart.sh
```

## Everyday commands

| Command | What it does |
|---|---|
| `sudo /opt/deriv-market-analyzer/status.sh` | service state, heartbeat, status counts, active signals |
| `/opt/deriv-market-analyzer/logs.sh` | follow the application log (`logs.sh journal` for systemd's journal) |
| `sudo /opt/deriv-market-analyzer/restart.sh` | restart (re-reads `.env` and `config.yaml`) |
| `sudo /opt/deriv-market-analyzer/stop.sh` / `start.sh` | stop / start |
| `sudo -u deriv-analyzer /opt/deriv-market-analyzer/.venv/bin/python /opt/deriv-market-analyzer/scripts/report.py --matrix` | compatibility matrix |
| `sudo -u deriv-analyzer /opt/deriv-market-analyzer/.venv/bin/python /opt/deriv-market-analyzer/scripts/report.py --bot delta` | detail for one bot |
| `sudo -u deriv-analyzer /opt/deriv-market-analyzer/.venv/bin/python /opt/deriv-market-analyzer/scripts/live_api_check.py` | read-only Deriv API check |

## Upgrading

Copy the new project folder to `/root/deriv-market-analyzer` again and re-run
`sudo ./install.sh`. Data, logs and `.env` are kept. If you edited
`/opt/deriv-market-analyzer/config.yaml`, it is kept too and the shipped version is saved next to
it as `config.yaml.new`.

## Service behaviour (systemd)

* `Restart=always`, `RestartSec=5`, no start-rate limit: a crash restarts the service after 5 s.
* `SIGTERM` (stop/restart) shuts down cleanly within `TimeoutStopSec=30`: ticks are flushed,
  pending Telegram messages are attempted once more, the "last alive" time is stored.
* After any restart the confirmation counters start from zero. If the analyzer was down for
  120 s or more (`signals.offline_stop_s`), every active signal gets a critical STOP, because
  it was not monitored during the gap.
* `MemoryMax=1500M`, `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`;
  the service can only write to `data/` and `logs/`.
* Logs: `logs/analyzer.log`, rotated at 10 MB × 10 files (≤ 110 MB). The journal only gets
  stderr. Ticks older than 30 days are pruned hourly.
