#!/usr/bin/env python3
"""
Scout data builder.

Pulls free football data, merges it per player and writes players.json for the
Scout web app. Every value carries its source, and anything that is an estimate
is flagged as one.

Sources (all free, no API key):
  understat   Top-5 leagues: minutes, goals, xG, npxG, assists, xA, shots, key passes,
              plus per-player shot logs -> headers, set pieces, penalties, in/out box.
  fpl         Premier League only (official Fantasy Premier League API, Opta data):
              date of birth, starts, Opta xG, tackles, recoveries, set-piece duties, injury news.
  transfermarkt  Market value (crowd-sourced ESTIMATE), contract expiry, date of birth, position.
  ecb         EUR->GBP reference rate for the currency toggle.
  model       Wage band: a rule-of-thumb ESTIMATE derived from market value. Not reported pay.

Not available from any free source (columns exist, filled only by CSV import in the app):
  average rating, aerial duels won, successful dribbles, distance covered,
  sprint distance, number of sprints.

Each source is fail-soft: if one breaks, the previous run's values for that source
are kept and the source is marked stale in players.json.

Usage:
  python scripts/build_data.py                       # full live run (GitHub Actions)
  python scripts/build_data.py --only fpl --fpl-csv path/players_raw.csv --fpl-teams path/teams.csv
"""
import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # only needed for Transfermarkt
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "players.json"
CACHE = ROOT / "data_cache"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

LEAGUES = {
    # app name        understat slug   transfermarkt code
    "Premier League": ("EPL",         "GB1"),
    "La Liga":        ("La_liga",     "ES1"),
    "Bundesliga":     ("Bundesliga",  "L1"),
    "Serie A":        ("Serie_A",     "IT1"),
    "Ligue 1":        ("Ligue_1",     "FR1"),
}

# Wage model: annual salary as a share of Transfermarkt value. A rough market
# rule of thumb, shown in the app as an estimate band, never as a reported figure.
WAGE_SHARE_LOW, WAGE_SHARE_HIGH = 0.08, 0.16

MIN_MINUTES_FOR_SHOT_LOG = 90   # skip shot-log requests for fringe players
REQUEST_PAUSE = 0.8             # be polite to free sites


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def season_start_year(today=None):
    today = today or dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


TEAM_ALIASES = {
    "man city": "manchester city", "man utd": "manchester united", "man united": "manchester united",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur", "wolves": "wolverhampton wanderers",
    "nott m forest": "nottingham forest", "newcastle": "newcastle united", "brighton": "brighton hove albion",
    "west ham": "west ham united", "leeds": "leeds united", "sheffield utd": "sheffield united",
    "psg": "paris saint germain", "paris sg": "paris saint germain", "inter": "inter milan",
    "ac milan": "milan", "bayern munich": "bayern munchen", "atletico madrid": "atletico de madrid",
}


def team_key(s):
    n = norm(s)
    n = TEAM_ALIASES.get(n, n)
    for junk in (" fc", " cf", " afc", "fc ", "afc ", " calcio", " ssc", " ac", " as ", " 1 ", " sc "):
        n = n.replace(junk, " ")
    return re.sub(r"\s+", " ", n).strip()


def best_match(name, candidates, cutoff=0.82):
    """candidates: dict normalized_name -> obj. Tries exact, surname+initial, then fuzzy."""
    n = norm(name)
    if n in candidates:
        return candidates[n]
    parts = n.split()
    if len(parts) >= 2:
        for k, v in candidates.items():
            kp = k.split()
            if kp and kp[-1] == parts[-1] and kp[0][0] == parts[0][0]:
                return v
    m = difflib.get_close_matches(n, list(candidates), n=1, cutoff=cutoff)
    return candidates[m[0]] if m else None


SESSION = requests.Session()
ERRORS = {}      # source -> short reason, shown in the app's Data & sources sheet
LAST_ERR = [""]  # reason for the most recent failed request


