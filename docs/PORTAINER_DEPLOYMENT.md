# Deploying with Docker and Portainer CE

This guide runs the Deriv Market Condition Analyzer as a one-service Portainer **stack** on an
Ubuntu VPS (22.04 / 24.04, 2 vCPU, 2 GB RAM, 40 GB disk).

> **What has and has not been tested.** The Docker files were written for this deployment.
> Unit tests and a Windows simulation of the container lifecycle passed. That simulation
> covered dry-run startup, data persisting when the container is recreated, the health check,
> backup and restore, and no writes outside the volumes.
>
> **No Docker engine was available when these files were prepared.** The image has **not**
> been built, `docker compose config` has **not** been run, and the stack has **not** been
> deployed in Portainer. Do the checks in [§5](#5-deploy-the-stack) on your VPS before you
> rely on it.

The analyzer only **reads public market data** and sends START / STOP / CRITICAL messages
to Telegram. It never trades, never logs in to Deriv, and needs no Deriv token.

**Contents:**
1. [Required VPS software](#1-required-vps-software)
2. [Upload the project](#2-upload-the-project-and-build-the-image)
3. [Create the stack](#3-create-the-stack)
4. [Environment variables](#4-environment-variables)
5. [Deploy](#5-deploy-the-stack)
6. [View logs](#6-view-logs)
7. [Restart](#7-restart)
8. [Update safely](#8-update-safely)
9. [Back up the database](#9-back-up-the-database)
10. [Restore the database](#10-restore-the-database)
11. [Stop or remove the stack without losing data](#11-stop-or-remove-the-stack-without-losing-data)
12. [Troubleshooting](#12-troubleshooting)

## What gets deployed

| Item | Value |
|---|---|
| Service / container | `analyzer` / `deriv-analyzer` (one container, no other services) |
| Image | `deriv-market-analyzer:1.0.0` (built on the VPS from this folder) |
| Entry point | `tini` → `python -m app.main` (tini forwards `docker stop`'s SIGTERM) |
| User in the container | `analyzer`, uid/gid **10001** (not root) |
| Ports | **none** (outbound HTTPS/WSS only: `api.derivws.com`, `api.telegram.org`) |
| Volumes | `deriv-analyzer-data` → `/app/data`: SQLite database, heartbeat, backups. `deriv-analyzer-logs` → `/app/logs`: rotating log file |
| Network | `deriv-analyzer-net` (its own bridge network) |
| Hardening | read-only root filesystem, `/tmp` in RAM (64 MB), all Linux capabilities dropped, `no-new-privileges`, no Docker socket, not privileged |
| Limits | 1500 MB RAM (measured peak ~325 MB); Docker logs 5 × 10 MB |
| Restart | `unless-stopped`; health check every 60 s after a 180 s start period |
| Default mode | **dry run**: notifications are only written to the log |

Files in this folder:

| File | Use |
|---|---|
| `Dockerfile` | builds the image |
| `docker-compose.yml` | stack for Portainer's **Web editor** (uses the image you built) |
| `docker-compose.repository.yml` | the same stack plus a `build:` section, for Portainer's **Repository** mode |
| `.dockerignore` | keeps `.env`, `data/`, `logs/`, tests and caches out of the image |
| `.env.example` | the variables you can set (no values) |
| `scripts/healthcheck.py`, `scripts/backup_db.py`, `scripts/restore_db.py` | health check, backup, restore |

---

## 1. Required VPS software

Log in over SSH as a sudo user. All commands below are for Ubuntu.

**Docker Engine and the Compose plugin**, from Docker's official apt repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl unzip
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker run --rm hello-world
```

**Portainer CE**, if it is not installed yet:

```bash
sudo docker volume create portainer_data
sudo docker run -d --name portainer --restart=always -p 9443:9443 \
  -v /var/run/docker.sock:/var/run/docker.sock -v portainer_data:/data \
  portainer/portainer-ce:lts
```

Open `https://<VPS-IP>:9443` within a few minutes and create the admin user. Choose the local
Docker environment.

Portainer itself needs the Docker socket. The analyzer container does **not** get it.
Allow port 9443 only from your own IP if you can, for example with
`sudo ufw allow from <your-ip> to any port 9443`. The analyzer needs no inbound port.

## 2. Upload the project and build the image

From your own computer:

```bash
scp deriv-market-analyzer-portainer.zip <user>@<VPS-IP>:/tmp/
```

Then on the VPS:

```bash
sudo mkdir -p /opt
sudo unzip -q /tmp/deriv-market-analyzer-portainer.zip -d /opt
cd /opt/deriv-market-analyzer
sudo docker build -t deriv-market-analyzer:1.0.0 .
```

The build installs the Python packages. It also checks the config and the 10 strategy files
with `python -m app.main --validate`, so a broken config fails the build rather than the
deployment.

Check the image:

```bash
sudo docker run --rm --network none deriv-market-analyzer:1.0.0 python -m app.main --validate
sudo docker run --rm --network none deriv-market-analyzer:1.0.0 id
```

The last lines of the first command should read `10 bots, 18 legs loaded`. The second
should print `uid=10001(analyzer) gid=10001(analyzer)`.

Optional: check that Docker Compose itself accepts the stack file.

```bash
sudo docker compose -f docker-compose.yml config
```

The ZIP contains no secrets. Do **not** create a `.env` file in this folder for Docker.
`.dockerignore` would keep it out of the image anyway; enter the values in Portainer instead
(§4).

## 3. Create the stack

**Option A: Web editor (recommended with the ZIP)**

1. In Portainer, go to **Stacks → + Add stack**.
2. **Name:** `deriv-analyzer`, in lower case.
3. **Build method:** *Web editor*. Paste the whole content of `docker-compose.yml`:
   ```bash
   cat /opt/deriv-market-analyzer/docker-compose.yml
   ```
4. Add the environment variables (§4) under **Environment variables**.
5. Click **Deploy the stack** (§5).

The Web editor cannot build images. It uses the image you built in §2. If you rebuild under
a different tag, set `ANALYZER_IMAGE_TAG` to match.

**Option B: Repository** (if you push this folder to a private Git repository)

1. **Stacks → + Add stack**. Name it `deriv-analyzer`. Choose *Repository*.
2. Enter the repository URL and reference (for example `refs/heads/main`), and credentials if
   the repository is private.
3. **Compose path:** `docker-compose.repository.yml`. This file has a `build:` section, so
   Portainer builds the image from the cloned repository.
4. Add the environment variables, then **Deploy the stack**.

**Never commit a filled-in `.env` file to the repository.**

## 4. Environment variables

Set these in the stack's **Environment variables** section: **+ Add an environment variable**,
or **Load variables from .env file** using a copy of `.env.example` that you filled in on your
own computer. Never put the values into `docker-compose.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `ANALYZER_DRY_RUN` | `true` | `true`: write notifications to the log, send nothing. `false`: send START / STOP / CRITICAL messages to Telegram. |
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Bot token from @BotFather. Needed only when dry run is `false`. |
| `TELEGRAM_CHAT_ID` | *(empty)* | Chat, group or channel id, for example `-1001234567890`. |
| `ANALYZER_IMAGE_TAG` | `1.0.0` | Which `deriv-market-analyzer:<tag>` image the stack runs. |
| `ANALYZER_CONFIG` | `/app/config.yaml` | Use a different config file, for example one copied into the data volume as `/app/data/config.yaml`. |

**Notes:**
- **Start with dry run.** Leave `ANALYZER_DRY_RUN=true` for the first deployment and watch
  the logs (§6). Switch it to `false` only after the Telegram test below has worked.
- **Secrets are not written to the logs.** The bot token is never logged; the logger also
  masks it if it appears anywhere.
- **Anyone with access to the stack can see these values.** Portainer stores them in its
  database and shows them on the stack page. Limit who can log in to Portainer.
- **No Deriv token is needed or used.**

**Telegram test.** This sends two messages clearly marked **TEST** and never touches the
database. It works while dry run is still on. Run it after the stack has started, in
Portainer under **Containers → deriv-analyzer → Console** (connect as the default user):

```bash
python scripts/test_telegram.py            # sends TEST START + TEST STOP
python scripts/test_telegram.py --print    # only shows the texts
```

Or run it from SSH:

```bash
sudo docker exec deriv-analyzer python scripts/test_telegram.py
```

**Switching dry run off** while a combination is already active: its START was only logged,
never sent. You will receive only its later STOP. That is safe but can look odd, so switch
when `python scripts/report.py --brief` shows `active signals: none`.

## 5. Deploy the stack

Click **Deploy the stack**. Then check:

1. **Containers:** `deriv-analyzer` is *running*. The status shows *starting* for up to
   3 minutes, then *healthy*.
2. **Logs (§6):** the first lines include
   ```
   event=analyzer_starting version=1.0.0 bots=10 telegram=dry_run
   event=api_connected url=wss://api.derivws.com/...
   event=market_filter allowed=1HZ10V,1HZ50V,R_10,R_100,R_25,R_50,R_75 excluded=88 ...
   event=discovery_done listed=95 candidates=7 monitored=7 compatible_pairs=70
   ```
   The counts follow Deriv's listing (these are from 2026-11-17). Only Continuous Indices plus
   1HZ10V / 1HZ50V are monitored; `python scripts/list_markets.py` shows what is allowed and
   why everything else is excluded.
3. **First start:** for about an hour the analyzer downloads tick history (`bootstrap_done`
   lines). The evaluations show `INSUFFICIENT_DATA` until enough ticks are stored. That is
   expected; no START is sent without enough data.
4. **From SSH:**
   ```bash
   sudo docker ps --filter name=deriv-analyzer
   sudo docker inspect --format '{{.State.Health.Status}}' deriv-analyzer
   sudo docker exec deriv-analyzer python scripts/healthcheck.py
   sudo docker exec deriv-analyzer python scripts/report.py --brief
   ```

## 6. View logs

**In Portainer:** **Containers → deriv-analyzer → Logs**. Turn on *Auto-refresh*; use *Search*
to filter, for example by `event=signal`, `level=ERROR` or `level=WARNING`.

**From SSH:**

```bash
sudo docker logs -f --tail 200 deriv-analyzer
sudo docker logs --since 1h deriv-analyzer | grep -E "level=(WARNING|ERROR|CRITICAL)"
```

**Line format.** Each line looks like this, with UTC timestamps:

```
2026-11-18T13:03:49.426Z level=INFO logger=app event=evaluation_cycle combos=70 checks=70 contract_checks=126 seconds=0.05
2026-11-18T13:03:49.430Z level=INFO logger=app event=rule_status bot=aria_v2 symbol=1HZ10V status=WAIT previous=RUN_CANDIDATE contracts_passing=1/2 contracts="base:DIGITUNDER9=PASS recovery:DIGITUNDER5=FAIL[3]" reason="contract DIGITUNDER 5 (recovery) FAIL - digit at or below 10%: 3 = 9.80%"
```

| What | Look for |
|---|---|
| Start / stop | `analyzer_starting`, `analyzer_restart`, `analyzer_stopping`, `analyzer_stopped crashed=False` |
| WebSocket | `api_connected`, `api_disconnected`, `api_reconnect_wait`, `api_connection_error` |
| Market discovery | `market_filter` (allowed / excluded markets), `discovery_done`, `market_added_to_monitoring`, `market_removed_from_monitoring`, `discovery_failed` |
| History download | `bootstrap_done`, `gap_backfill_done`, `missing_ticks_detected`, `backfill_job_failed` |
| Evaluation | `evaluation_cycle` (every minute), `rule_passed` / `rule_status` (a combination's status or one of its contracts' PASS / FAIL changed; `contracts=` lists every contract's result and failing digits), `evaluation_slow` (WARNING) |
| Signals | `notification_not_sent reason=dry_run` (dry run), `telegram_sent`, `signal_stop` |
| Telegram problems | `telegram_send_failed`, `notification_not_sent reason=telegram_not_configured` |
| Critical | `level=ERROR`, `level=CRITICAL`, `task_crashed`, `incident_opened` |

**Where logs are kept, and how much:**
- **Docker** keeps 5 × 10 MB per container; this is what Portainer shows.
- **The app** also writes `logs/analyzer.log` in the `deriv-analyzer-logs` volume, rotated at
  10 MB × 10 files, so it survives container recreation. To read it:
  ```bash
  sudo docker exec deriv-analyzer tail -n 100 /app/logs/analyzer.log
  ```

## 7. Restart

**In Portainer:** **Containers → deriv-analyzer → Restart**, or **Stacks → deriv-analyzer →
Stop**, then **Start**.

**From SSH:**

```bash
sudo docker restart -t 45 deriv-analyzer
```

`docker stop` / `restart` send SIGTERM. The app then:
1. closes the WebSocket;
2. writes buffered ticks;
3. delivers pending notifications;
4. logs `analyzer_stopped`.

It has 45 s before Docker kills it.

What happens to signals over a restart:
- **Short restart** (under 2 minutes): active signals are kept.
- **Longer outage** (2 minutes or more): every signal that was active gets a **critical STOP**,
  because nothing was monitored in between.

The container also restarts by itself after a crash or a reboot of the VPS. It does not
restart if you stopped it on purpose (`unless-stopped`).

## 8. Update safely

1. **Back up first** (§9).
2. **Build the new version under a new tag**, so the old image stays available for a rollback:
   ```bash
   sudo unzip -q /tmp/deriv-market-analyzer-portainer-NEW.zip -d /opt/new
   cd /opt/new/deriv-market-analyzer
   sudo docker build -t deriv-market-analyzer:1.0.1 .
   sudo docker run --rm --network none deriv-market-analyzer:1.0.1 python -m app.main --validate
   ```
3. **Point the stack at it.** In Portainer, open **Stacks → deriv-analyzer**:
   - In **Environment variables**, set `ANALYZER_IMAGE_TAG=1.0.1`.
   - If the compose file changed, open **Editor** and paste the new `docker-compose.yml`.
   - Click **Update the stack**. Leave **Re-pull image** *off*: the image exists only on this
     VPS, so a pull from Docker Hub would fail.
4. **Check** the logs and the health status, as in §5. The data volumes are reused; nothing
   is re-downloaded.
5. **Rollback:** set `ANALYZER_IMAGE_TAG` back to the old tag and update the stack again.

Remove old images only when you no longer need them:

```bash
sudo docker image rm deriv-market-analyzer:1.0.0
```

**Changing `config.yaml`.** It is part of the image, so rebuild with a new tag as above.
Alternatively:
1. copy it into the data volume (`/app/data/config.yaml`);
2. set `ANALYZER_CONFIG=/app/data/config.yaml`;
3. restart.

**In Repository mode**, push the change to the repository. Then in Portainer click **Pull and
redeploy**, or **Update the stack** with re-build enabled.

## 9. Back up the database

The backup script takes a consistent copy while the analyzer keeps running, using SQLite's
online backup API. It checks the copy's integrity and keeps the newest 3 backups in
`/app/data/backups`, inside the data volume.

```bash
sudo docker exec deriv-analyzer python scripts/backup_db.py
# -> backup written: /app/data/backups/analyzer-20261116-130542.db (… MB, integrity ok)
```

Copy it off the container and off the VPS:

```bash
sudo mkdir -p /opt/deriv-backups
sudo docker cp deriv-analyzer:/app/data/backups/analyzer-20261116-130542.db /opt/deriv-backups/
scp <user>@<VPS-IP>:/opt/deriv-backups/analyzer-20261116-130542.db .   # from your computer
```

**Daily backup at 03:00 UTC** with the host's cron (`sudo crontab -e`):

```
0 3 * * * docker exec deriv-analyzer python scripts/backup_db.py --keep 3 >> /var/log/deriv-analyzer-backup.log 2>&1
```

**Size.** The database keeps 30 days of ticks. With the 7 monitored markets it grows to roughly
**0.4–0.5 GB** after a month; it measured 16 MB for 460,000 ticks. Each backup is about the
same size, so `--keep 3` uses up to about 1.5 GB. Ticks stored earlier for markets that are now
excluded (for example Jump Indices) are no longer read and are removed by the normal 30-day
retention; nothing has to be migrated. Check usage with:

```bash
sudo du -sh /var/lib/docker/volumes/deriv-analyzer-data/_data
df -h /
```

You can also back up the whole volume while the stack is **stopped**:

```bash
sudo docker run --rm -v deriv-analyzer-data:/data:ro -v /opt/deriv-backups:/out \
  debian:bookworm-slim tar czf /out/deriv-analyzer-data-$(date +%F).tar.gz -C /data .
```

## 10. Restore the database

The restore script:
- refuses to run while the analyzer is running (heartbeat less than 2 minutes old);
- checks that the backup is a valid analyzer database;
- keeps the current database as `analyzer.db.before-restore-<time>`.

1. **Stop the stack:** **Stacks → deriv-analyzer → Stop this stack**, or
   `sudo docker stop -t 45 deriv-analyzer`.
2. **Make the backup available inside the data volume** (`docker cp` works on a stopped
   container). Skip this if the backup is still in `/app/data/backups`; then use that path in
   step 3.
   ```bash
   sudo docker cp /opt/deriv-backups/analyzer-20261116-130542.db deriv-analyzer:/app/data/
   ```
3. **Wait 2 minutes** after the stop, then restore with a one-off container that uses the same
   image and volume:
   ```bash
   sudo docker run --rm --network none -v deriv-analyzer-data:/app/data \
     deriv-market-analyzer:1.0.0 \
     python scripts/restore_db.py /app/data/analyzer-20261116-130542.db
   ```
   If you did not wait, it refuses. Add `--force` only if you are sure the stack is stopped.
4. **Start the stack** again and check the logs. If the backup's "last alive" time is 2 minutes
   or more in the past, every signal that was active in the backup gets a **critical STOP**.
   That is the safe outcome, because those signals were not monitored in between. Missing
   ticks are downloaded again automatically.

When the restored database is fine, delete the old copy:

```bash
sudo docker exec deriv-analyzer sh -c 'rm /app/data/analyzer.db.before-restore-*'
```

## 11. Stop or remove the stack without losing data

| Goal | How | Data |
|---|---|---|
| Pause | **Stacks → deriv-analyzer → Stop this stack**, or `docker stop deriv-analyzer` | kept |
| Resume | **Start this stack** | kept |
| Remove the stack | **Stacks → deriv-analyzer → Delete this stack** | volumes kept, see below |

The database and logs live in the named volumes `deriv-analyzer-data` and
`deriv-analyzer-logs`. They are not part of the container: removing or recreating the
container, or deleting the stack, does not remove named volumes unless you explicitly ask for
it.

To be safe:
- **back up first** (§9);
- if your Portainer version offers an option to remove volumes, leave it **unticked**;
- never remove these two volumes under **Volumes**;
- never run `docker volume rm`, `docker volume prune`, `docker system prune --volumes` or
  `docker compose down -v`.

Check afterwards:

```bash
sudo docker volume ls | grep deriv-analyzer
```

To deploy again later, create the stack again with the same compose file. It reattaches to
the existing volumes by name. If the new stack has a different name, Compose may warn that
the volume "already exists but was created for project …"; that warning is harmless. The data
is still used.

**Deleting everything for good** is irreversible, so take a backup first:

```bash
sudo docker volume rm deriv-analyzer-data deriv-analyzer-logs
```

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| *pull access denied for deriv-market-analyzer* / *image not found* | The image was not built on this VPS, or the tag differs from `ANALYZER_IMAGE_TAG`. Run `sudo docker images deriv-market-analyzer`, rebuild (§2), and keep **Re-pull image** off. |
| Status *unhealthy* | Run `sudo docker inspect --format '{{json .State.Health}}' deriv-analyzer` and `sudo docker exec deriv-analyzer python scripts/healthcheck.py`. *no heartbeat file yet* or *heartbeat … old*: the service loop is not running; check the logs for `ERROR`. *disconnected and no tick*: there has been no connection to Deriv for more than 5 minutes (next row). *no evaluation cycle completed and saved for …*: evaluations have been failing for over 10 minutes; the message shows the last error (e.g. a database write error, also logged as `event=db_write_failed` / `event=evaluation_save_failed`). Check free disk space on the data volume, then restart the container. Docker does **not** restart an *unhealthy* container by itself (`restart: unless-stopped` only covers exits). |
| `api_connection_error` / reconnect loop | Outbound network or DNS. Check with `sudo docker exec deriv-analyzer python -c "import socket; print(socket.gethostbyname('api.derivws.com'))"`. A firewall must allow outbound 443. |
| Restart loop, `task_crashed` | A loop failed. The log line before `analyzer_stopped crashed=True` names it. Send the log lines; never the token. |
| *Permission denied* on `/app/data` or `/app/logs` | Only happens if you replaced the named volumes with host folders (bind mounts). Fix: `sudo chown -R 10001:10001 /your/folder`. |
| *Read-only file system* errors | The root filesystem is read-only on purpose. Everything the app writes goes to `/app/data`, `/app/logs` or `/tmp`; this was checked in the simulation, not yet in Docker. If a library still needs to write elsewhere, delete the `read_only: true` line in the stack and update it. Please report it. |
| Container killed, `OOMKilled=true` | Check with `sudo docker inspect -f '{{.State.OOMKilled}}' deriv-analyzer`. The limit is 1500 MB against a measured peak of about 325 MB, so look for `evaluation_slow` and memory use (`sudo docker stats deriv-analyzer`). On a 2 GB VPS, leave at least about 400 MB for the OS and Portainer. |
| No Telegram messages | Is `ANALYZER_DRY_RUN` still `true`? Run `python scripts/test_telegram.py` (§4) and look for `telegram_send_failed` in the logs. Also check you started a chat with the bot, or added it to the group or channel. |
| `evaluation_slow` warnings | An evaluation took longer than expected. That is fine now and then (for example the first cycle with simulations). If it happens constantly, check `sudo docker stats` and CPU steal on the VPS. |
| Disk filling up | Check `sudo docker system df -v` and `sudo du -sh /var/lib/docker/volumes/deriv-analyzer-*/_data`. Old backups go in `/app/data/backups` (`--keep`); old images can be removed with `sudo docker image prune` (images only, not volumes). |
| Times look wrong | Logs are in UTC (`…Z`). Telegram messages use `telegram.timezone` from `config.yaml`. |
| Stack deploy says the volume *already exists* | Harmless (§11). The existing data is reused. |
