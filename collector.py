"""Collects nicknames from recent matches of source players via the PUBG API and keeps them in SQLite.

Every collected player is checked for a clan right away; only players without a clan become "valid".
Players the API no longer returns (renamed, banned) are deleted, and so are players not seen for N days.
"""
import gzip
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from i18n import t

DAY = 86400


class Stopped(Exception):
    pass


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- PUBG API

class PubgApi:
    """The /players endpoint is rate-limited (10 req/min for a default key); /matches is not."""

    def __init__(self, get_cfg):
        self.get_cfg = get_cfg
        self.lock = threading.Lock()
        self.next_allowed = 0.0
        self.rpm = 10

    def _wait(self, until, tick):
        while time.monotonic() < until:
            tick()
            time.sleep(min(0.2, max(0.0, until - time.monotonic())))

    def get(self, path, tick, limited=True):
        cfg = self.get_cfg()
        key = cfg.get("api_key", "").strip()
        if not key:
            raise ApiError(t("e_no_key"))
        url = f"https://api.pubg.com/shards/{cfg.get('shard', 'steam')}/{path}"
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/vnd.api+json",
                   "Accept-Encoding": "gzip"}
        for attempt in range(5):
            if limited:
                with self.lock:
                    self._wait(self.next_allowed, tick)
                    self.next_allowed = time.monotonic() + 60 / max(1, self.rpm) + 0.3
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:
                    limit = r.headers.get("X-Ratelimit-Limit")
                    if limited and limit and limit.isdigit():
                        self.rpm = int(limit)
                    body = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {"data": []}
                if e.code == 401:
                    raise ApiError(t("e_bad_key"))
                if e.code == 429:
                    reset = e.headers.get("X-Ratelimit-Reset")
                    wait = max(5, int(reset) - time.time() + 1) if reset and reset.isdigit() else 60
                    with self.lock:
                        self.next_allowed = time.monotonic() + wait
                    continue
                if attempt == 4:
                    raise ApiError(t("e_http", code=e.code))
                self._wait(time.monotonic() + 5, tick)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 4:
                    raise ApiError(t("e_net", err=e))
                self._wait(time.monotonic() + 10, tick)
        raise ApiError(t("e_request"))

    def players_by_names(self, names, tick):
        q = urllib.parse.quote(",".join(names), safe=",")
        return self.get(f"players?filter[playerNames]={q}", tick).get("data", [])

    def players_by_ids(self, ids, tick):
        return self.get(f"players?filter[playerIds]={','.join(ids)}", tick).get("data", [])

    def match(self, match_id, tick):
        return self.get(f"matches/{match_id}", tick, limited=False)


