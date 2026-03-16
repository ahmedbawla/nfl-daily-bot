"""
Fantasy Football Agent — Per-user, class-based autonomous Sleeper manager.
Each Telegram user gets their own FantasyAgent instance with their own credentials.
"""

import os
import json
import time
import requests
from datetime import datetime
from anthropic import Anthropic
import pytz
import nfl_tools

EASTERN      = pytz.timezone("America/New_York")
SLEEPER_BASE = "https://api.sleeper.app/v1"
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
client = Anthropic()


# ── Telegram (standalone, no circular import) ─────────────────────────────────
def send_message(chat_id: str, text: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id":                  chat_id,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )


def send_messages(chat_id: str, text: str):
    """Split on ||| and send each part as a separate message."""
    for part in text.split("|||"):
        if part.strip():
            send_message(chat_id, part.strip())
            time.sleep(0.3)


# ── FantasyAgent class ────────────────────────────────────────────────────────
class FantasyAgent:
    def __init__(self, username: str, password: str, league_id: str, chat_id: str):
        self.username   = username
        self.password   = password
        self.league_id  = league_id
        self.chat_id    = chat_id
        self._token     = None
        self._user_id   = None
        self._roster_id = None
        self._players   = {}   # full player cache

    # ── Auth ──────────────────────────────────────────────────────────────────
    def login(self):
        resp = requests.post(f"{SLEEPER_BASE}/login", json={
            "login": self.username, "password": self.password,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self._token   = data.get("token")
        self._user_id = str(data.get("user_id", ""))

    def init(self):
        """Login, find roster, warm player cache. Raises on bad credentials."""
        self.login()
        self._load_my_roster()
        self._cache_players()

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _notify(self, text: str):
        send_message(self.chat_id, text)

    def _notify_multi(self, text: str):
        send_messages(self.chat_id, text)

    # ── Sleeper reads ─────────────────────────────────────────────────────────
    def _get(self, path: str, **params) -> any:
        resp = requests.get(f"{SLEEPER_BASE}{path}", params=params, timeout=15)
        return resp.json() if resp.ok else {}

    def get_league(self) -> dict:
        return self._get(f"/league/{self.league_id}")

    def get_rosters(self) -> list:
        return self._get(f"/league/{self.league_id}/rosters") or []

    def _load_my_roster(self):
        for r in self.get_rosters():
            if str(r.get("owner_id")) == self._user_id:
                self._roster_id = r["roster_id"]
                return r
        raise RuntimeError("Your roster not found in this league. Check your league ID.")

    def get_my_roster(self) -> dict:
        for r in self.get_rosters():
            if str(r.get("owner_id")) == self._user_id:
                self._roster_id = r["roster_id"]
                return r
        return {}

    def get_matchups(self, week: int) -> list:
        return self._get(f"/league/{self.league_id}/matchups/{week}") or []

    def get_transactions(self, week: int) -> list:
        return self._get(f"/league/{self.league_id}/transactions/{week}") or []

    def get_projections(self, week: int) -> dict:
        s = datetime.now(EASTERN).year
        resp = requests.get(
            f"{SLEEPER_BASE}/projections/nfl/regular/{s}/{week}",
            params={"position[]": ["QB","RB","WR","TE","K","DEF"]},
            timeout=10,
        )
        return resp.json() if resp.ok else {}

    def get_stats(self, week: int) -> dict:
        s = datetime.now(EASTERN).year
        resp = requests.get(
            f"{SLEEPER_BASE}/stats/nfl/regular/{s}/{week}",
            params={"position[]": ["QB","RB","WR","TE","K","DEF"]},
            timeout=10,
        )
        return resp.json() if resp.ok else {}

    def get_current_week(self) -> int:
        return int(self.get_league().get("settings", {}).get("leg", 1))

    def _cache_players(self):
        if not self._players:
            resp = requests.get(f"{SLEEPER_BASE}/players/nfl", timeout=30)
            self._players = resp.json() if resp.ok else {}

    def player_name(self, pid: str) -> str:
        p = self._players.get(str(pid), {})
        return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or pid

    def player_info(self, pid: str) -> dict:
        p = self._players.get(str(pid), {})
        return {
            "id": pid, "name": p.get("full_name", pid),
            "position": p.get("position", ""),
            "team": p.get("team", "FA"),
            "status": p.get("injury_status", "Active") or "Active",
        }

    def available_players(self, limit: int = 40) -> list:
        rostered = {p for r in self.get_rosters() for p in (r.get("players") or [])}
        trending = {t["player_id"] for t in (
            requests.get(f"{SLEEPER_BASE}/players/nfl/trending/add",
                         params={"lookback_hours": 48, "limit": 25}, timeout=10).json() or []
        )}
        avail = []
        for pid, p in self._players.items():
            if pid in rostered:
                continue
            pos    = p.get("position", "")
            status = p.get("injury_status", "")
            if pos not in ["QB","RB","WR","TE","K","DEF"] or status in ["IR","PUP"]:
                continue
            avail.append({
                "id": pid, "name": p.get("full_name", pid),
                "position": pos, "team": p.get("team","FA"),
                "trending": pid in trending, "status": status or "Active",
            })
        return avail[:limit]

    def roster_summary(self) -> dict:
        roster   = self.get_my_roster()
        starters = roster.get("starters", [])
        bench    = [p for p in (roster.get("players") or []) if p not in starters]
        return {
            "starters": [self.player_info(p) for p in starters],
            "bench":    [self.player_info(p) for p in bench],
        }

    # ── Sleeper writes ────────────────────────────────────────────────────────
    def set_starters(self, starter_ids: list) -> bool:
        resp = requests.put(
            f"{SLEEPER_BASE}/league/{self.league_id}/rosters/{self._roster_id}/starters",
            headers=self._headers(), json={"starters": starter_ids}, timeout=10,
        )
        return resp.ok

    def submit_waiver(self, add_id: str, drop_id: str, bid: int = 0) -> bool:
        league  = self.get_league()
        tx_type = "waiver" if league.get("settings", {}).get("waiver_type", 0) else "free_agent"
        payload = {
            "type": tx_type, "roster_id": self._roster_id,
            "adds": {add_id: self._roster_id},
            "drops": {drop_id: self._roster_id} if drop_id else {},
            "settings": {"waiver_bid": bid} if tx_type == "waiver" else {},
            "metadata": {},
        }
        resp = requests.post(
            f"{SLEEPER_BASE}/league/{self.league_id}/transactions",
            headers=self._headers(), json=payload, timeout=10,
        )
        return resp.ok

    def propose_trade(self, target_roster_id: int, give_ids: list, get_ids: list) -> bool:
        adds = {pid: self._roster_id       for pid in get_ids}
        adds.update({pid: target_roster_id for pid in give_ids})
        resp = requests.post(
            f"{SLEEPER_BASE}/league/{self.league_id}/transactions",
            headers=self._headers(),
            json={"type":"trade","roster_ids":[self._roster_id,target_roster_id],
                  "adds":adds,"drops":{},"settings":{},"metadata":{}},
            timeout=10,
        )
        return resp.ok

    def respond_trade(self, transaction_id: str, accept: bool) -> bool:
        action = "accept" if accept else "reject"
        resp = requests.post(
            f"{SLEEPER_BASE}/league/{self.league_id}/transactions/{transaction_id}/{action}",
            headers=self._headers(), timeout=10,
        )
        return resp.ok

    # ── Claude decision engine ────────────────────────────────────────────────
    DECISION_SYSTEM = """You are an autonomous fantasy football manager making decisions for a Sleeper league team.
You have access to real NFL data tools via BILL.

RULES:
- Check injury status before setting any player as starter.
- Only claim a waiver player if they are a clear upgrade over someone on bench.
- On trades: accept if the return is equal or better value. Reject bad deals immediately.
- Never drop a key player without a verified replacement.

Return ONLY valid JSON — no extra text:
{
  "action": "set_lineup"|"waiver_claim"|"propose_trade"|"accept_trade"|"reject_trade"|"no_action",
  "reasoning": "brief reason",
  "starters": [...],              // set_lineup: list of player IDs
  "add_player_id": "...",         // waiver_claim
  "drop_player_id": "...",        // waiver_claim
  "waiver_bid": 0,                // waiver_claim FAAB
  "trade_give": [...],            // propose_trade: player IDs to give
  "trade_get": [...],             // propose_trade: player IDs to get
  "trade_target_roster_id": 1,   // propose_trade
  "transaction_id": "...",        // accept/reject_trade
  "notify_user": true|false,
  "notify_message": "..."         // casual BILL-style message to user
}"""

    def _decide(self, task: str, context: dict) -> dict:
        try:
            injuries = nfl_tools.get_nfl_injuries()
            context["current_injuries"] = injuries.get("injuries", [])[:25]
        except Exception:
            pass
        today  = datetime.now(EASTERN).strftime("%B %-d, %Y")
        prompt = f"Today: {today}\nTask: {task}\n\nContext:\n{json.dumps(context, default=str)}\n\nReturn JSON only."
        resp   = client.messages.create(
            model="claude-opus-4-6", max_tokens=800,
            system=self.DECISION_SYSTEM,
            messages=[{"role":"user","content":prompt}],
        )
        raw = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(raw)

    def _ask_bill(self, question: str) -> str:
        msgs = [{"role":"user","content":question}]
        resp = client.messages.create(
            model="claude-opus-4-6", max_tokens=300,
            system="You are BILL, a sharp NFL expert. Answer briefly.",
            tools=nfl_tools.NFL_TOOLS, messages=msgs,
        )
        for _ in range(3):
            msgs.append({"role":"assistant","content":resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    try:   r = nfl_tools.TOOL_DISPATCH[b.name](**b.input)
                    except Exception as e: r = {"error": str(e)}
                    results.append({"type":"tool_result","tool_use_id":b.id,"content":json.dumps(r)})
            msgs.append({"role":"user","content":results})
            resp = client.messages.create(
                model="claude-opus-4-6", max_tokens=300,
                system="You are BILL, a sharp NFL expert. Answer briefly.",
                tools=nfl_tools.NFL_TOOLS, messages=msgs,
            )
        return next((b.text for b in resp.content if hasattr(b,"text")), "")

    # ── Agent tasks ───────────────────────────────────────────────────────────
    def optimize_lineup(self):
        print(f"[Fantasy:{self.chat_id}] Optimizing lineup…")
        week   = self.get_current_week()
        roster = self.roster_summary()
        projs  = self.get_projections(week)
        for p in roster["starters"] + roster["bench"]:
            p["projected_pts"] = projs.get(p["id"], {}).get("pts_ppr", 0)
        try:
            d = self._decide("Optimize starting lineup. Bench injured/doubtful players.", {"roster":roster,"week":week})
            if d.get("action") == "set_lineup" and d.get("starters"):
                ok = self.set_starters(d["starters"])
                if ok and d.get("notify_user"):
                    self._notify(d.get("notify_message","✅ Lineup updated."))
        except Exception as e:
            print(f"[Fantasy] optimize_lineup error: {e}")

    def check_waivers(self):
        print(f"[Fantasy:{self.chat_id}] Checking waivers…")
        week   = self.get_current_week()
        roster = self.roster_summary()
        avail  = self.available_players(limit=30)
        projs  = self.get_projections(week)
        for p in avail:
            p["projected_pts"] = projs.get(p["id"], {}).get("pts_ppr", 0)
        bill   = self._ask_bill(f"Best available WR/RB on waivers right now for fantasy?") if avail else ""
        try:
            d = self._decide("Find the best waiver add. Only claim if it's a clear upgrade.",
                             {"roster":roster,"available":avail[:15],"week":week,"bill_take":bill})
            if d.get("action") == "waiver_claim":
                ok = self.submit_waiver(d["add_player_id"], d.get("drop_player_id",""), d.get("waiver_bid",0))
                if ok and d.get("notify_user"):
                    self._notify(d.get("notify_message","📋 Waiver claim submitted."))
        except Exception as e:
            print(f"[Fantasy] check_waivers error: {e}")

    def check_trades(self):
        print(f"[Fantasy:{self.chat_id}] Checking trades…")
        week  = self.get_current_week()
        txns  = self.get_transactions(week)
        rid   = self._roster_id

        # Incoming pending trades
        pending = [t for t in txns
                   if t.get("type") == "trade"
                   and t.get("status") == "pending"
                   and rid in (t.get("roster_ids") or [])]

        for trade in pending:
            tid   = str(trade.get("transaction_id") or trade.get("id",""))
            adds  = trade.get("adds", {})
            give  = [self.player_name(pid) for pid,r in adds.items() if r != rid]
            get_p = [self.player_name(pid) for pid,r in adds.items() if r == rid]
            bill  = self._ask_bill(f"Fantasy trade: I give {give}, I get {get_p}. Good deal?")
            try:
                d = self._decide("Evaluate and respond to this trade.",
                                 {"give":give,"get":get_p,"my_roster":self.roster_summary(),
                                  "bill_analysis":bill,"transaction_id":tid})
                if d.get("action") == "accept_trade":
                    self.respond_trade(tid, True)
                    self._notify(d.get("notify_message", f"🤝 Trade accepted: got {get_p} for {give}"))
                elif d.get("action") == "reject_trade":
                    self.respond_trade(tid, False)
            except Exception as e:
                print(f"[Fantasy] trade response error: {e}")

        # Proactive trade hunting on Tuesdays
        if datetime.now(EASTERN).weekday() == 1:
            self._hunt_trades()

    def _hunt_trades(self):
        rosters = self.get_rosters()
        projs   = self.get_projections(self.get_current_week())
        others  = []
        for r in rosters:
            if r["roster_id"] == self._roster_id:
                continue
            players = [{**self.player_info(pid), "proj": projs.get(pid,{}).get("pts_ppr",0)}
                       for pid in (r.get("players") or [])[:8]]
            others.append({"roster_id": r["roster_id"], "players": players})
        try:
            d = self._decide("Scout for a trade that improves my team. Propose one if you find a good target.",
                             {"my_roster": self.roster_summary(), "other_rosters": others[:8]})
            if d.get("action") == "propose_trade":
                give = d.get("trade_give",[])
                get  = d.get("trade_get",[])
                tgt  = d.get("trade_target_roster_id")
                if give and get and tgt:
                    ok = self.propose_trade(tgt, give, get)
                    if ok:
                        self._notify(d.get("notify_message",
                            f"📤 Trade proposed: sending {[self.player_name(p) for p in give]} "
                            f"for {[self.player_name(p) for p in get]}"))
        except Exception as e:
            print(f"[Fantasy] hunt_trades error: {e}")

    def weekly_recap(self):
        print(f"[Fantasy:{self.chat_id}] Sending weekly recap…")
        week = self.get_current_week() - 1
        if week < 1:
            return
        matchups = self.get_matchups(week)
        stats    = self.get_stats(week)
        roster   = self.get_my_roster()
        rid      = self._roster_id

        my_match  = next((m for m in matchups if m.get("roster_id") == rid), None)
        if not my_match:
            return
        opp_match = next((m for m in matchups
                          if m.get("matchup_id") == my_match.get("matchup_id")
                          and m.get("roster_id") != rid), None)

        my_score  = float(my_match.get("points", 0))
        opp_score = float(opp_match.get("points", 0)) if opp_match else 0
        won       = my_score > opp_score

        breakdown = sorted([
            {"name": self.player_name(pid), "pts": round(float(stats.get(pid,{}).get("pts_ppr",0)),1)}
            for pid in roster.get("starters", [])
        ], key=lambda x: x["pts"], reverse=True)

        bill_take = self._ask_bill(
            f"Fantasy week {week}: my team scored {my_score:.1f} and {'won' if won else 'lost'}. "
            f"Top scorer: {breakdown[0]['name'] if breakdown else 'N/A'}. Quick takeaways?"
        )

        self._notify(
            f"<b>📊 Week {week} Recap</b>\n"
            f"{'✅ WIN' if won else '❌ LOSS'}  {my_score:.1f}–{opp_score:.1f}\n\n"
            f"<b>Starters:</b>\n" +
            "\n".join(f"  {p['name']}: {p['pts']}" for p in breakdown)
        )
        time.sleep(0.4)
        self._notify_multi(bill_take)

    def notable_performances(self):
        print(f"[Fantasy:{self.chat_id}] Checking notable performances…")
        week  = self.get_current_week()
        stats = self.get_stats(week)
        if not stats:
            return

        boom, bust = [], []
        for pid, s in stats.items():
            pts = float(s.get("pts_ppr", 0) or 0)
            if pts >= 30:
                boom.append((self.player_name(pid), pts))
            elif 0 < pts <= 3 and s.get("gp"):
                bust.append((self.player_name(pid), pts))

        boom.sort(key=lambda x: x[1], reverse=True)
        bust.sort(key=lambda x: x[1])

        if boom[:3]:
            names    = ", ".join(f"{n} ({p:.1f})" for n,p in boom[:3])
            bill_hot = self._ask_bill(f"{boom[0][0]} just dropped {boom[0][1]:.1f} pts. Hot take?")
            self._notify(f"🔥 <b>Big weeks:</b> {names}")
            time.sleep(0.3)
            self._notify_multi(bill_hot)

        if bust[:3]:
            names     = ", ".join(f"{n} ({p:.1f})" for n,p in bust[:3])
            bill_cold = self._ask_bill(f"{bust[0][0]} only scored {bust[0][1]:.1f} pts. What happened?")
            self._notify(f"💀 <b>Rough ones:</b> {names}")
            time.sleep(0.3)
            self._notify_multi(bill_cold)
