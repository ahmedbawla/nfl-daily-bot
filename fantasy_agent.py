"""
Fantasy Football Agent — Fully autonomous Sleeper roster manager.
Consults BILL (nfl_tools) for analysis. Reports via Telegram.

Runs:
- Every day 7 AM ET  : injury check + lineup optimization
- Tuesday 9 AM ET    : weekly recap + waiver wire analysis + claims
- Wednesday 9 AM ET  : confirm lineup after waivers process
- Sunday 11:30 AM ET : final lineup lock check
- Sun 9 PM / Mon 11:30 PM ET : notable performance alerts
- Every 4 hours      : check incoming trade offers
"""

import os
import json
import time
import requests
from datetime import datetime
from anthropic import Anthropic
import pytz
import nfl_tools

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
SLEEPER_USERNAME  = os.environ["SLEEPER_USERNAME"]
SLEEPER_PASSWORD  = os.environ["SLEEPER_PASSWORD"]
SLEEPER_LEAGUE_ID = os.environ["SLEEPER_LEAGUE_ID"]

client  = Anthropic()
EASTERN = pytz.timezone("America/New_York")

SLEEPER_BASE = "https://api.sleeper.app/v1"


# ── Telegram ──────────────────────────────────────────────────────────────────
def notify(text: str):
    """Send a message to the user via Telegram."""
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )


# ── Sleeper API ───────────────────────────────────────────────────────────────
_auth_token: str | None = None
_user_id:    str | None = None
_roster_id:  int | None = None
_all_players: dict     = {}


def _headers() -> dict:
    return {"Authorization": f"Bearer {_auth_token}", "Content-Type": "application/json"}


def sleeper_login():
    global _auth_token, _user_id
    resp = requests.post(f"{SLEEPER_BASE}/login", json={
        "login":    SLEEPER_USERNAME,
        "password": SLEEPER_PASSWORD,
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    _auth_token = data.get("token") or data.get("user_id")  # fallback
    _user_id    = str(data.get("user_id", ""))
    print(f"[Sleeper] Logged in as {data.get('display_name')} (user_id={_user_id})")


def get_league() -> dict:
    return requests.get(f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}", timeout=10).json()


def get_rosters() -> list:
    return requests.get(f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}/rosters", timeout=10).json()


def get_my_roster() -> dict:
    global _roster_id
    rosters = get_rosters()
    for r in rosters:
        if str(r.get("owner_id")) == _user_id:
            _roster_id = r["roster_id"]
            return r
    raise RuntimeError("Could not find your roster in this league.")


def get_matchups(week: int) -> list:
    return requests.get(f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}/matchups/{week}", timeout=10).json()


def get_transactions(week: int) -> list:
    return requests.get(f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}/transactions/{week}", timeout=10).json()


def get_all_players() -> dict:
    """Full player DB — cache after first load (~5 MB)."""
    global _all_players
    if _all_players:
        return _all_players
    print("[Sleeper] Loading full player DB…")
    _all_players = requests.get(f"{SLEEPER_BASE}/players/nfl", timeout=30).json()
    return _all_players


def get_player_projections(week: int, season: int = None) -> dict:
    s = season or datetime.now(EASTERN).year
    resp = requests.get(
        f"{SLEEPER_BASE}/projections/nfl/regular/{s}/{week}",
        params={"position[]": ["QB", "RB", "WR", "TE", "K", "DEF"]},
        timeout=10,
    )
    return resp.json() if resp.ok else {}


def get_player_stats(week: int, season: int = None) -> dict:
    s = season or datetime.now(EASTERN).year
    resp = requests.get(
        f"{SLEEPER_BASE}/stats/nfl/regular/{s}/{week}",
        params={"position[]": ["QB", "RB", "WR", "TE", "K", "DEF"]},
        timeout=10,
    )
    return resp.json() if resp.ok else {}


def get_trending_adds(hours: int = 24, limit: int = 25) -> list:
    resp = requests.get(
        f"{SLEEPER_BASE}/players/nfl/trending/add",
        params={"lookback_hours": hours, "limit": limit},
        timeout=10,
    )
    return resp.json() if resp.ok else []


def get_current_week() -> int:
    league = get_league()
    return int(league.get("settings", {}).get("leg", 1))


# ── Sleeper Writes (unofficial authenticated endpoints) ───────────────────────
def set_starters(starter_ids: list[str]) -> bool:
    """Set the starting lineup."""
    if not _roster_id:
        get_my_roster()
    resp = requests.put(
        f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}/rosters/{_roster_id}/starters",
        headers=_headers(),
        json={"starters": starter_ids},
        timeout=10,
    )
    if resp.ok:
        print(f"[Sleeper] Lineup set: {starter_ids}")
    else:
        print(f"[Sleeper] Set lineup failed: {resp.status_code} {resp.text}")
    return resp.ok


def submit_waiver_claim(add_player_id: str, drop_player_id: str, bid: int = 0) -> bool:
    """Submit a waiver claim or free agent add."""
    if not _roster_id:
        get_my_roster()
    league   = get_league()
    waivers  = league.get("settings", {}).get("waiver_type", 0)
    tx_type  = "waiver" if waivers else "free_agent"

    payload = {
        "type":      tx_type,
        "roster_id": _roster_id,
        "adds":      {add_player_id: _roster_id},
        "drops":     {drop_player_id: _roster_id} if drop_player_id else {},
        "settings":  {"waiver_bid": bid} if tx_type == "waiver" else {},
        "metadata":  {},
    }
    resp = requests.post(
        f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}/transactions",
        headers=_headers(),
        json=payload,
        timeout=10,
    )
    if resp.ok:
        print(f"[Sleeper] Waiver submitted: add {add_player_id} / drop {drop_player_id}")
    else:
        print(f"[Sleeper] Waiver failed: {resp.status_code} {resp.text}")
    return resp.ok