def parse_time(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# ---------------------------------------------------------------- storage

class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS players(
                account_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                first_seen INTEGER, last_seen INTEGER,
                clan_id TEXT, clan_checked_at INTEGER, invited_at INTEGER);
            CREATE INDEX IF NOT EXISTS players_last_seen ON players(last_seen);
            CREATE TABLE IF NOT EXISTS matches(match_id TEXT PRIMARY KEY, created_at INTEGER, processed_at INTEGER);
            CREATE TABLE IF NOT EXISTS invited_names(name_lower TEXT PRIMARY KEY, invited_at INTEGER, result TEXT);
        """)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(invited_names)")]
        if "result" not in cols:
            self.db.execute("ALTER TABLE invited_names ADD COLUMN result TEXT")
        self.db.commit()

    def _exec(self, sql, args=(), many=False):
        with self.lock:
            cur = self.db.executemany(sql, args) if many else self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _query(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def processed_matches(self, ids):
        if not ids:
            return set()
        rows = self._query(f"SELECT match_id FROM matches WHERE match_id IN ({','.join('?' * len(ids))})", ids)
        return {r[0] for r in rows}

    def mark_match(self, match_id, created_at):
        self._exec("INSERT OR REPLACE INTO matches VALUES(?,?,?)", (match_id, created_at, int(time.time())))

    def upsert_seen(self, rows):
        """rows: (account_id, name, seen_at)"""
        self._exec("""
            INSERT INTO players(account_id, name, first_seen, last_seen) VALUES(?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                name = CASE WHEN excluded.last_seen >= players.last_seen THEN excluded.name ELSE players.name END,
                last_seen = MAX(players.last_seen, excluded.last_seen)
        """, [(a, n, t, t) for a, n, t in rows], many=True)

    def needing_check(self, ids, recheck_before):
        if not ids:
            return []
        rows = self._query(f"""
            SELECT account_id FROM players WHERE account_id IN ({','.join('?' * len(ids))})
              AND invited_at IS NULL AND (clan_checked_at IS NULL OR clan_checked_at < ?)""",
                           (*ids, recheck_before))
        return [r[0] for r in rows]

    def set_clan(self, results, checked_ids):
        """results: {account_id: (name, clan_id)}; ids the API did not return are deleted."""
        now = int(time.time())
        gone = [(a,) for a in checked_ids if a not in results]
        self._exec("UPDATE players SET name=?, clan_id=?, clan_checked_at=? WHERE account_id=?",
                   [(n, c, now, a) for a, (n, c) in results.items()], many=True)
        if gone:
            self._exec("DELETE FROM players WHERE account_id=?", gone, many=True)
        return len(gone)

    def delete_player(self, account_id):
        self._exec("DELETE FROM players WHERE account_id=?", (account_id,))

    def set_has_clan(self, account_id):
        self._exec("UPDATE players SET clan_id='?', clan_checked_at=? WHERE account_id=?",
                   (int(time.time()), account_id))

    def purge_stale(self, cutoff):
        return self._exec("DELETE FROM players WHERE last_seen < ? AND invited_at IS NULL", (cutoff,)).rowcount

    def valid(self, cutoff, n):
        invited = {r[0] for r in self._query("SELECT name_lower FROM invited_names")}
        rows = self._query("""
            SELECT account_id, name FROM players
            WHERE clan_checked_at IS NOT NULL AND clan_id = '' AND invited_at IS NULL AND last_seen >= ?
            ORDER BY last_seen DESC""", (cutoff,))
        return [r for r in rows if r[1].lower() not in invited][:n]

    def mark_invited(self, account_id, name, result):
        now = int(time.time())
        self._exec("UPDATE players SET invited_at=? WHERE account_id=?", (now, account_id))
        self._exec("INSERT OR REPLACE INTO invited_names VALUES(?,?,?)", (name.lower(), now, result))

    def stats(self, cutoff):
        q = lambda sql, *a: self._query(sql, a)[0][0]
        today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return {
            "valid": len(self.valid(cutoff, 10 ** 9)),
            "checked": q("SELECT COUNT(*) FROM players WHERE clan_checked_at IS NOT NULL"),
            "with_clan": q("SELECT COUNT(*) FROM players WHERE clan_id != '' AND clan_id IS NOT NULL"),
            "invited_total": q("SELECT COUNT(*) FROM invited_names WHERE result IS NULL OR result='ok'"),
            "invited_today": q("SELECT COUNT(*) FROM invited_names WHERE invited_at >= ? "
                               "AND (result IS NULL OR result='ok')", today),
            "matches": q("SELECT COUNT(*) FROM matches"),
        }


def check_clans(api, store, ids, tick):
    """Checks clans in batches of 10 ids per request. Returns ({account_id: (name, clan_id)}, deleted_count)."""
    found, deleted = {}, 0
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        res = {p["id"]: (p["attributes"]["name"], (p["attributes"].get("clanId") or "").strip())
               for p in api.players_by_ids(batch, tick)}
        deleted += store.set_clan(res, batch)
        found.update(res)
    return found, deleted


# ---------------------------------------------------------------- background collector

class Collector:
    def __init__(self, get_cfg, api, store, log):
        self.get_cfg = get_cfg
        self.api = api
        self.store = store
        self.log = log
        self.running = False
        self.stop_event = threading.Event()
        self.thread = None
        self.state = "stopped"      # stopped | running | waiting | error
        self.detail = ""
        self.next_run = 0.0

    def tick(self):
        if self.stop_event.is_set():
            raise Stopped()

    def start(self):
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        self.state, self.detail = "stopped", ""

    def loop(self):
        while not self.stop_event.is_set():
            cfg = self.get_cfg()
            self.state, self.detail = "running", t("d_find_matches")
            try:
                self.cycle()
                interval = max(5, int(cfg.get("collect_interval_min", 15))) * 60
                self.next_run = time.time() + interval
                self.state = "waiting"
                self.detail = t("d_next_check", time=f"{datetime.fromtimestamp(self.next_run):%H:%M}")
            except Stopped:
                break
            except ApiError as e:
                self.state, self.detail = "error", str(e)
                self.log(t("l_collect_err", err=e))
                interval = 120
            except Exception as e:
                self.state, self.detail = "error", repr(e)
                self.log(t("l_collect_err", err=repr(e)))
                interval = 120
            if self.stop_event.wait(interval):
                break
        self.state, self.detail = "stopped", ""

    def cycle(self):
        cfg = self.get_cfg()
        now = time.time()
        cutoff = int(now - float(cfg.get("stale_days", 30)) * DAY)
        recheck_before = int(now - float(cfg.get("recheck_clan_days", 7)) * DAY)
        sources = [s.strip() for s in cfg.get("source_players", []) if s.strip()]
        if not sources:
            raise ApiError(t("e_no_sources"))

        purged = self.store.purge_stale(cutoff)
        if purged:
            self.log(t("l_purged", n=purged))

        match_ids = []
        for i in range(0, len(sources), 10):
            batch = sources[i:i + 10]
            got = self.api.players_by_names(batch, self.tick)
            found = {p["attributes"]["name"].lower() for p in got}
            for s in batch:
                if s.lower() not in found:
                    self.log(t("l_source_missing", nick=s))
            for p in got:
                match_ids += [m["id"] for m in p["relationships"]["matches"]["data"]]
        match_ids = list(dict.fromkeys(match_ids))
        done = self.store.processed_matches(match_ids)
        new = [m for m in match_ids if m not in done][:int(cfg.get("max_matches_per_cycle", 60))]
        if not new:
            self.log(t("l_no_new_matches"))
            return
        skip = {s.lower() for s in sources}

        total_valid = total_checked = total_deleted = 0
        for n, mid in enumerate(new, 1):
            self.tick()
            self.detail = t("d_match", n=n, total=len(new))
            m = self.api.match(mid, self.tick)
            created = parse_time(m["data"]["attributes"]["createdAt"])
            if created < cutoff:
                self.store.mark_match(mid, created)
                continue
            rows = []
            for it in m.get("included", []):
                if it.get("type") != "participant":
                    continue
                st = it["attributes"]["stats"]
                pid, name = st.get("playerId", ""), st.get("name", "")
                if pid.startswith("account.") and name and name.lower() not in skip:
                    rows.append((pid, name, created))
            self.store.upsert_seen(rows)
            ids = self.store.needing_check([r[0] for r in rows], recheck_before)
            for i in range(0, len(ids), 10):
                self.detail = t("d_match_check", n=n, total=len(new), i=min(i + 10, len(ids)), k=len(ids))
                res, deleted = check_clans(self.api, self.store, ids[i:i + 10], self.tick)
                total_checked += len(res)
                total_deleted += deleted
                total_valid += sum(1 for _, c in res.values() if not c)
            self.store.mark_match(mid, created)
        self.log(t("l_collect_summary", m=len(new), c=total_checked, v=total_valid)
                 + (t("l_collect_deleted", d=total_deleted) if total_deleted else ""))
