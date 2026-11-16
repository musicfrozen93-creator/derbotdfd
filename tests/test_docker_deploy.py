"""Docker / Portainer deployment files, health check, backup / restore, env loading and
graceful shutdown. Static checks only for Dockerfile / compose (no Docker daemon needed)."""
import asyncio
import fnmatch
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import backup_db  # noqa: E402
import healthcheck  # noqa: E402
import restore_db  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.monitoring.heartbeat import write_heartbeat  # noqa: E402
from app.storage import repositories as repo  # noqa: E402
from app.storage.database import init_db  # noqa: E402

SECRET_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


def interpolate(text: str, env: dict) -> str:
    """Minimal Compose interpolation: ${VAR}, ${VAR:-default}."""
    def rep(m):
        name, default = m.group(1), m.group(3)
        val = env.get(name)
        return val if val not in (None, "") else (default or "")
    return re.sub(r"\$\{([A-Z0-9_]+)(:-([^}]*))?\}", rep, text)


def _interp_tree(node, env):
    if isinstance(node, dict):
        return {k: _interp_tree(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_interp_tree(v, env) for v in node]
    return interpolate(node, env) if isinstance(node, str) else node


def compose(name="docker-compose.yml", env=None):
    """Like Compose: parse the YAML first, then interpolate inside string values."""
    return _interp_tree(yaml.safe_load((ROOT / name).read_text(encoding="utf-8")), env or {})


# ----------------------------------------------------------------- Dockerfile
def test_dockerfile_uses_real_entry_point_non_root_and_no_secrets():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "app.main"]' in df and (ROOT / "app" / "main.py").exists()
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in df
    users = re.findall(r"^USER\s+(\S+)", df, re.M)
    assert users == ["analyzer"]
    assert "ANALYZER_DRY_RUN=true" in df
    assert "TELEGRAM" not in df and not SECRET_RE.search(df)
    assert "HEALTHCHECK" in df and (ROOT / "scripts" / "healthcheck.py").exists()
    base = re.search(r"^FROM\s+(\S+)", df, re.M).group(1)
    assert base.startswith("python:3.12")
    # requirements installed from the project's own file
    assert "pip install --no-cache-dir -r requirements.txt" in df


def _context_files():
    """Files Docker would send as build context, applying .dockerignore (simplified rules)."""
    rules = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append((line.startswith("!"), line.lstrip("!").rstrip("/")))
    out = []
    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(ROOT).as_posix()
        excluded = False
        for neg, pat in rules:
            pat2 = pat[3:] if pat.startswith("**/") else pat
            parts = rel.split("/")
            hit = (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(parts[-1], pat2) or
                   any(fnmatch.fnmatch("/".join(parts[:i]), pat) for i in range(1, len(parts))) or
                   any(fnmatch.fnmatch(seg, pat2) for seg in parts[:-1]))
            if hit:
                excluded = not neg
        if not excluded:
            out.append(rel)
    return out


def test_build_context_has_the_app_and_never_secrets_or_data():
    files = _context_files()
    for need in ("app/main.py", "config.yaml", "requirements.txt", "scripts/healthcheck.py",
                 "scripts/backup_db.py", "strategies/aria.yaml", ".env.example"):
        assert need in files, need
    assert len([f for f in files if f.startswith("strategies/")]) == 10
    for f in files:
        assert not f.startswith(("data/", "logs/", "tests/", ".git/")), f
        assert f != ".env" and not f.endswith((".db", ".db-wal", ".db-shm", ".pyc")), f
        assert "__pycache__" not in f and ".pytest_cache" not in f


# -------------------------------------------------------------------- compose
@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.repository.yml"])
def test_compose_service_is_safe_and_persistent(name):
    c = compose(name)
    assert "version" not in c                                   # obsolete key, Compose v2 warns
    assert list(c["services"]) == ["analyzer"]                  # single service, nothing else
    svc = c["services"]["analyzer"]
    assert svc["restart"] == "unless-stopped"
    assert "ports" not in svc and "expose" not in svc
    assert not svc.get("privileged") and "network_mode" not in svc
    assert all("docker.sock" not in v for v in svc["volumes"])
    assert svc["cap_drop"] == ["ALL"] and "no-new-privileges:true" in svc["security_opt"]
    assert svc["read_only"] is True and any(t.startswith("/tmp") for t in svc["tmpfs"])
    mounts = dict(v.split(":")[::-1] for v in svc["volumes"])
    assert set(mounts) == {"/app/data", "/app/logs"}
    assert c["volumes"]["analyzer-data"]["name"] == "deriv-analyzer-data"
    assert c["volumes"]["analyzer-logs"]["name"] == "deriv-analyzer-logs"
    assert svc["networks"] == ["analyzer-net"] and c["networks"]["analyzer-net"]["name"] == "deriv-analyzer-net"
    assert svc["healthcheck"]["test"] == ["CMD", "python", "/app/scripts/healthcheck.py"]
    assert svc["stop_signal"] == "SIGTERM" and svc["stop_grace_period"] == "45s"
    assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "5"}
    assert int(svc["mem_limit"].rstrip("m")) >= 1000           # ~4.6x the measured 325 MB peak
    assert svc["image"] == "deriv-market-analyzer:1.0.0"


