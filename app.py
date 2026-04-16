"""
CricLive — Cricket Live Score App
Data: api.cricketdata.org

Render.com:
  Build command:  pip install -r requirements.txt
  Start command:  gunicorn app:app
  Env var:        CRICKET_API_KEY = c2e7a722-f15f-4b4c-a3be-4a94b3aaedc4
"""

import os, time, json
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template, Response
import requests

app = Flask(__name__)

API_KEY  = os.environ.get("CRICKET_API_KEY", "")
BASE_URL = "https://api.cricketdata.org"

# Cache TTLs in seconds
TTL_LIVE      = 15
TTL_UPCOMING  = 120
TTL_RECENT    = 120
TTL_SCORECARD = 10

_cache: dict = {}

def _fetch(path: str, ttl: int) -> dict:
    now = time.time()
    if path in _cache:
        data, ts = _cache[path]
        if now - ts < ttl:
            return data
    r = requests.get(f"{BASE_URL}{path}", timeout=12,
                     headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    status = (data.get("status") or "").lower()
    if status not in ("success", "ok", ""):
        raise RuntimeError(f"{status}:{data.get('reason','')}")
    _cache[path] = (data, now)
    return data

# ── Filter: IPL · India · ICC events only ────────────────────────────────────
_KW = [
    "ipl", "indian premier league", "tata ipl",
    "t20 world cup", "icc t20 world cup",
    "icc cricket world cup", "icc odi world cup", "cricket world cup",
    "icc champions trophy", "champions trophy",
    "icc world test championship", "world test championship",
    "icc women",
]

def _allowed(m: dict) -> bool:
    name  = (m.get("name") or "").lower()
    teams = [t.lower() for t in (m.get("teams") or [])]
    ti    = [t.get("name", "").lower() for t in (m.get("teamInfo") or [])]
    for kw in _KW:
        if kw in name:
            return True
    return any("india" in t for t in teams + ti)

# ── Formatters ───────────────────────────────────────────────────────────────
def _short(m: dict, idx: int) -> str:
    ti = m.get("teamInfo") or []
    if idx < len(ti):
        return ti[idx].get("shortname") or (ti[idx].get("name", "")[:3].upper())
    teams = m.get("teams") or []
    return teams[idx][:3].upper() if idx < len(teams) else "???"

def _score_str(score_arr: list, idx: int) -> str:
    if not score_arr or idx >= len(score_arr):
        return "Yet to bat"
    s = score_arr[idx]
    return f"{s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)} ov)"

def _fmt_dt(dt: str) -> str:
    """GMT string → DD Mon YYYY, HH:MM AM/PM IST"""
    if not dt:
        return ""
    try:
        d = datetime.strptime(dt.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
        d = d.replace(tzinfo=timezone.utc).astimezone(
                timezone(timedelta(hours=5, minutes=30)))
        return d.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return dt

def _parse(m: dict) -> dict:
    teams = m.get("teams") or ["?", "?"]
    score = m.get("score") or []
    return {
        "matchId":            m.get("id", ""),
        "name":               m.get("name", ""),
        "matchType":          (m.get("matchType") or "").upper(),
        "status":             m.get("status", ""),
        "venue":              m.get("venue", ""),
        "startDateFormatted": _fmt_dt(m.get("dateTimeGMT", "")),
        "team1": {"name": teams[0] if teams else "?",
                  "short": _short(m, 0)},
        "team2": {"name": teams[1] if len(teams) > 1 else "?",
                  "short": _short(m, 1)},
        "team1Score": _score_str(score, 0),
        "team2Score": _score_str(score, 1),
    }

def _get_matches(path: str, ttl: int) -> tuple:
    try:
        data = _fetch(path, ttl)
        raw  = data.get("data") or []
        if not isinstance(raw, list):
            return [], "UNEXPECTED_RESPONSE"
        return [_parse(m) for m in raw if _allowed(m)], None
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg:
            return [], "INVALID_KEY"
        if "429" in msg:
            return [], "RATE_LIMITED"
        return [], msg

# ── Scorecard parser ─────────────────────────────────────────────────────────
def _scorecard(match_id: str) -> dict:
    """
    CricketData.org /v1/matchScorecard response:

    data.scorecard[]:
      batcard[]:  batsman.name, r, b, 4s, 6s, sr
                  dismissal-wicket: null  →  batter is still at crease
      bowlcard[]: bowler.name, o, m, r, w, wd, nb, eco

    Active batsmen = those where dismissal-wicket is None.
    Current bowler = last entry in bowlcard (most recently bowling).
    """
    try:
        data  = _fetch(f"/v1/matchScorecard?id={match_id}&apikey={API_KEY}",
                       TTL_SCORECARD)
        d     = data.get("data") or {}
        cards = d.get("scorecard") or []
        if not cards:
            return {}

        # Current (live) innings = last card
        inn      = cards[-1]
        batcard  = inn.get("batcard") or []
        bowlcard = inn.get("bowlcard") or []

        # Active batsmen: dismissal-wicket == null
        active = [b for b in batcard if b.get("dismissal-wicket") is None]
        if not active and len(batcard) >= 2:
            active = batcard[-2:]   # final fallback

        def _bat(b):
            if not b:
                return None
            p = b.get("batsman") or {}
            return {
                "name":  p.get("name", ""),
                "runs":  b.get("r", 0),
                "balls": b.get("b", 0),
                "fours": b.get("4s", 0),
                "sixes": b.get("6s", 0),
                "sr":    float(b.get("sr", 0) or 0),
            }

        striker     = _bat(active[0]) if len(active) > 0 else None
        non_striker = _bat(active[1]) if len(active) > 1 else None

        # Current bowler: last in bowlcard
        bowler = None
        if bowlcard:
            lb = bowlcard[-1]
            bw = lb.get("bowler") or {}
            bowler = {
                "name":    bw.get("name", ""),
                "overs":   str(lb.get("o", "")),
                "runs":    lb.get("r", 0),
                "wickets": lb.get("w", 0),
                "maidens": int(lb.get("m", 0) or 0),
                "economy": float(lb.get("eco", 0) or 0),
            }

        # Current run rate from live score
        score = d.get("score") or []
        cur   = score[-1] if score else {}
        runs, ov = cur.get("r", 0), float(cur.get("o", 0) or 0)
        crr = round(runs / ov, 2) if ov > 0 else None

        # Required run rate (2nd innings only)
        rrr = None
        mtype = (d.get("matchType") or "").lower()
        total = 20 if "t20" in mtype else (50 if "odi" in mtype else None)
        if total and len(score) >= 2 and ov > 0:
            target     = score[0].get("r", 0) + 1
            runs_needed = target - runs
            rem_ov     = round(total - ov, 1)
            if rem_ov > 0 and runs_needed > 0:
                rrr = round(runs_needed / rem_ov, 2)

        return {
            "striker":         striker,
            "nonStriker":      non_striker,
            "bowler":          bowler,
            "partnership":     None,   # available via /v1/bbb if needed
            "runRate":         crr,
            "requiredRunRate": rrr,
        }
    except Exception as e:
        return {"error": str(e)}

# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/live")
def r_live():
    m, err = _get_matches(f"/v1/currentMatches?apikey={API_KEY}", TTL_LIVE)
    return jsonify({"matches": m, "error": err})

@app.route("/api/upcoming")
def r_upcoming():
    m, err = _get_matches(f"/v1/matches?apikey={API_KEY}&type=upcoming", TTL_UPCOMING)
    return jsonify({"matches": m, "error": err})

@app.route("/api/recent")
def r_recent():
    m, err = _get_matches(f"/v1/matches?apikey={API_KEY}&type=recent", TTL_RECENT)
    return jsonify({"matches": m, "error": err})

@app.route("/api/scorecard/<path:match_id>")
def r_scorecard(match_id: str):
    return jsonify(_scorecard(match_id))

@app.route("/live_feed.json")
def r_feed():
    """Godot-compatible JSON endpoint."""
    matches, err = _get_matches(f"/v1/currentMatches?apikey={API_KEY}", TTL_LIVE)
    feed = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "error":     err,
        "live_matches": [
            {
                "match_id":    m["matchId"],
                "title":       f"{m['team1']['short']} vs {m['team2']['short']}",
                "name":        m["name"],
                "format":      m["matchType"],
                "status":      m["status"],
                "team1":       m["team1"]["short"],
                "team1_score": m["team1Score"],
                "team2":       m["team2"]["short"],
                "team2_score": m["team2Score"],
                "venue":       m["venue"],
            }
            for m in matches[:5]
        ],
    }
    return Response(json.dumps(feed, indent=2), mimetype="application/json",
                    headers={"Cache-Control": "no-cache, no-store"})

@app.route("/health")
def r_health():
    return jsonify({"ok": True, "key_set": bool(API_KEY)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