def note(source, msg):
    log(f"{source}: {msg}")
    ERRORS.setdefault(source, [])
    if msg not in ERRORS[source] and len(ERRORS[source]) < 4:
        ERRORS[source].append(msg)


def get(url, tries=3, **kw):
    headers = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"}
    headers.update(kw.pop("headers", {}))
    for attempt in range(tries):
        try:
            r = SESSION.get(url, headers=headers, timeout=45, **kw)
            if r.status_code == 200:
                return r
            LAST_ERR[0] = f"HTTP {r.status_code} from {url.split('/')[2]}"
            log(f"  {r.status_code} {url} :: {r.text[:160]!r}")
            if r.status_code in (403, 404, 405, 410):
                break  # retrying a block or a missing page will not help
        except requests.RequestException as e:
            LAST_ERR[0] = f"{type(e).__name__} reaching {url.split('/')[2]}"
            log(f"  error {url}: {e}")
        time.sleep(2 * (attempt + 1))
    return None


# --------------------------------------------------------------------------- understat

US_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}


def describe_page(content):
    """Short description of a non-JSON reply, so the app can show what Understat actually sent."""
    text = content[:4000].decode("utf-8", "replace")
    t = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    title = re.sub(r"\s+", " ", t.group(1)).strip()[:60] if t else ""
    if re.search(r"cf-chl|challenge-platform|Just a moment|Attention Required", text, re.I):
        kind = "Cloudflare bot check"
    elif "captcha" in text.lower():
        kind = "captcha page"
    else:
        kind = "web page"
    return f"{kind}{' “' + title + '”' if title else ''}, {len(content):,} bytes"


def parse_json(content):
    try:
        return json.loads(content.decode("utf-8-sig"))  # tolerates a byte-order mark
    except (ValueError, UnicodeDecodeError):
        return None


def understat_json(path, referer):
    """GET an Understat AJAX endpoint. Falls back to a browser-like client if the plain one is refused."""
    url = f"https://understat.com/{path}"
    r = get(url, headers={**US_HEADERS, "Referer": referer})
    if r is not None:
        data = parse_json(r.content)
        if data is not None:
            return data
        LAST_ERR[0] = describe_page(r.content)
    if impersonated is None:
        return None
    try:
        r2 = impersonated.get(url, headers={**US_HEADERS, "Referer": referer}, impersonate="chrome", timeout=45)
        if r2.status_code == 200:
            data = parse_json(r2.content)
            if data is not None:
                US_VIA[0] = "browser-like client"
                return data
            LAST_ERR[0] += f"; browser client also got {describe_page(r2.content)}"
        else:
            LAST_ERR[0] += f"; browser client got HTTP {r2.status_code}"
    except Exception as e:
        LAST_ERR[0] += f"; browser client error {type(e).__name__}"
    return None


try:
    from curl_cffi import requests as impersonated  # mimics Chrome's network fingerprint
except ImportError:
    impersonated = None
US_VIA = [""]

def fetch_understat(season):
    """Returns {league: [player dicts]} with season totals."""
    out = {}
    get("https://understat.com/", tries=1)  # picks up any session cookie the site sets
    for league, (slug, _) in LEAGUES.items():
        players = None
        for sl in dict.fromkeys([slug, slug.replace("_liga", "_Liga"), slug.replace("_Liga", "_liga")]):
            LAST_ERR[0] = ""
            data = understat_json(f"getLeagueData/{sl}/{season}", f"https://understat.com/league/{sl}/{season}")
            if not isinstance(data, dict):
                continue
            players = data.get("players", [])
            if isinstance(players, dict):
                players = list(players.values())
            if players:
                break
        if not players:
            note("understat", f"{league}: {LAST_ERR[0] or 'no players returned'}")
            continue
        out[league] = players
        log(f"understat: {league} {len(players)} players")
        time.sleep(REQUEST_PAUSE)
    return out


