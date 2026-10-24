#!/usr/bin/env python3
"""Collect public Mastodon aggregates and render a standalone dashboard. No dependencies."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SERVERS = ("datasci.social", "journa.host", "mastodon.social")
WINDOW_DAYS = 28
TEMPLATE = Path(__file__).with_name("dashboard.html")


class FetchError(Exception):
    def __init__(self, message, retry_at=0):
        super().__init__(message)
        self.retry_at = retry_at


def retry_deadline(headers, now, fallback=0):
    """Respect the later of Retry-After and X-RateLimit-Reset (UTC)."""
    deadlines = [fallback]
    retry = headers.get("Retry-After", "")
    if retry:
        try:
            deadlines.append(now + max(0, int(retry)))
        except ValueError:
            try:
                deadlines.append(parsedate_to_datetime(retry).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
    reset = headers.get("X-RateLimit-Reset", "")
    if reset:
        try:
            deadlines.append(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return max(deadlines)


def fetch_json(url, user_agent):
    request = Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Language": "en",
    })
    try:
        with urlopen(request, timeout=20) as response:
            headers = response.headers
            for header in ("Deprecation", "Sunset"):
                if headers.get(header):
                    print(f"NOTICE {url}: {header}: {headers[header]}", file=sys.stderr)
            exhausted = headers.get("X-RateLimit-Remaining") == "0"
            now = time.time()
            deadline = retry_deadline(headers, now, now + 900 if exhausted else 0) if exhausted or headers.get("Retry-After") else 0
            return json.load(response), deadline
    except HTTPError as exc:
        now = time.time()
        deadline = retry_deadline(exc.headers, now, now + 900 if exc.code == 429 else 0) if exc.code == 429 or exc.headers.get("Retry-After") else 0
        exc.close()
        raise FetchError(f"HTTP {exc.code}", deadline) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise FetchError(f"Request failed: {exc}") from exc


def nonnegative_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Expected an integer count")
    result = int(value)
    if result < 0:
        raise ValueError("Expected a nonnegative count")
    return result


def parse_trends(payload):
    if not isinstance(payload, list):
        raise ValueError("Expected a list of tags")
    tags = []
    for tag in payload[:10]:
        name = tag["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("Expected a tag name")
        history = [{
            "day": nonnegative_int(item["day"]),
            "uses": nonnegative_int(item["uses"]),
            "accounts": nonnegative_int(item["accounts"]),
        } for item in tag["history"]]
        if len(history) != 7 or len({item["day"] for item in history}) != 7:
            raise ValueError("Expected seven distinct daily history buckets")
        tags.append({"name": name, "history": sorted(history, key=lambda item: item["day"])})
    return tags


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive_run(database):
    """A second overlapping cron run should exit, not duplicate requests."""
    lock_path = Path(str(database) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another poll is already running") from None
        yield


def render(data, output):
    # API-supplied names must not be able to close the inline JSON script element.
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    template = TEMPLATE.read_text(encoding="utf-8")
    atomic_write(output, template.replace("__DASHBOARD_DATA__", payload), mode=0o644)


def collect(database, output, user_agent, fetch=fetch_json, now=None):
    now = int(time.time() if now is None else now)
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    cooldown_path = database.with_name(database.name + ".cooldowns.json")
    # Persist only server cooldowns, not API payloads. This survives cron exits.
    cooldowns = json.loads(cooldown_path.read_text()) if cooldown_path.exists() else {}
    report = {"generated_at": now, "window_days": WINDOW_DAYS, "servers": []}
    errors = 0

    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS samples (
            server TEXT NOT NULL,
            fetched_at_utc INTEGER NOT NULL,
            status_count INTEGER NOT NULL CHECK(status_count >= 0),
            PRIMARY KEY (server, fetched_at_utc)
        )""")
        for server in SERVERS:
            panel = {"name": server, "activity_error": None, "trends_error": None, "tags": None}
            endpoints = (("activity", "/api/v1/instance"), ("trends", "/api/v1/trends/tags?limit=10"))
            for kind, path in endpoints:
                if cooldowns.get(server, 0) > now:
                    until = datetime.fromtimestamp(cooldowns[server], timezone.utc).isoformat()
                    panel[kind + "_error"] = f"Paused until {until} (server rate limit)"
                    errors += 1
                    continue
                try:
                    payload, deadline = fetch(f"https://{server}{path}", user_agent)
                    if deadline > now:
                        cooldowns[server] = deadline
                        atomic_write(cooldown_path, json.dumps(cooldowns))
                    if kind == "activity":
                        count = nonnegative_int(payload["stats"]["status_count"])
                        db.execute("INSERT OR IGNORE INTO samples VALUES (?, ?, ?)", (server, now, count))
                    else:
                        panel["tags"] = parse_trends(payload)
                except (FetchError, ValueError, KeyError, TypeError) as exc:
                    if isinstance(exc, FetchError) and exc.retry_at > now:
                        cooldowns[server] = exc.retry_at
                        atomic_write(cooldown_path, json.dumps(cooldowns))
                    panel[kind + "_error"] = str(exc) or "Invalid response"
                    errors += 1
                    print(f"WARNING {server} {kind}: {exc}", file=sys.stderr)
            panel["samples"] = db.execute(
                "SELECT fetched_at_utc, status_count FROM samples "
                "WHERE server = ? AND fetched_at_utc >= ? ORDER BY fetched_at_utc",
                (server, now - WINDOW_DAYS * 86400),
            ).fetchall()
            report["servers"].append(panel)
        db.execute("DELETE FROM samples WHERE fetched_at_utc < ?", (now - WINDOW_DAYS * 86400,))
    render(report, output)
    print(f"Wrote {output} ({len(SERVERS)} servers, {errors} unavailable sources)")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/stats.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("out/index.html"))
    parser.add_argument("--contact", default="https://datafrosch.fun", help="Public contact URL for the User-Agent")
    args = parser.parse_args()
    try:
        with exclusive_run(args.database):
            collect(args.database, args.output, f"mastodon-stats/1.0 (+{args.contact})")
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
