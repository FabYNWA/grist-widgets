#!/usr/bin/env python3
"""MPP Pronostics - synchronisation quotidienne vers Grist.

Modes :
  all      cotes (The Odds API) + resultats (football-data.org + scores Odds API) + logos
  results  resultats uniquement (n'utilise des credits Odds API que si necessaire)

Variables d'environnement : ODDS_API_KEY, FD_TOKEN, GRIST_API_KEY
Optionnelles : GRIST_DOC, GRIST_BASE
"""
import difflib
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GRIST_BASE = os.environ.get("GRIST_BASE", "https://docs.getgrist.com").rstrip("/")
GRIST_DOC = os.environ.get("GRIST_DOC", "qmJfcfW8XCRi1m4GJWYPRE")
GRIST_KEY = os.environ.get("GRIST_API_KEY", "")
ODDS_KEY = os.environ.get("ODDS_API_KEY", "")
FD_TOKEN = os.environ.get("FD_TOKEN", "")
BOOKMAKERS = ["betclic_fr", "unibet_fr"]  # ordre de priorite
MODE = sys.argv[1] if len(sys.argv) > 1 else "all"

NOW = datetime.now(timezone.utc)
log_lines = []


def log(msg):
    print(msg, flush=True)
    log_lines.append(msg)


def http(url, headers=None, method="GET", body=None, timeout=40, retries=2):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}, dict(r.headers)
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:300]
            last = "HTTP %s %s" % (e.code, txt)
            if e.code in (401, 403, 404, 422):
                break
            if e.code == 429:
                time.sleep(15)
        except Exception as e:  # reseau
            last = str(e)
        time.sleep(2 + attempt * 3)
    raise RuntimeError(last)


# ---------------------------------------------------------------- Grist
def g(path, method="GET", body=None):
    url = "%s/api/docs/%s%s" % (GRIST_BASE, GRIST_DOC, path)
    out, _ = http(url, {"Authorization": "Bearer " + GRIST_KEY}, method, body)
    return out


def g_records(table):
    return g("/tables/%s/records" % table).get("records", [])


def g_add(table, rows):
    ids = []
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        out = g("/tables/%s/records" % table, "POST", {"records": [{"fields": f} for f in chunk]})
        ids += [r["id"] for r in out.get("records", [])]
    return ids


def g_update(table, updates):
    for i in range(0, len(updates), 200):
        chunk = updates[i:i + 200]
        g("/tables/%s/records" % table, "PATCH", {"records": [{"id": rid, "fields": f} for rid, f in chunk]})