def summarise_shots(shots, season):
    """Shot log -> set-piece, header, penalty and box splits for one season."""
    s = dict(shots_in=0, shots_out=0, head_goals=0, head_shots=0, sp_goals=0, sp_xg=0.0,
             sp_shots=0, pen_taken=0, pen_scored=0)
    for sh in shots:
        if str(sh.get("season")) != str(season):
            continue
        x, y = float(sh.get("X", 0)), float(sh.get("Y", 0))
        goal = sh.get("result") == "Goal"
        situ = sh.get("situation", "")
        # Understat pitch is 0-1 along the length; penalty area ~ x >= 0.843, 0.2035 <= y <= 0.7965
        if x >= 0.843 and 0.2035 <= y <= 0.7965:
            s["shots_in"] += 1
        else:
            s["shots_out"] += 1
        if sh.get("shotType") == "Head":
            s["head_shots"] += 1
            s["head_goals"] += goal
        if situ == "Penalty":
            s["pen_taken"] += 1
            s["pen_scored"] += goal
        elif situ in ("FromCorner", "SetPiece", "DirectFreekick"):
            s["sp_shots"] += 1
            s["sp_goals"] += goal
            s["sp_xg"] += float(sh.get("xG", 0))
    s["sp_xg"] = round(s["sp_xg"], 2)
    return s


def fetch_understat_shots(players_by_league, season, cache):
    """Per-player shot logs. Cached by shot count so each daily run only refetches movers."""
    done = failed = 0
    for league, players in players_by_league.items():
        for p in players:
            pid, nshots = str(p["id"]), int(p.get("shots", 0))
            if int(p.get("time", 0)) < MIN_MINUTES_FOR_SHOT_LOG or nshots == 0:
                continue
            c = cache.get(pid)
            if c and c.get("n") == nshots and c.get("season") == season:
                continue
            data = understat_json(f"getPlayerData/{pid}", f"https://understat.com/player/{pid}")
            time.sleep(REQUEST_PAUSE)
            if not isinstance(data, dict):
                failed += 1
                if failed >= 25 and done == 0:
                    note("understat", f"shot logs: {LAST_ERR[0]}")
                    return cache  # site is refusing us; don't hammer it
                continue
            shots = data.get("shots", [])
            cache[pid] = {"n": nshots, "season": season, **summarise_shots(shots, season)}
            done += 1
    log(f"understat shots: refreshed {done} players")
    return cache


# --------------------------------------------------------------------------- FPL

FPL_POS = {"1": "GK", "2": "DEF", "3": "MID", "4": "FWD"}


def fetch_fpl(csv_path=None, teams_path=None):
    """Live FPL API, or a players_raw.csv snapshot (vaastav/Fantasy-Premier-League)."""
    if csv_path:
        import csv
        teams = {t["id"]: t["name"] for t in csv.DictReader(open(teams_path, encoding="utf-8"))}
        rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
        clean = lambda v: "" if v in (None, "None", "nan") else v
        return [{**{k: clean(v) for k, v in r.items()}, "team_name": teams.get(r["team"], "")} for r in rows]
    r = get("https://fantasy.premierleague.com/api/bootstrap-static/")
    if not r:
        reason = LAST_ERR[0]
        log(f"fpl: {reason}; trying the vaastav GitHub mirror")
        rows = fetch_fpl_mirror()
        if rows:
            FPL_VIA[0] = f"GitHub mirror (official API refused: {reason})"
        return rows
    j = r.json()
    teams = {str(t["id"]): t["name"] for t in j["teams"]}
    return [{**{k: "" if v is None else str(v) for k, v in e.items()},
             "team_name": teams.get(str(e["team"]), "")} for e in j["elements"]]


def fetch_fpl_mirror():
    """Same FPL data, as mirrored a few times a week to GitHub by vaastav/Fantasy-Premier-League."""
    import csv, io
    y = season_start_year()
    base = f"https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{y}-{str(y + 1)[-2:]}"
    pr, tr = get(f"{base}/players_raw.csv"), get(f"{base}/teams.csv")
    if not (pr and tr):
        note("fpl", f"mirror: {LAST_ERR[0]}")
        return None
    clean = lambda v: "" if v in (None, "None", "nan") else v
    teams = {t["id"]: t["name"] for t in csv.DictReader(io.StringIO(tr.text))}
    FPL_VIA[0] = "GitHub mirror of the FPL API"
    return [{**{k: clean(v) for k, v in row.items()}, "team_name": teams.get(row["team"], "")}
            for row in csv.DictReader(io.StringIO(pr.text))]