def propose_trade(target_roster_id: int, give_player_ids: list, get_player_ids: list) -> bool:
    """Propose a trade to another roster."""
    if not _roster_id:
        get_my_roster()
    adds  = {pid: _roster_id for pid in get_player_ids}
    adds.update({pid: target_roster_id for pid in give_player_ids})
    drops = {}

    payload = {
        "type":       "trade",
        "roster_ids": [_roster_id, target_roster_id],
        "adds":       adds,
        "drops":      drops,
        "settings":   {},
        "metadata":   {},
    }
    resp = requests.post(
        f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}/transactions",
        headers=_headers(),
        json=payload,
        timeout=10,
    )
    if resp.ok:
        print(f"[Sleeper] Trade proposed to roster {target_roster_id}")
    else:
        print(f"[Sleeper] Trade proposal failed: {resp.status_code} {resp.text}")
    return resp.ok


def respond_to_trade(transaction_id: str, accept: bool) -> bool:
    """Accept or reject an incoming trade offer."""
    action = "accept" if accept else "reject"
    resp = requests.post(
        f"{SLEEPER_BASE}/league/{SLEEPER_LEAGUE_ID}/transactions/{transaction_id}/{action}",
        headers=_headers(),
        timeout=10,
    )
    return resp.ok


# ── Helpers ───────────────────────────────────────────────────────────────────
def player_name(pid: str) -> str:
    players = get_all_players()
    p = players.get(str(pid), {})
    return p.get("full_name") or p.get("first_name", "") + " " + p.get("last_name", "")


def player_info(pid: str) -> dict:
    players = get_all_players()
    p = players.get(str(pid), {})
    return {
        "id":       pid,
        "name":     p.get("full_name", pid),
        "position": p.get("position", ""),
        "team":     p.get("team", "FA"),
        "status":   p.get("injury_status", "Active"),
    }


def my_roster_summary() -> dict:
    """Return a clean summary of my current roster."""
    roster  = get_my_roster()
    players = get_all_players()
    starters = roster.get("starters", [])
    all_ids  = roster.get("players", [])
    bench    = [p for p in all_ids if p not in starters]

    def fmt(pid):
        p = players.get(str(pid), {})
        return {
            "id":       pid,
            "name":     p.get("full_name", pid),
            "position": p.get("position", "?"),
            "team":     p.get("team", "FA"),
            "status":   p.get("injury_status", "Active"),
        }

    return {
        "starters": [fmt(p) for p in starters],
        "bench":    [fmt(p) for p in bench],
        "waiver_budget": roster.get("settings", {}).get("waiver_budget_used", 0),
    }