# ---------------------------------------------------------------- noms
DROP = {"fc", "afc", "cf", "sc", "ac", "as", "us", "ss", "ssc", "rc", "rcd", "cd", "ud", "sv", "fsv", "bv",
        "vfb", "tsg", "fk", "sk", "club", "de", "del", "la", "le", "the", "1"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    s = "".join(c if c.isalnum() else " " for c in s)
    toks = [t for t in s.split() if t not in DROP]
    return " ".join(toks)


class Teams:
    def __init__(self):
        self.by_norm = {}
        self.names = {}
        self.logo = {}
        for r in g_records("Equipes"):
            f = r["fields"]
            self.names[r["id"]] = f.get("Nom", "")
            self.logo[r["id"]] = f.get("Logo") or ""
            self.by_norm[norm(f.get("Nom", ""))] = r["id"]
        self.alias = {}
        self.alias_rows = set()
        for r in g_records("Alias"):
            f = r["fields"]
            key = (f.get("Source"), norm(f.get("Alias", "")))
            self.alias_rows.add(key)
            if f.get("Equipe"):
                self.alias[key] = f["Equipe"]
        self.orphans = []

    def resolve(self, name, source, create=False):
        n = norm(name)
        if not n:
            return None
        if (source, n) in self.alias:
            return self.alias[(source, n)]
        if n in self.by_norm:
            return self.by_norm[n]
        # approchant
        cands = difflib.get_close_matches(n, list(self.by_norm.keys()), n=2, cutoff=0.86)
        if cands and (len(cands) == 1 or difflib.SequenceMatcher(None, n, cands[0]).ratio() -
                      difflib.SequenceMatcher(None, n, cands[1]).ratio() > 0.04):
            tid = self.by_norm[cands[0]]
            self._learn(name, source, tid)
            return tid
        if create:
            tid = g_add("Equipes", [{"Nom": name}])[0]
            self.names[tid] = name
            self.logo[tid] = ""
            self.by_norm[n] = tid
            log("  + nouvelle equipe : %s" % name)
            return tid
        # orpheline : on la note pour correction manuelle
        if (source, n) not in self.alias_rows:
            g_add("Alias", [{"Alias": name, "Source": source}])
            self.alias_rows.add((source, n))
        self.orphans.append(name)
        return None

    def _learn(self, name, source, tid):
        key = (source, norm(name))
        if key not in self.alias_rows:
            g_add("Alias", [{"Alias": name, "Source": source, "Equipe": tid}])
            self.alias_rows.add(key)
        self.alias[key] = tid
        log("  ~ alias appris : %s -> %s" % (name, self.names.get(tid)))


# ---------------------------------------------------------------- cotes
def ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def pick_odds(event):
    by_key = {b["key"]: b for b in event.get("bookmakers", [])}
    for key in BOOKMAKERS:
        b = by_key.get(key)
        if not b:
            continue
        for m in b.get("markets", []):
            if m.get("key") != "h2h":
                continue
            o = {x["name"]: x["price"] for x in m.get("outcomes", [])}
            h, a = event["home_team"], event["away_team"]
            if h in o and a in o and "Draw" in o:
                return key, o[h], o["Draw"], o[a]
    return None


def sync_odds(comps, teams, matchs):
    by_id = {m["fields"].get("IdOdds"): m for m in matchs if m["fields"].get("IdOdds")}
    created = updated = 0
    for comp in comps:
        key = comp["fields"].get("CleOdds")
        if not key:
            continue
        url = ("https://api.the-odds-api.com/v4/sports/%s/odds?regions=fr&markets=h2h&oddsFormat=decimal"
               "&bookmakers=%s&apiKey=%s" % (key, ",".join(BOOKMAKERS), ODDS_KEY))
        try:
            events, hdr = http(url)
        except Exception as e:
            log("! cotes %s : %s" % (key, str(e).replace(ODDS_KEY, "***")))
            continue
        log("cotes %s : %d matchs (credits restants : %s)" % (key, len(events), hdr.get("x-requests-remaining", "?")))
        new_rows, upd = [], []
        for ev in events:
            start = ts(ev["commence_time"])
            odds = pick_odds(ev)
            ex = by_id.get(ev["id"])
            if ex:
                f = ex["fields"]
                if f.get("Statut") == "Termine" or start <= NOW.timestamp():
                    continue  # on ne touche plus aux cotes d'un match commence
                if odds:
                    bk, c1, cn, c2 = odds
                    new = {"Bookmaker": bk, "Cote1": c1, "CoteN": cn, "Cote2": c2, "CoteMaj": int(NOW.timestamp())}
                    if int(f.get("DateHeure") or 0) != start:
                        new["DateHeure"] = start
                    upd.append((ex["id"], new))
                continue
            dom = teams.resolve(ev["home_team"], "The Odds API", create=True)
            ext = teams.resolve(ev["away_team"], "The Odds API", create=True)
            row = {"IdOdds": ev["id"], "Competition": comp["id"], "DateHeure": start, "Domicile": dom,
                   "Exterieur": ext, "NomDomicileBrut": ev["home_team"], "NomExterieurBrut": ev["away_team"],
                   "Statut": "A venir"}
            if odds:
                bk, c1, cn, c2 = odds
                row.update({"Bookmaker": bk, "Cote1": c1, "CoteN": cn, "Cote2": c2, "CoteMaj": int(NOW.timestamp())})
            new_rows.append(row)
        if new_rows:
            ids = g_add("Matchs", new_rows)
            created += len(ids)
            for rid, row in zip(ids, new_rows):
                by_id[row["IdOdds"]] = {"id": rid, "fields": dict(row)}
        if upd:
            g_update("Matchs", upd)
            updated += len(upd)
    log("Cotes : %d matchs crees, %d mis a jour" % (created, updated))


# ---------------------------------------------------------------- resultats
def result_fields(sd, se):
    return {"Statut": "Termine", "ScoreDom": int(sd), "ScoreExt": int(se)}


def sync_results_fd(comps, teams, matchs):
    if not FD_TOKEN:
        log("! FD_TOKEN absent : resultats football-data ignores")
        return
    pending = [m for m in matchs if m["fields"].get("Statut") != "Termine"
               and (m["fields"].get("DateHeure") or 0) < NOW.timestamp() - 2 * 3600]
    if not pending:
        log("Resultats : aucun match en attente")
        return
    idx = {}
    for m in pending:
        f = m["fields"]
        idx.setdefault((f.get("Domicile"), f.get("Exterieur")), []).append(m)
    d_from = (NOW - timedelta(days=5)).strftime("%Y-%m-%d")
    d_to = (NOW + timedelta(days=1)).strftime("%Y-%m-%d")
    done = 0
    for comp in comps:
        code = comp["fields"].get("CodeFD")
        if not code:
            continue
        url = ("https://api.football-data.org/v4/competitions/%s/matches?status=FINISHED&dateFrom=%s&dateTo=%s"
               % (code, d_from, d_to))
        try:
            data, _ = http(url, {"X-Auth-Token": FD_TOKEN})
        except Exception as e:
            log("! resultats %s : %s" % (code, e))
            time.sleep(7)
            continue
        ups = []
        for fm in data.get("matches", []):
            dom = teams.resolve(fm["homeTeam"].get("name") or fm["homeTeam"].get("shortName"), "football-data.org")
            ext = teams.resolve(fm["awayTeam"].get("name") or fm["awayTeam"].get("shortName"), "football-data.org")
            cand = idx.get((dom, ext)) if dom and ext else None
            if not cand:
                continue
            start = ts(fm["utcDate"])
            cand = [m for m in cand if abs((m["fields"].get("DateHeure") or 0) - start) < 36 * 3600]
            if not cand:
                continue
            sc = fm.get("score", {})
            # 1N2 = score apres 90 minutes (regularTime si prolongation, sinon fullTime)
            part = sc.get("regularTime") or sc.get("fullTime") or {}
            if part.get("home") is None or part.get("away") is None:
                continue
            ups.append((cand[0]["id"], result_fields(part["home"], part["away"])))
            idx[(dom, ext)].remove(cand[0])
        if ups:
            g_update("Matchs", ups)
            done += len(ups)
        log("resultats %s : %d matchs termines" % (code, len(ups)))
        time.sleep(7)  # limite 10 requetes/minute
    log("Resultats football-data : %d mis a jour" % done)


def sync_results_odds(comps, matchs):
    """Europa League / Conference League : scores via The Odds API (2 credits par appel)."""
    done = 0
    for comp in comps:
        if comp["fields"].get("SourceResultats") != "The Odds API":
            continue
        pend = [m for m in matchs if m["fields"].get("Competition") == comp["id"]
                and m["fields"].get("Statut") != "Termine"
                and (m["fields"].get("DateHeure") or 0) < NOW.timestamp() - 2.5 * 3600]
        if not pend:
            continue
        key = comp["fields"]["CleOdds"]
        url = "https://api.the-odds-api.com/v4/sports/%s/scores/?daysFrom=3&apiKey=%s" % (key, ODDS_KEY)
        try:
            events, hdr = http(url)
        except Exception as e:
            log("! scores %s : %s" % (key, str(e).replace(ODDS_KEY, "***")))
            continue
        byid = {m["fields"].get("IdOdds"): m for m in pend}
        ups = []
        for ev in events:
            m = byid.get(ev.get("id"))
            if not m or not ev.get("completed") or not ev.get("scores"):
                continue
            sc = {s["name"]: s["score"] for s in ev["scores"]}
            try:
                sd, se = sc[ev["home_team"]], sc[ev["away_team"]]
            except KeyError:
                continue
            ups.append((m["id"], result_fields(sd, se)))
        if ups:
            g_update("Matchs", ups)
            done += len(ups)
        log("scores %s : %d termines (credits restants : %s)" % (key, len(ups), hdr.get("x-requests-remaining", "?")))
    log("Resultats Odds API : %d mis a jour" % done)


# ---------------------------------------------------------------- logos
def sync_logos(comps, teams):
    missing = [i for i, l in teams.logo.items() if not l]
    if not missing:
        return
    log("Logos : %d equipes sans logo" % len(missing))
    if FD_TOKEN:
        for comp in comps:
            code = comp["fields"].get("CodeFD")
            if not code:
                continue
            try:
                data, _ = http("https://api.football-data.org/v4/competitions/%s/teams" % code,
                               {"X-Auth-Token": FD_TOKEN})
            except Exception as e:
                log("! logos %s : %s" % (code, e))
                time.sleep(7)
                continue
            ups = []
            for t in data.get("teams", []):
                tid = teams.resolve(t.get("name"), "football-data.org")
                if tid and not teams.logo.get(tid) and t.get("crest"):
                    teams.logo[tid] = t["crest"]
                    ups.append((tid, {"Logo": t["crest"]}))
            if ups:
                g_update("Equipes", ups)
            time.sleep(7)
    # reste : TheSportsDB (equipes europeennes hors 5 grands championnats)
    left = [i for i, l in teams.logo.items() if not l][:25]
    for tid in left:
        try:
            data, _ = http("https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t=" +
                           urllib.parse.quote(teams.names[tid]), timeout=20, retries=0)
            lst = [x for x in (data.get("teams") or []) if x.get("strSport") == "Soccer" and x.get("strBadge")]
            exact = [x for x in lst if norm(x.get("strTeam")) == norm(teams.names[tid])]
            pick = (exact or lst or [None])[0]
            if pick:
                url = pick["strBadge"] + "/small"
                teams.logo[tid] = url
                g_update("Equipes", [(tid, {"Logo": url})])
        except Exception:
            pass
        time.sleep(2.2)


# ---------------------------------------------------------------- main
def main():
    if not GRIST_KEY:
        sys.exit("GRIST_API_KEY manquant")
    log("MPP sync - mode %s - %s UTC" % (MODE, NOW.strftime("%Y-%m-%d %H:%M")))
    comps = g_records("Competitions")
    teams = Teams()
    if MODE == "all":
        if ODDS_KEY:
            matchs = g_records("Matchs")
            sync_odds(comps, teams, matchs)
        else:
            log("! ODDS_API_KEY absent : cotes ignorees")
    matchs = g_records("Matchs")
    sync_results_fd(comps, teams, matchs)
    matchs = g_records("Matchs")
    if ODDS_KEY:
        sync_results_odds(comps, matchs)
    if MODE == "all":
        sync_logos(comps, teams)
    if teams.orphans:
        log("A CORRIGER (table Alias, colonne Equipe vide) : " + ", ".join(sorted(set(teams.orphans))))
    log("Termine.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = str(e)
        for k in (GRIST_KEY, ODDS_KEY, FD_TOKEN):
            if k:
                msg = msg.replace(k, "***")
        print("ERREUR : " + msg)
        sys.exit(1)