FPL_VIA = [""]
TM_VIA = [""]


# --------------------------------------------------------------------------- Transfermarkt

def parse_money(txt):
    """'€180.00m' / '€750k' / '£12.5m' -> (amount_in_units, currency)"""
    t = txt.strip().replace(",", "")
    m = re.match(r"([€£$])\s*([\d.]+)\s*(bn|m|k|Th\.|Mio\.)?", t)
    if not m:
        return None, None
    cur = {"€": "EUR", "£": "GBP", "$": "USD"}[m.group(1)]
    mult = {"bn": 1e9, "m": 1e6, "Mio.": 1e6, "k": 1e3, "Th.": 1e3, None: 1}[m.group(3)]
    return float(m.group(2)) * mult, cur


def parse_date(txt):
    for fmt in ("%b %d, %Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(txt.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


TM_DATASET = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/players.csv.gz"
TM_COMPS = {code: league for league, (_, code) in LEAGUES.items()}


def fetch_transfermarkt(season):
    rows = fetch_tm_dataset(season)
    if rows and sum(len(v) for v in rows.values()) > 200:
        TM_VIA[0] = "transfermarkt-datasets (refreshed twice weekly)"
        return rows
    TM_VIA[0] = "transfermarkt.com"
    return scrape_transfermarkt(season)


def fetch_tm_dataset(season):
    """dcaribou/transfermarkt-datasets: open CSV export of Transfermarkt, scraped twice a week."""
    import csv, gzip, io
    r = get(TM_DATASET)
    if not r:
        note("transfermarkt", f"dataset: {LAST_ERR[0]}; falling back to the website")
        return None
    try:
        text = gzip.decompress(r.content).decode("utf-8")
    except OSError:
        text = r.text
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        league = TM_COMPS.get(row.get("current_club_domestic_competition_id"))
        if not league:
            continue
        try:
            if int(float(row.get("last_season") or 0)) < season - 1:
                continue  # retired or left these leagues
        except ValueError:
            pass
        mv = num(row.get("market_value_in_eur"))
        out.setdefault(league, []).append(dict(
            name=row.get("name", ""), team=row.get("current_club_name", ""),
            position=row.get("sub_position") or row.get("position") or "",
            dob=(row.get("date_of_birth") or "")[:10] or None,
            contract=(row.get("contract_expiration_date") or "")[:10] or None,
            nat=[row["country_of_citizenship"]] if row.get("country_of_citizenship") else [],
            mv=mv, mv_cur="EUR" if mv else None, tm_url=row.get("url") or None))
    log("transfermarkt dataset: " + ", ".join(f"{k} {len(v)}" for k, v in out.items()))
    if not out:
        note("transfermarkt", "dataset had no rows for the five leagues")
    return out


def scrape_transfermarkt(season):
    if BeautifulSoup is None:
        log("transfermarkt: bs4 missing")
        return None
    base = "https://www.transfermarkt.com"
    out = {}
    for league, (_, code) in LEAGUES.items():
        r = get(f"{base}/x/startseite/wettbewerb/{code}/saison_id/{season}")
        if not r:
            note("transfermarkt", f"website {league}: {LAST_ERR[0]}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        clubs = {}
        for a in soup.select("table.items td.hauptlink a[href*='/startseite/verein/']"):
            m = re.search(r"/([^/]+)/startseite/verein/(\d+)", a["href"])
            if m and a.get_text(strip=True):
                clubs[m.group(2)] = (m.group(1), a.get_text(strip=True))
        rows = []
        for cid, (slug, cname) in clubs.items():
            time.sleep(REQUEST_PAUSE * 2)
            rr = get(f"{base}/{slug}/kader/verein/{cid}/saison_id/{season}/plus/1")
            if not rr:
                continue
            s = BeautifulSoup(rr.text, "html.parser")
            for tr in s.select("table.items > tbody > tr"):
                a = tr.select_one("td.hauptlink a")
                if not a:
                    continue
                inline = tr.select("table.inline-table tr")
                position = inline[1].get_text(strip=True) if len(inline) > 1 else ""
                cells = [td.get_text(" ", strip=True) for td in tr.select("td.zentriert")]
                dob = None
                for c in cells:
                    m = re.match(r"(.+?)\s*\((\d+)\)", c)
                    if m:
                        dob = parse_date(m.group(1))
                        break
                dates = [parse_date(c) for c in cells]
                dates = [d for d in dates if d]
                contract = dates[-1] if dates and dates[-1] > dt.date.today().isoformat() else None
                nat = [img.get("title") for img in tr.select("td.zentriert img.flaggenrahmen")]
                mv_td = tr.select_one("td.rechts.hauptlink")
                mv, cur = parse_money(mv_td.get_text(strip=True)) if mv_td else (None, None)
                rows.append(dict(name=a.get_text(strip=True), team=cname, position=position,
                                 dob=dob, contract=contract, nat=nat[:2], mv=mv, mv_cur=cur,
                                 tm_url=base + a["href"]))
        out[league] = rows
        log(f"transfermarkt: {league} {len(clubs)} clubs, {len(rows)} players")
    return out


def fetch_gbp_rate():
    r = get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml")
    if not r:
        note("fx", LAST_ERR[0])
        return None
    m = re.search(r"currency='GBP' rate='([\d.]+)'", r.text)
    d = re.search(r"time='([\d-]+)'", r.text)
    return {"eur_gbp": float(m.group(1)), "date": d.group(1) if d else None} if m else None


# --------------------------------------------------------------------------- merge

def age_on(dob, today):
    if not dob:
        return None
    b = dt.date.fromisoformat(dob)
    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def us_pos(p):
    s = (p or "").split()
    for code, broad in (("GK", "GK"), ("D", "DEF"), ("M", "MID"), ("F", "FWD")):
        if code in s:
            return broad
    return None


def tm_broad(pos):
    p = (pos or "").lower()
    if "goalkeeper" in p:
        return "GK"
    if "back" in p or "defender" in p or "sweeper" in p:
        return "DEF"
    if "midfield" in p:
        return "MID"
    if "forward" in p or "winger" in p or "striker" in p:
        return "FWD"
    return None


def num(x, f=float):
    try:
        return f(x)
    except (TypeError, ValueError):
        return None


def build(prev, us, shots_cache, fpl, tm, fx, season, today):
    players = {}

    # 1. Understat base for all five leagues
    for league, rows in (us or {}).items():
        for p in rows:
            pid = f"us{p['id']}"
            goals, npg = num(p.get("goals"), int) or 0, num(p.get("npg"), int) or 0
            rec = dict(
                id=pid, name=p["player_name"], team=p["team_title"].split(",")[-1], league=league,
                pos=us_pos(p.get("position")), apps=num(p.get("games"), int), min=num(p.get("time"), int),
                goals=goals, xg=round(num(p.get("xG")) or 0, 2), npxg=round(num(p.get("npxG")) or 0, 2),
                ast=num(p.get("assists"), int), xa=round(num(p.get("xA")) or 0, 2),
                shots=num(p.get("shots"), int), kp=num(p.get("key_passes"), int),
                penScored=goals - npg, src={"core": "understat"})
            sc = shots_cache.get(str(p["id"]))
            if sc and sc.get("season") == season:
                rec.update(shotsIn=sc["shots_in"], shotsOut=sc["shots_out"], headG=sc["head_goals"],
                           spG=sc["sp_goals"], spXG=sc["sp_xg"], penTaken=sc["pen_taken"])
                rec["penScored"] = sc["pen_scored"]
                rec["src"]["shots"] = "understat"
            players[pid] = rec

    # 2. FPL: enrich Premier League players, or act as the PL base if Understat failed
    if fpl:
        pl = {norm(p["name"]): p for p in players.values() if p["league"] == "Premier League"}
        pl_by_team = {}
        for p in pl.values():
            pl_by_team.setdefault(team_key(p["team"]), {})[norm(p["name"])] = p
        for f in fpl:
            if f.get("removed") in ("True", "true"):
                continue
            full = f"{f['first_name']} {f['second_name']}"
            tk = team_key(f["team_name"])
            team_pool = pl_by_team.get(tk) or next(
                (v for k, v in pl_by_team.items() if difflib.SequenceMatcher(None, k, tk).ratio() > 0.8), None)
            hit = None
            if team_pool:
                hit = best_match(full, team_pool) or best_match(f["web_name"], team_pool, 0.9)
            mins = num(f.get("minutes"), int) or 0
            if not hit:
                if mins == 0 and not f.get("birth_date"):
                    continue
                pid = f"fpl{f['id']}"
                hit = players[pid] = dict(
                    id=pid, name=full, team=f["team_name"], league="Premier League",
                    pos=FPL_POS.get(f["element_type"]), min=mins,
                    goals=num(f.get("goals_scored"), int), xg=num(f.get("expected_goals")),
                    ast=num(f.get("assists"), int), xa=num(f.get("expected_assists")),
                    src={"core": "fpl"})
            hit["pos"] = hit.get("pos") or FPL_POS.get(f["element_type"])
            hit["dob"] = f.get("birth_date") or None
            hit["starts"] = num(f.get("starts"), int)
            hit["xgOpta"] = num(f.get("expected_goals"))
            hit["penMissed"] = num(f.get("penalties_missed"), int)
            hit["tackles"] = num(f.get("tackles"), int)
            hit["recov"] = num(f.get("recoveries"), int)
            duties = []
            if f.get("penalties_order") == "1": duties.append("Penalties")
            if f.get("direct_freekicks_order") == "1": duties.append("Direct free kicks")
            if f.get("corners_and_indirect_freekicks_order") == "1": duties.append("Corners")
            hit["duties"] = duties
            if f.get("news"):
                hit["news"] = f["news"]
            hit["src"]["fpl"] = "fpl"

    # 3. Transfermarkt: value, contract, DOB, detailed position
    if tm:
        for league, rows in tm.items():
            by_team = {}
            for r in rows:
                by_team.setdefault(team_key(r["team"]), {})[norm(r["name"])] = r
            for p in players.values():
                if p["league"] != league:
                    continue
                tk = team_key(p["team"])
                pool = by_team.get(tk)
                if pool is None:
                    m = difflib.get_close_matches(tk, list(by_team), n=1, cutoff=0.6)
                    pool = by_team[m[0]] if m else None
                r = best_match(p["name"], pool) if pool else None
                if not r:
                    continue
                if r["mv"]:
                    eur = r["mv"] if r["mv_cur"] == "EUR" else (
                        r["mv"] / fx["eur_gbp"] if (fx and r["mv_cur"] == "GBP") else None)
                    if eur:
                        p["mv"] = round(eur)
                        p["wageLo"] = round(eur * WAGE_SHARE_LOW / 52)
                        p["wageHi"] = round(eur * WAGE_SHARE_HIGH / 52)
                        p["src"]["wage"] = "model"
                p["contract"] = r["contract"]
                p["dob"] = p.get("dob") or r["dob"]
                p["posDetail"] = r["position"] or None
                p["pos"] = p.get("pos") or tm_broad(r["position"])
                p["nat"] = r["nat"]
                p["tm"] = r["tm_url"]
                p["src"]["value"] = "transfermarkt"

    # 4. Derived fields
    for p in players.values():
        p["age"] = age_on(p.get("dob"), today)
        mins, goals, shots = p.get("min") or 0, p.get("goals") or 0, p.get("shots")
        p["g90"] = round(goals * 90 / mins, 2) if mins >= 90 else None
        p["minPerGoal"] = round(mins / goals) if goals else None
        p["conv"] = round(100 * goals / shots, 1) if shots else None

    # Drop empty keys to keep the file small
    clean = [{k: v for k, v in p.items() if v not in (None, [], "")} for p in players.values()]
    clean.sort(key=lambda p: (-(p.get("min") or 0)))
    return clean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=["understat", "fpl", "transfermarkt", "fx"])
    ap.add_argument("--fpl-csv")
    ap.add_argument("--fpl-teams")
    ap.add_argument("--fpl-asof", help="ISO timestamp of the FPL snapshot when using --fpl-csv")
    args = ap.parse_args()
    want = set(args.only or ["understat", "fpl", "transfermarkt", "fx"])

    today = dt.date.today()
    season = season_start_year(today)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    CACHE.mkdir(exist_ok=True)

    prev = json.loads(OUT.read_text()) if OUT.exists() else {"sources": {}}
    sources = prev.get("sources", {})

    def cached(name):
        f = CACHE / f"{name}.json"
        return json.loads(f.read_text()) if f.exists() else None

    def store(name, data):
        (CACHE / f"{name}.json").write_text(json.dumps(data))

    def run(name, fn, ok=lambda d: bool(d), asof=None):
        if name in want:
            try:
                data = fn()
            except Exception as e:  # a broken source must never stop the others
                note(name, f"{type(e).__name__}: {e}"[:200])
                data = None
            via = {"fpl": FPL_VIA[0], "transfermarkt": TM_VIA[0], "understat": US_VIA[0]}.get(name)
            if ok(data):
                store(name, data)
                sources[name] = {"status": "partial" if ERRORS.get(name) else "ok", "updated": asof or now}
                if via:
                    sources[name]["via"] = via
                if ERRORS.get(name):
                    sources[name]["error"] = "; ".join(ERRORS[name])
                return data
            note(name, "no usable data this run, keeping the previous copy")
            src = sources.setdefault(name, {})
            src.update(status="stale", error="; ".join(ERRORS[name]), checked=now)
        return cached(name)

    us = run("understat", lambda: fetch_understat(season))
    shots_cache = cached("understat_shots") or {}
    if "understat" in want and us:
        shots_cache = fetch_understat_shots(us, season, shots_cache)
        store("understat_shots", shots_cache)
    fpl = run("fpl", lambda: fetch_fpl(args.fpl_csv, args.fpl_teams), asof=args.fpl_asof)
    fx = run("fx", fetch_gbp_rate)
    tm = run("transfermarkt", lambda: fetch_transfermarkt(season),
             ok=lambda d: bool(d) and sum(len(v) for v in d.values()) > 200)

    players = build(prev, us, shots_cache, fpl, tm, fx, season, today)
    log("\n=== SOURCE SUMMARY ===")
    for k, v in sources.items():
        log(f"{k:14} {v.get('status'):8} {v.get('via', '')} {v.get('error', '')}")
    out = {
        "generated": now,
        "season": f"{season}/{str(season + 1)[-2:]}",
        "fx": fx,
        "wageModel": {"low": WAGE_SHARE_LOW, "high": WAGE_SHARE_HIGH},
        "sources": sources,
        "players": players,
    }
    blob = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    OUT.write_text(blob)
    log(f"wrote {len(players)} players -> {OUT}")
    embed_seed(blob)


def embed_seed(blob):
    """Copy the data into index.html so the page also works as a single offline file."""
    page = ROOT / "index.html"
    if not page.exists():
        return
    html = page.read_text(encoding="utf-8")
    safe = blob.replace("</", "<\\/")
    new, n = re.subn(r'(<script id="seed" type="application/json">).*?(</script>)',
                     lambda m: m.group(1) + safe + m.group(2), html, count=1, flags=re.S)
    if n:
        page.write_text(new, encoding="utf-8")
        log("embedded data in index.html")


if __name__ == "__main__":
    main()