def available_players(positions: list[str] = None, limit: int = 50) -> list:
    """Get top available free agents by position."""
    rosters     = get_rosters()
    rostered    = {p for r in rosters for p in (r.get("players") or [])}
    all_players = get_all_players()
    trending    = {t["player_id"] for t in get_trending_adds(hours=48)}

    avail = []
    for pid, p in all_players.items():
        if pid in rostered:
            continue
        pos = p.get("position", "")
        if positions and pos not in positions:
            continue
        if pos not in ["QB", "RB", "WR", "TE", "K", "DEF"]:
            continue
        status = p.get("injury_status", "")
        if status in ["IR", "PUP"]:
            continue
        avail.append({
            "id":       pid,
            "name":     p.get("full_name", pid),
            "position": pos,
            "team":     p.get("team", "FA"),
            "trending": pid in trending,
            "status":   status or "Active",
        })

    return avail[:limit]


# ── Claude decision engine ────────────────────────────────────────────────────
FANTASY_AGENT_SYSTEM = """You are an autonomous fantasy football manager. Your job is to make optimal decisions for the team.
You have access to BILL's NFL analysis tools for stats, injury data, and projections.

DECISION RULES:
- Always check injury status before setting a player as starter.
- Prioritize floor over ceiling for must-start positions (QB, TE1).
- For flex/RB2/WR3, favor upside.
- On waivers: only claim if the add is a clear upgrade over the drop target.
- On trades: evaluate both sides fairly using real stats. Accept if the return is equal or better value.
- Never drop a player on IR until their return timeline is confirmed as "out for season."

OUTPUT FORMAT:
Return a JSON object with your decisions. Example:
{
  "action": "set_lineup" | "waiver_claim" | "propose_trade" | "accept_trade" | "reject_trade" | "no_action",
  "reasoning": "brief explanation",
  "starters": [...player_ids...],           // for set_lineup
  "add_player_id": "...",                   // for waiver_claim
  "drop_player_id": "...",                  // for waiver_claim
  "waiver_bid": 0,                          // for waiver_claim (FAAB leagues)
  "trade_give": [...player_ids...],         // for propose_trade
  "trade_get": [...player_ids...],          // for propose_trade
  "trade_target_roster_id": 1,             // for propose_trade
  "transaction_id": "...",                 // for accept/reject_trade
  "notify_user": true | false,             // whether to send a Telegram message
  "notify_message": "..."                  // what to tell the user (BILL voice, casual)
}"""


def ask_agent(task: str, context: dict) -> dict:
    """Ask Claude to make a fantasy decision given context."""
    today = datetime.now(EASTERN).strftime("%B %-d, %Y")

    # Enrich context with BILL's injury data
    try:
        injuries = nfl_tools.get_nfl_injuries()
        context["current_injuries"] = injuries.get("injuries", [])[:30]
    except Exception:
        pass

    prompt = (
        f"Today: {today}\n\n"
        f"Task: {task}\n\n"
        f"Context:\n{json.dumps(context, indent=2, default=str)}\n\n"
        "Return ONLY a valid JSON object with your decision. No extra text."
    )

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        system=FANTASY_AGENT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def ask_bill(question: str) -> str:
    """Ask BILL a quick football question for trade/waiver analysis."""
    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=400,
        system="You are BILL, an NFL expert. Answer briefly and factually. Use real stats from tools when relevant.",
        tools=nfl_tools.NFL_TOOLS,
        messages=[{"role": "user", "content": question}],
    )
    # Handle tool use loop
    messages = [{"role": "user", "content": question}]
    for _ in range(3):
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                try:
                    result = nfl_tools.TOOL_DISPATCH[block.name](**block.input)
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})
        resp = client.messages.create(
            model="claude-opus-4-6", max_tokens=400,
            system="You are BILL, an NFL expert. Answer briefly and factually.",
            tools=nfl_tools.NFL_TOOLS, messages=messages,
        )
    return next((b.text for b in resp.content if hasattr(b, "text")), "")


# ── Agent Tasks ───────────────────────────────────────────────────────────────
def optimize_lineup():
    """Check injuries and set the optimal starting lineup."""
    print("[Fantasy] Optimizing lineup…")
    week    = get_current_week()
    roster  = my_roster_summary()
    projs   = get_player_projections(week)

    # Attach projections to each player
    for p in roster["starters"] + roster["bench"]:
        p["projected_pts"] = projs.get(p["id"], {}).get("pts_ppr", 0)

    decision = ask_agent(
        "Optimize the starting lineup. Check for injured/doubtful starters and swap in the best available bench players.",
        {"roster": roster, "week": week},
    )

    if decision.get("action") == "set_lineup" and decision.get("starters"):
        ok = set_starters(decision["starters"])
        if ok and decision.get("notify_user"):
            notify(decision.get("notify_message", "✅ Lineup updated."))


