"""
NHL Injury Agent
----------------
Reads CBS Sports' NHL injury report (https://www.cbssports.com/nhl/injuries/),
compares it to the last saved snapshot, and records every status change.

Files it reads and writes (in the data/ folder):
  snapshot.json  - every player's latest known status
  log.json       - list of status changes, newest first
  meta.json      - when the agent last ran and what it found

Uses only Python's built-in libraries, so there is nothing to install.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

CBS_URL = "https://www.cbssports.com/nhl/injuries/"

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
LOG_FILE = DATA_DIR / "log.json"
META_FILE = DATA_DIR / "meta.json"

MAX_LOG_ENTRIES = 1000

# If CBS suddenly returns far fewer players than last time, the page has
# probably changed or failed to load. Stop rather than mark everyone "Cleared".
SAFETY_MIN_SHARE = 0.4   # must find at least 40% of last run's players...
SAFETY_APPLIES_ABOVE = 15  # ...when last run had more than this many


# ---------- helpers ----------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


# ---------- step 1: fetch and read the CBS page ----------

def fetch_cbs():
    req = urllib.request.Request(CBS_URL, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


class CBSInjuryParser(HTMLParser):
    """Reads the team-by-team injury tables on the CBS page.

    Each team has a heading with a link to /nhl/teams/XXX/..., followed by a
    table with the columns Player, Position, Updated, Injury, Injury Status.
    """

    TEAM_LINK = re.compile(r"/nhl/teams/[A-Z]{2,4}/")
    PLAYER_LINK = re.compile(r"/nhl/players/\d+/")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.team = None
        self.in_table = 0
        self.in_cell = False
        self.cell_text = []
        self.cell_links = []
        self.row = None
        self.headers = None
        self.link_kind = None
        self.link_text = []
        self.tables = []   # list of (team, headers, rows)
        self.cur_rows = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.in_table += 1
            self.headers, self.cur_rows = [], []
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag in ("td", "th"):
            self.in_cell = True
            self.cell_text, self.cell_links = [], []
        elif tag == "a":
            href = a.get("href", "")
            if self.in_cell and self.PLAYER_LINK.search(href):
                self.link_kind, self.link_text = "player", []
            elif not self.in_table and self.TEAM_LINK.search(href):
                self.link_kind, self.link_text = "team", []

    def handle_endtag(self, tag):
        if tag == "a" and self.link_kind:
            text = clean("".join(self.link_text))
            if self.link_kind == "player" and text:
                self.cell_links.append(text)
            elif self.link_kind == "team" and text:
                self.team = text
            self.link_kind = None
        elif self.in_table and tag in ("td", "th") and self.in_cell:
            self.in_cell = False
            cell = {"text": clean("".join(self.cell_text)), "players": self.cell_links}
            if self.row is not None:
                self.row.append((tag, cell))
        elif self.in_table and tag == "tr" and self.row is not None:
            if self.row and all(t == "th" for t, _ in self.row):
                self.headers = [c["text"].lower() for _, c in self.row]
            elif self.row:
                self.cur_rows.append([c for _, c in self.row])
            self.row = None
        elif tag == "table" and self.in_table:
            self.in_table -= 1
            if self.team and self.headers:
                self.tables.append((self.team, self.headers, self.cur_rows))

    def handle_data(self, data):
        if self.in_cell:
            self.cell_text.append(data)
        if self.link_kind:
            self.link_text.append(data)


def parse_cbs(html):
    """Returns {player_name: {team, position, injury, status, comment}}."""
    parser = CBSInjuryParser()
    parser.feed(html)
    parsed = {}
    for team, headers, rows in parser.tables:
        def col(name):
            for i, h in enumerate(headers):
                if h.startswith(name):
                    return i
            return None
        i_player, i_pos, i_inj = col("player"), col("position"), col("injury")
        i_status = col("injury status")
        if i_status is not None and i_inj == i_status:
            # "injury" matched "injury status"; find the plain Injury column
            i_inj = next((i for i, h in enumerate(headers) if h == "injury"), None)
        if i_player is None or i_status is None:
            continue
        for cells in rows:
            if len(cells) <= max(i_player, i_status):
                continue
            pc = cells[i_player]
            # the full name is the longest link ("Ian Moore" rather than "I. Moore")
            name = max(pc["players"], key=len) if pc["players"] else pc["text"]
            status = cells[i_status]["text"]
            if not name or not status:
                continue
            position = cells[i_pos]["text"].upper() if i_pos is not None else ""
            injury = cells[i_inj]["text"] if i_inj is not None else ""
            parsed[name] = {
                "team": team, "position": position, "injury": injury, "status": status,
                "comment": " ".join(p for p in (position, injury) if p),
            }
    return parsed


# ---------- step 2: compare (same rules as the NFL tracker) ----------

def diff_and_log(snapshot, new_parsed, now_iso):
    changes = []
    first_import = len(snapshot) == 0

    for player, nxt in new_parsed.items():
        prev = snapshot.get(player)
        if prev is None:
            if not first_import:
                changes.append({"ts": now_iso, "player": player, "team": nxt["team"],
                                "from": "Available", "to": nxt["status"]})
        elif prev.get("status") != nxt["status"]:
            changes.append({"ts": now_iso, "player": player, "team": nxt["team"],
                            "from": prev.get("status"), "to": nxt["status"]})
        snapshot[player] = {**nxt, "updatedAt": now_iso}

    for player, prev in list(snapshot.items()):
        if player not in new_parsed and prev.get("status") != "Cleared":
            changes.append({"ts": now_iso, "player": player, "team": prev.get("team"),
                            "from": prev.get("status"), "to": "Cleared"})
            snapshot[player] = {**prev, "status": "Cleared", "updatedAt": now_iso}

    return changes, first_import


# ---------- main ----------

def main():
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Running NHL injury agent at {now_iso}")

    try:
        html = fetch_cbs()
    except Exception as e:
        print(f"ERROR: could not reach CBS Sports: {e}")
        print("Nothing was changed. The agent will try again on its next run.")
        sys.exit(1)

    new_parsed = parse_cbs(html)
    teams = len({p["team"] for p in new_parsed.values()})
    print(f"Found {len(new_parsed)} players across {teams} teams on CBS's injury report.")

    snapshot = load_json(SNAPSHOT_FILE, {})
    log = load_json(LOG_FILE, [])

    if not new_parsed:
        print("ERROR: no players found on the CBS page. The page layout may have changed.")
        print("Nothing was changed.")
        sys.exit(1)
    active_before = sum(1 for p in snapshot.values() if p.get("status") != "Cleared")
    if active_before > SAFETY_APPLIES_ABOVE and len(new_parsed) < active_before * SAFETY_MIN_SHARE:
        print(f"ERROR: only {len(new_parsed)} players found, but {active_before} were listed last time.")
        print("The CBS page may be incomplete right now. Nothing was changed.")
        sys.exit(1)

    changes, first_import = diff_and_log(snapshot, new_parsed, now_iso)
    log = (changes + log)[:MAX_LOG_ENTRIES]

    save_json(SNAPSHOT_FILE, snapshot)
    save_json(LOG_FILE, log)
    save_json(META_FILE, {
        "lastChecked": now_iso,
        "playerCount": len(new_parsed),
        "lastRunChanges": len(changes),
        "firstRun": first_import,
        "source": "CBS Sports",
    })

    if first_import:
        print(f"Baseline saved: {len(new_parsed)} players. Future runs will log changes.")
    else:
        print(f"{len(changes)} status change(s) logged:")
        for c in changes:
            print(f"  {c['player']} ({c['team']}): {c['from']} -> {c['to']}")


if __name__ == "__main__":
    main()