def test_compose_defaults_to_dry_run_and_passes_secrets_only_from_environment():
    raw = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert not SECRET_RE.search(raw)
    env = compose()["services"]["analyzer"]["environment"]
    assert env == {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "ANALYZER_DRY_RUN": "true",
                   "ANALYZER_CONFIG": "/app/config.yaml"}
    env2 = compose(env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-100", "ANALYZER_DRY_RUN": "false"})
    e2 = env2["services"]["analyzer"]["environment"]
    assert e2["TELEGRAM_BOT_TOKEN"] == "t" and e2["ANALYZER_DRY_RUN"] == "false"


def test_repository_compose_differs_only_by_build_section():
    a, b = compose(), compose("docker-compose.repository.yml")
    build = b["services"]["analyzer"].pop("build")
    assert build == {"context": ".", "dockerfile": "Dockerfile"} and a == b


def test_every_runtime_write_path_is_inside_a_volume():
    s = load_settings(ROOT / "config.yaml")
    for p in (s.storage.db_path, s.heartbeat.file):
        assert Path(p).parts[0] == "data", p
    assert s.logging.dir == "logs"


def test_env_example_has_safe_defaults_and_no_values():
    ex = (ROOT / ".env.example").read_text(encoding="utf-8")
    vals = dict(l.split("=", 1) for l in ex.splitlines() if l and not l.startswith("#"))
    assert vals == {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "ANALYZER_DRY_RUN": "true"}


# ----------------------------------------------------------- env var loading
@pytest.mark.parametrize("raw,expected", [("true", True), ("TRUE", True), ("1", True), ("yes", True),
                                          ("false", False), ("0", False), ("", False)])
def test_dry_run_env_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("ANALYZER_DRY_RUN", raw)
    assert load_settings(ROOT / "config.yaml").dry_run is expected


def test_telegram_env_loaded_and_never_in_repr(monkeypatch):
    tok = "123456789:AAEhBOweik9ai2o4ifjfi4jf4jf4jf4jf4j"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", tok)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    s = load_settings(ROOT / "config.yaml")
    assert s.telegram_configured and s.telegram_token == tok and s.telegram_chat_id == "-1001234567890"
    assert tok not in repr(s) and tok not in str(s)


# --------------------------------------------------------------- healthcheck
def test_healthcheck_rules():
    now = 1_800_000_000
    ok = {"written_at": now - 20, "connected": True, "last_tick_wall": now - 2, "monitored_symbols": ["R_10"]}
    assert healthcheck.check(ok, now, 120, 300)[0]
    assert not healthcheck.check(None, now, 120, 300)[0]
    assert not healthcheck.check(dict(ok, written_at=now - 400), now, 120, 300)[0]
    # short disconnects are normal (reconnect within seconds): still healthy
    assert healthcheck.check(dict(ok, connected=False, last_tick_wall=now - 60), now, 120, 300)[0]
    bad, why = healthcheck.check(dict(ok, connected=False, last_tick_wall=now - 600), now, 120, 300)
    assert not bad and "disconnected" in why


def test_healthcheck_script_reads_the_configured_heartbeat(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.yaml"
    hb = tmp_path / "hb.json"
    cfg.write_text(f'heartbeat: {{file: "{hb.as_posix()}"}}\n', encoding="utf-8")
    monkeypatch.setenv("ANALYZER_CONFIG", str(cfg))
    assert healthcheck.main([]) == 1
    write_heartbeat(hb, {"connected": True, "last_tick_wall": int(time.time())})
    assert healthcheck.main([]) == 0 and "ok:" in capsys.readouterr().out


# ----------------------------------------------------------- backup / restore
def test_backup_while_writing_then_restore(tmp_path):
    db = tmp_path / "data" / "analyzer.db"
    conn = init_db(db)
    repo.insert_ticks(conn, [("R_10", e, "4849.430", 0) for e in range(1, 50001)])
    conn.execute("INSERT INTO signal_state(bot, symbol, active) VALUES ('delta','R_10',1)")
    stop = threading.Event()

    def writer():                                       # the live service keeps inserting
        c = init_db(db)
        e = 50001
        while not stop.is_set():
            repo.insert_ticks(c, [("R_10", e, "4849.431", 1)])
            e += 1
        c.close()
    t = threading.Thread(target=writer)
    t.start()
    try:
        out = backup_db.backup(db, tmp_path / "data" / "backups", keep=2)
    finally:
        stop.set()
        t.join()
    b = sqlite3.connect(out)
    assert b.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert b.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] >= 50000
    assert b.execute("SELECT active FROM signal_state").fetchone()[0] == 1
    b.close()
    # retention (later timestamps: the first backup is pruned, the newest two stay)
    for i in range(3):
        out = backup_db.backup(db, tmp_path / "data" / "backups", keep=2, now=1_800_000_000 + i)
    assert len(list((tmp_path / "data" / "backups").glob("analyzer-*.db"))) == 2 and out.exists()
    # restore refuses while the service is alive
    hb = tmp_path / "data" / "heartbeat.json"
    write_heartbeat(hb, {})
    with pytest.raises(RuntimeError, match="running"):
        restore_db.restore(out, db, hb)
    conn.execute("DELETE FROM signal_state")
    conn.close()
    kept = restore_db.restore(out, db, hb, now=time.time() + 600)
    assert kept is not None and kept.exists()
    assert not db.with_name(db.name + "-wal").exists()
    c = sqlite3.connect(db)
    assert c.execute("SELECT active FROM signal_state").fetchone()[0] == 1
    c.close()


def test_restore_rejects_a_non_analyzer_file(tmp_path):
    junk = tmp_path / "x.db"
    sqlite3.connect(junk).execute("CREATE TABLE t(a)").connection.close()
    with pytest.raises(RuntimeError, match="not a valid"):
        restore_db.restore(junk, tmp_path / "a.db", tmp_path / "none.json")


# ---------------------------------------------------------- graceful shutdown
class IdleClient:
    """Stands in for the Deriv client: never connects, stops on request."""
    connected = False
    connect_count = 0

    def __init__(self):
        self._stop = asyncio.Event()
        self.stopped = False

    async def run(self):
        await self._stop.wait()

    async def stop(self):
        self.stopped = True
        self._stop.set()

    async def wait_connected(self, timeout=None):
        await asyncio.Event().wait()


def _app(tmp_path):
    from app.main import Application
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"""
storage: {{db_path: "{(tmp_path / 'data' / 'a.db').as_posix()}"}}
logging: {{dir: "{(tmp_path / 'logs').as_posix()}"}}
heartbeat: {{file: "{(tmp_path / 'data' / 'hb.json').as_posix()}"}}
""", encoding="utf-8")
    app = Application(load_settings(cfg, dry_run=True))
    app.client = app.collector.client = IdleClient()
    return app