def check_waivers():
    """Analyze waiver wire and submit the best claim."""
    print("[Fantasy] Checking waiver wire…")
    week   = get_current_week()
    roster = my_roster_summary()
    avail  = available_players(limit=40)
    projs  = get_player_projections(week)

    for p in avail:
        p["projected_pts"] = projs.get(p["id"], {}).get("pts_ppr", 0)

    # Get BILL's take on the top trending available player
    if avail:
        top = avail[0]
        bill_take = ask_bill(f"Quick take on {top['name']} ({top['position']}, {top['team']}) for fantasy right now?")
    else:
        bill_take = ""

    decision = ask_agent(
        "Review the available free agents. If any player is a clear upgrade over a bench player, submit a waiver claim.",
        {"roster": roster, "available_players": avail[:20], "week": week, "bill_analysis": bill_take},
    )

    if decision.get("action") == "waiver_claim":
        add  = decision.get("add_player_id")
        drop = decision.get("drop_player_id")
        bid  = decision.get("waiver_bid", 0)
        ok   = submit_waiver_claim(add, drop, bid)
        if ok and decision.get("notify_user"):
            notify(decision.get("notify_message", f"📋 Waiver claim submitted."))


def check_trades():
    """Check for incoming trade offers and respond. Also look for proactive trade targets."""
    print("[Fantasy] Checking trades…")
    week         = get_current_week()
    transactions = get_transactions(week)
    my_roster    = get_my_roster()
    rosters      = get_rosters()

    # ── Incoming trade offers ──
    pending = [t for t in transactions
               if t.get("type") == "trade"
               and t.get("status") == "pending"
               and _roster_id in t.get("roster_ids", [])]

    for trade in pending:
        tid = trade.get("transaction_id") or trade.get("id")

        # Build a readable description of the trade
        adds  = trade.get("adds", {})
        give  = [player_name(pid) for pid, rid in adds.items() if rid != _roster_id]
        get_p = [player_name(pid) for pid, rid in adds.items() if rid == _roster_id]

        bill_analysis = ask_bill(
            f"Fantasy trade eval: I give {give}, I get {get_p}. Fair deal?"
        )

        decision = ask_agent(
            f"Evaluate this incoming trade offer. Accept or reject?",
            {
                "trade_give": give, "trade_get": get_p,
                "my_roster": my_roster_summary(),
                "bill_analysis": bill_analysis,
                "transaction_id": tid,
            }
        )

        action = decision.get("action")
        if action == "accept_trade":
            respond_to_trade(tid, accept=True)
            notify(decision.get("notify_message",
                   f"🤝 Trade accepted: got {get_p} for {give}"))
        elif action == "reject_trade":
            respond_to_trade(tid, accept=False)

    # ── Proactive trade hunting (once a week) ──
    now = datetime.now(EASTERN)
    if now.weekday() == 1:  # Tuesday only
        _hunt_for_trades(rosters, week)


def _hunt_for_trades(rosters: list, week: int):
    """Look for a trade that improves the team."""
    my_roster  = my_roster_summary()
    all_players = get_all_players()
    projs      = get_player_projections(week)

    # Find teams with surplus at a position I'm weak at
    other_rosters = []
    for r in rosters:
        if r["roster_id"] == _roster_id:
            continue
        players = [
            {**player_info(pid), "proj": projs.get(pid, {}).get("pts_ppr", 0)}
            for pid in (r.get("players") or [])
        ]
        other_rosters.append({"roster_id": r["roster_id"], "players": players[:10]})

    decision = ask_agent(
        "Scout for a trade that improves my team. Identify one good target and propose the trade.",
        {"my_roster": my_roster, "other_rosters": other_rosters[:8]},
    )

    if decision.get("action") == "propose_trade":
        give      = decision.get("trade_give", [])
        get_p     = decision.get("trade_get", [])
        target_id = decision.get("trade_target_roster_id")
        if give and get_p and target_id:
            ok = propose_trade(target_id, give, get_p)
            if ok:
                give_names = [player_name(p) for p in give]
                get_names  = [player_name(p) for p in get_p]
                notify(decision.get("notify_message",
                    f"📤 Trade proposed: sending {give_names} for {get_names}"))


def weekly_recap():
    """Send a recap of last week's performance."""
    print("[Fantasy] Sending weekly recap…")
    week = get_current_week() - 1
    if week < 1:
        return

    matchups = get_matchups(week)
    stats    = get_player_stats(week)
    roster   = get_my_roster()
    starters = roster.get("starters", [])

    # Find my matchup
    my_matchup = next((m for m in matchups if m.get("roster_id") == _roster_id), None)
    if not my_matchup:
        return

    my_score  = my_matchup.get("points", 0)
    opp_match = next((m for m in matchups
                      if m.get("matchup_id") == my_matchup.get("matchup_id")
                      and m.get("roster_id") != _roster_id), None)
    opp_score = opp_match.get("points", 0) if opp_match else 0
    won       = my_score > opp_score

    # Build player breakdown
    breakdown = []
    for pid in starters:
        s    = stats.get(pid, {})
        pts  = s.get("pts_ppr", 0)
        name = player_name(pid)
        breakdown.append({"name": name, "pts": round(float(pts), 1)})
    breakdown.sort(key=lambda x: x["pts"], reverse=True)

    result_str = "W" if won else "L"
    lines = [
        f"Week {week} recap: {result_str} {my_score:.1f}–{opp_score:.1f}",
        "Starters: " + " | ".join(f"{p['name']} {p['pts']}" for p in breakdown[:5]),
    ]
    if len(breakdown) > 5:
        lines.append("Bench: " + " | ".join(f"{p['name']} {p['pts']}" for p in breakdown[5:]))

    bill_recap = ask_bill(
        f"Fantasy recap week {week}: my team scored {my_score:.1f} ({'won' if won else 'lost'}). "
        f"Top scorer was {breakdown[0]['name']} with {breakdown[0]['pts']} pts. "
        f"Brief takeaways for next week?"
    )

    notify(f"<b>📊 Week {week} Recap</b>\n{'✅ WIN' if won else '❌ LOSS'} {my_score:.1f}–{opp_score:.1f}")
    time.sleep(0.4)
    for part in bill_recap.split("|||"):
        if part.strip():
            notify(part.strip())
            time.sleep(0.3)


def notable_performances():
    """Alert user when any player has an exceptional or terrible game."""
    print("[Fantasy] Checking notable performances…")
    week  = get_current_week()
    stats = get_player_stats(week)

    if not stats:
        return

    boom_threshold = 30.0  # PPR points
    bust_threshold = 3.0

    boom_players = []
    bust_players = []

    for pid, s in stats.items():
        pts = float(s.get("pts_ppr", 0) or 0)
        if pts >= boom_threshold:
            boom_players.append((player_name(pid), pts))
        elif 0 < pts <= bust_threshold and s.get("gp", 0):
            bust_players.append((player_name(pid), pts))

    boom_players.sort(key=lambda x: x[1], reverse=True)
    bust_players.sort(key=lambda x: x[1])

    if boom_players[:3]:
        names = ", ".join(f"{n} ({p:.1f})" for n, p in boom_players[:3])
        take  = ask_bill(f"{boom_players[0][0]} just dropped {boom_players[0][1]:.1f} fantasy points. Hot take?")
        notify(f"🔥 <b>Big weeks:</b> {names}")
        time.sleep(0.3)
        for part in take.split("|||"):
            if part.strip():
                notify(part.strip())
                time.sleep(0.3)

    if bust_players[:3]:
        names = ", ".join(f"{n} ({p:.1f})" for n, p in bust_players[:3])
        take  = ask_bill(f"{bust_players[0][0]} only scored {bust_players[0][1]:.1f} points today. What happened?")
        notify(f"💀 <b>Rough ones:</b> {names}")
        time.sleep(0.3)
        for part in take.split("|||"):
            if part.strip():
                notify(part.strip())
                time.sleep(0.3)


# ── Startup ───────────────────────────────────────────────────────────────────
def init():
    """Login and load initial data. Called once at startup."""
    sleeper_login()
    get_my_roster()   # sets _roster_id
    get_all_players() # warms player cache
    print(f"[Fantasy] Ready. Roster ID: {_roster_id}")