def _run_and_stop(app, trigger):
    async def go():
        task = asyncio.create_task(app.run())
        await asyncio.sleep(0.5)
        trigger()
        await asyncio.wait_for(task, 20)
    t0 = time.monotonic()
    asyncio.run(go())
    return time.monotonic() - t0


def test_graceful_shutdown_on_stop_request(tmp_path, caplog):
    app = _app(tmp_path)
    caplog.set_level("INFO")
    elapsed = _run_and_stop(app, app.request_stop)
    assert elapsed < 10 and app.client.stopped and not app.crashed
    assert "event=analyzer_stopping" in caplog.text and "event=analyzer_stopped" in caplog.text
    assert all(t.done() for t in app._tasks)
    c = sqlite3.connect(tmp_path / "data" / "a.db")
    assert c.execute("SELECT value FROM meta WHERE key='last_alive'").fetchone() is not None
    c.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handlers (Docker/Linux only)")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_graceful_shutdown_on_posix_signal(tmp_path, caplog, sig):
    app = _app(tmp_path)
    caplog.set_level("INFO")
    elapsed = _run_and_stop(app, lambda: os.kill(os.getpid(), sig))
    assert elapsed < 10 and app.client.stopped and not app.crashed
    assert "event=analyzer_stopped" in caplog.text
