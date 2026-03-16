"""
NFL Daily Telegram Bot
- /update or /nfl  → general NFL digest
- /fantasy         → fantasy football focused digest
- /draft           → NFL draft news and prospect updates
- Any other text   → all-knowing NFL agent (Q&A, analysis, history)
- Midnight ET      → auto daily digest
Sources: ESPN · Reddit · nflreadpy (NGS/PBP/stats 1999-present) · Brave Web Search
"""

import os
import sys
import time
import json
import requests
from anthropic import Anthropic
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from nfl_tools import NFL_TOOLS, TOOL_DISPATCH
from fantasy_agent import FantasyAgent, send_message, send_messages

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]   # owner — gets nightly digest

client  = Anthropic()
HEADERS = {"User-Agent": "NFLDailyBot/1.0 (Telegram digest bot)"}
EASTERN = pytz.timezone("America/New_York")

# ── Per-user state ────────────────────────────────────────────────────────────
conversation_history = defaultdict(list)  # chat_id → message history
fantasy_agents: dict[str, FantasyAgent] = {}  # chat_id → FantasyAgent

# Onboarding state machine
ONBOARD_STATE = {}   # chat_id → "username" | "password" | "league"
ONBOARD_DATA  = {}   # chat_id → {"username": ..., "password": ...}

USERS_FILE = "users.json"


def _load_users():
    """Load saved user credentials and restore FantasyAgent instances."""
    try:
        with open(USERS_FILE) as f:
            users = json.load(f)
        for cid, u in users.items():
            try:
                agent = FantasyAgent(u["username"], u["password"], u["league_id"], cid)
                agent.init()
                fantasy_agents[cid] = agent
                print(f"[Users] Restored agent for chat {cid}")
            except Exception as e:
                print(f"[Users] Could not restore agent for {cid}: {e}")
    except FileNotFoundError:
        pass


def _save_user(chat_id: str, username: str, password: str, league_id: str):
    try:
        with open(USERS_FILE) as f:
            users = json.load(f)
    except FileNotFoundError:
        users = {}
    users[chat_id] = {"username": username, "password": password, "league_id": league_id}
    with open(USERS_FILE, "w") as f:
        json.dump(users, f)


# ── ESPN Scoreboard ───────────────────────────────────────────────────────────
def get_nfl_scores(date_str: str) -> list[dict]:
    """date_str: YYYYMMDD (ESPN format)"""
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"dates": date_str},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        games = []
        for event in resp.json().get("events", []):
            comp  = event["competitions"][0]
            teams = comp["competitors"]
            home  = next(t for t in teams if t["homeAway"] == "home")
            away  = next(t for t in teams if t["homeAway"] == "away")
            st    = comp["status"]["type"]
            games.append({
                "home":       home["team"]["displayName"],
                "away":       away["team"]["displayName"],
                "home_score": home.get("score", "—"),
                "away_score": away.get("score", "—"),
                "completed":  st.get("completed", False),
                "status":     st.get("description", "Scheduled"),
            })
        return games
    except Exception as e:
        print(f"[WARN] ESPN scoreboard ({date_str}): {e}")
        return []


def fmt_score(g: dict) -> str:
    if g["completed"]:
        winner = g["home"] if int(g["home_score"] or 0) > int(g["away_score"] or 0) else g["away"]
        return (
            f"{g['away']} {g['away_score']} @ {g['home']} {g['home_score']} "
            f"[Final — {winner} win]"
        )
    return f"{g['away']} @ {g['home']} [{g['status']}]"


# ── ESPN News ─────────────────────────────────────────────────────────────────
def get_nfl_news(limit: int = 10) -> list[str]:
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
            params={"limit": limit},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        out = []
        for a in resp.json().get("articles", []):
            headline    = a.get("headline", "").strip()
            description = a.get("description", "").strip()
            if headline:
                entry = headline
                if description and description.lower() != headline.lower():
                    desc_short = description[:120] + ("…" if len(description) > 120 else "")
                    entry += f" — {desc_short}"
                out.append(entry)
        return out
    except Exception as e:
        print(f"[WARN] ESPN news: {e}")
        return []


# ── ESPN Injuries ─────────────────────────────────────────────────────────────
def get_nfl_injuries() -> list[str]:
    """Current NFL injury report — key for fantasy decisions."""
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        injuries = []
        for team_block in resp.json().get("injuries", []):
            team_name = team_block.get("team", {}).get("displayName", "")
            for player in team_block.get("injuries", []):
                athlete = player.get("athlete", {})
                name    = athlete.get("displayName", "")
                pos     = athlete.get("position", {}).get("abbreviation", "")
                status  = player.get("status", "")
                details = player.get("details", {})
                inj_type = details.get("type", "")
                side     = details.get("side", "")
                fantasy_status = details.get("fantasyStatus", {}).get("description", "")

                if not name or not status:
                    continue

                line = f"{name} ({pos}, {team_name}) — {status}"
                if inj_type:
                    line += f" [{inj_type}{' ' + side if side else ''}]"
                if fantasy_status:
                    line += f" · {fantasy_status}"
                injuries.append(line)
        return injuries[:25]
    except Exception as e:
        print(f"[WARN] ESPN injuries: {e}")
        return []


# ── Reddit ────────────────────────────────────────────────────────────────────
def get_reddit_top(subreddit: str, limit: int = 15) -> list[str]:
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/top.json",
            params={"t": "day", "limit": limit},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        buzz = []
        for p in resp.json().get("data", {}).get("children", []):
            d     = p.get("data", {})
            title = d.get("title", "").strip()
            score = d.get("score", 0)
            flair = d.get("link_flair_text", "")
            if not title or (flair and "megathread" in flair.lower() and score < 500):
                continue
            tag = f"[{flair}] " if flair else ""
            buzz.append(f"{tag}{title}  (↑{score:,})")
        return buzz[:10]
    except Exception as e:
        print(f"[WARN] Reddit r/{subreddit}: {e}")
        return []


# ── Telegram helpers ──────────────────────────────────────────────────────────
def send_telegram(text: str, chat_id: str = None):
    cid  = chat_id or TELEGRAM_CHAT_ID
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":                  cid,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    data = resp.json()
    if not data.get("ok"):
        print(f"[ERROR] Telegram: {data}", file=sys.stderr)
    else:
        print("✓ Message sent.")


def send_typing(chat_id: str = None):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
        json={"chat_id": chat_id or TELEGRAM_CHAT_ID, "action": "typing"},
        timeout=5,
    )


# ── Onboarding ────────────────────────────────────────────────────────────────
def handle_onboarding(chat_id: str, text: str) -> bool:
    """
    Returns True if this message was consumed by onboarding.
    New users are intercepted here before reaching the agent.
    """
    # Trigger onboarding for users we haven't seen yet
    if chat_id not in fantasy_agents and chat_id not in ONBOARD_STATE:
        ONBOARD_STATE[chat_id] = "username"
        ONBOARD_DATA[chat_id]  = {}
        send_telegram(
            "Hey! I'm <b>BILL</b> 🏈 — your NFL expert and fantasy manager.\n\n"
            "Let's link your Sleeper account. What's your <b>Sleeper username</b>?",
            chat_id,
        )
        return True

    state = ONBOARD_STATE.get(chat_id)
    if not state:
        return False

    if state == "username":
        ONBOARD_DATA[chat_id]["username"] = text
        ONBOARD_STATE[chat_id] = "password"
        send_telegram("Got it. Now your <b>Sleeper password</b>?", chat_id)
        return True

    if state == "password":
        ONBOARD_DATA[chat_id]["password"] = text
        ONBOARD_STATE[chat_id] = "league"
        send_telegram(
            "Almost there. What's your <b>league ID</b>?\n"
            "<i>(Find it in your league URL: sleeper.com/leagues/<b>XXXXXXXXX</b>)</i>",
            chat_id,
        )
        return True

    if state == "league":
        ONBOARD_DATA[chat_id]["league_id"] = text
        d = ONBOARD_DATA[chat_id]
        send_telegram("Connecting to Sleeper…", chat_id)
        try:
            agent = FantasyAgent(d["username"], d["password"], text, chat_id)
            agent.init()
            fantasy_agents[chat_id] = agent
            _save_user(chat_id, d["username"], d["password"], text)
            del ONBOARD_STATE[chat_id]
            del ONBOARD_DATA[chat_id]
            send_telegram(
                "✅ All set! Your fantasy team is linked and I'm managing it autonomously.\n\n"
                "You can now ask me anything about the NFL, or tell me to make a move:\n"
                "<i>\"Start Lamar over Mahomes this week\"</i>\n"
                "<i>\"Drop [Player] and pick up [Player]\"</i>\n"
                "<i>\"What do you think about trading X for Y?\"</i>",
                chat_id,
            )
        except Exception as e:
            ONBOARD_STATE[chat_id] = "username"
            ONBOARD_DATA[chat_id]  = {}
            send_telegram(
                f"❌ Couldn't connect: <i>{e}</i>\n\nLet's try again. What's your Sleeper username?",
                chat_id,
            )
        return True

    return False


# ── NFL Digest ────────────────────────────────────────────────────────────────
NFL_SYSTEM_PROMPT = """You are a sharp, knowledgeable NFL analyst writing a daily Telegram digest.
Your job: distill a lot of information into one tight, satisfying message.

RULES:
- ONE message only. No multi-part replies.
- Absolute max: 550 words / ~3,000 characters.
- Use Telegram HTML only: <b>bold</b>, <i>italic</i>. No markdown, no asterisks.
- Structure with clear emoji section headers so it's skimmable at a glance.
- Be selective — pick the 3-4 most interesting news items and 2-3 social highlights.
- If it's the off-season, lean hard into free agency, trades, draft, coaching moves.
- Write like a smart friend who follows the NFL obsessively, not a press release.
- No fluff, no filler. Every sentence should earn its place."""

def run_digest():
    now           = datetime.now(EASTERN)
    yesterday_fmt = (now - timedelta(days=1)).strftime("%Y%m%d")
    today_fmt     = now.strftime("%Y%m%d")
    report_date   = now.strftime("%B %-d, %Y")

    print(f"[{now.isoformat()}] Running NFL digest…")

    yesterday_games = get_nfl_scores(yesterday_fmt)
    upcoming_games  = get_nfl_scores(today_fmt)
    news            = get_nfl_news(limit=10)
    reddit_buzz     = get_reddit_top("nfl", limit=15)

    lines = [f"Report date: {report_date}", ""]
    lines.append("=== YESTERDAY'S NFL SCORES ===")
    if yesterday_games:
        for g in yesterday_games:
            lines.append("  " + fmt_score(g))
    else:
        lines.append("  No games yesterday (off-season or bye week).")
    lines.append("")
    lines.append("=== UPCOMING / IN-PROGRESS GAMES ===")
    if upcoming_games:
        for g in upcoming_games:
            lines.append("  " + fmt_score(g))
    else:
        lines.append("  No games currently scheduled in the near term.")
    lines.append("")
    lines.append("=== LATEST ESPN NFL NEWS ===")
    for item in (news or ["No news available."]):
        lines.append(f"  • {item}")
    lines.append("")
    lines.append("=== REDDIT r/nfl — TOP POSTS TODAY ===")
    for item in (reddit_buzz or ["No posts found."]):
        lines.append(f"  • {item}")

    context = "\n".join(lines)
    print(context)

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1100,
        system=NFL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f"Here is today's NFL data for {report_date}:\n\n{context}\n\n"
            "Write the daily NFL Telegram digest now. Follow the system rules exactly."
        )}],
    )
    digest = resp.content[0].text.strip()
    print(digest)
    send_telegram(digest)


# ── Fantasy Digest ────────────────────────────────────────────────────────────
FANTASY_SYSTEM_PROMPT = """You are a sharp fantasy football analyst writing a daily Telegram briefing.
Focus entirely on what matters for fantasy football managers — not general NFL interest.

RULES:
- ONE message only. No multi-part replies.
- Absolute max: 550 words / ~3,000 characters.
- Use Telegram HTML only: <b>bold</b>, <i>italic</i>. No markdown, no asterisks.
- Structure with clear emoji section headers so it's skimmable.
- Always lead with the injury report — that's the #1 thing fantasy managers need.
- Then: start/sit implications, waiver wire targets, trending players, matchup notes.
- Call out specific players by name — be actionable, not vague.
- If it's the off-season, focus on: depth chart battles, camp standouts, ADP movers, rookie hype.
- Write like a fantasy expert texting their league group chat. Sharp, direct, no fluff."""

def run_fantasy_digest():
    now         = datetime.now(EASTERN)
    report_date = now.strftime("%B %-d, %Y")

    print(f"[{now.isoformat()}] Running fantasy digest…")

    injuries    = get_nfl_injuries()
    news        = get_nfl_news(limit=15)
    reddit_buzz = get_reddit_top("fantasyfootball", limit=15)

    lines = [f"Report date: {report_date} (Fantasy Focus)", ""]
    lines.append("=== INJURY REPORT ===")
    for item in (injuries or ["No significant injuries reported."]):
        lines.append(f"  • {item}")
    lines.append("")
    lines.append("=== NFL NEWS (fantasy lens) ===")
    for item in (news or ["No news available."]):
        lines.append(f"  • {item}")
    lines.append("")
    lines.append("=== r/fantasyfootball — TOP POSTS TODAY ===")
    for item in (reddit_buzz or ["No posts found."]):
        lines.append(f"  • {item}")

    context = "\n".join(lines)
    print(context)

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1100,
        system=FANTASY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f"Here is today's fantasy football data for {report_date}:\n\n{context}\n\n"
            "Write the fantasy football Telegram digest now. Follow the system rules exactly."
        )}],
    )
    digest = resp.content[0].text.strip()
    print(digest)
    send_telegram(digest)


# ── Draft Digest ─────────────────────────────────────────────────────────────
DRAFT_SYSTEM_PROMPT = """You are an NFL draft expert writing a daily Telegram briefing on the upcoming NFL Draft.

RULES:
- ONE message only. No multi-part replies.
- Absolute max: 550 words / ~3,000 characters.
- Use Telegram HTML only: <b>bold</b>, <i>italic</i>. No markdown, no asterisks.
- Structure with clear emoji section headers so it's skimmable.
- Cover: top prospect news, mock draft movement, team needs, pro day/combine results, trade rumors involving picks.
- Name specific prospects and teams — be concrete, not generic.
- Highlight any risers, fallers, or surprise storylines in the draft landscape.
- Write like a draft analyst who lives and breathes tape and prospect rankings."""

def run_draft_digest():
    now         = datetime.now(EASTERN)
    report_date = now.strftime("%B %-d, %Y")

    print(f"[{now.isoformat()}] Running draft digest…")

    # Pull more news since we need Claude to filter for draft-relevant items
    news        = get_nfl_news(limit=20)
    reddit_buzz = get_reddit_top("nfldraft", limit=15)
    # r/NFL often has draft discussion too — grab a few extra posts
    nfl_reddit  = get_reddit_top("nfl", limit=20)
    draft_nfl_posts = [p for p in nfl_reddit if any(
        kw in p.lower() for kw in ["draft", "prospect", "combine", "pro day", "mock", "pick", "round"]
    )][:5]

    lines = [f"Report date: {report_date} (NFL Draft Focus)", ""]
    lines.append("=== NFL DRAFT NEWS (ESPN) ===")
    for item in (news or ["No news available."]):
        lines.append(f"  • {item}")
    lines.append("")
    lines.append("=== r/nfldraft — TOP POSTS TODAY ===")
    for item in (reddit_buzz or ["No posts found."]):
        lines.append(f"  • {item}")
    if draft_nfl_posts:
        lines.append("")
        lines.append("=== r/nfl — DRAFT DISCUSSION ===")
        for item in draft_nfl_posts:
            lines.append(f"  • {item}")

    context = "\n".join(lines)
    print(context)

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1100,
        system=DRAFT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f"Here is today's NFL draft data for {report_date}:\n\n{context}\n\n"
            "Write the NFL draft Telegram digest now. Follow the system rules exactly."
        )}],
    )
    digest = resp.content[0].text.strip()
    print(digest)
    send_telegram(digest)


# ── NFL Agent ────────────────────────────────────────────────────────────────
AGENT_SYSTEM_PROMPT = """You are BILL — a football-obsessed friend who happens to know everything about the NFL.
You've watched every game, memorized every stat, and have a take on everything. You're confident, a little sharp, and genuinely fun to talk to.

Today's date: {today}

ACCURACY RULES (non-negotiable):
- ALWAYS call a tool before stating any specific stat, score, standing, or injury status. Never recite numbers from memory.
- For pre-1999 history, Super Bowl records, rules, or anything outside the structured tools — use web_search.
- If a specific metric isn't directly available (e.g. EPA, DVOA, passer rating, yards per route run):
    1. Use web_search to find the exact formula for that metric
    2. Use get_player_stats or other tools to fetch the raw numbers needed
    3. Calculate it yourself and present the result with your working shown
  Never say "I don't have that stat" if the underlying data exists and the metric can be derived.

PERSONALITY & FORMAT:
- You're texting a friend, not writing a report. Be casual, direct, a little personality.
- Split your response into multiple short messages using ||| as a separator between each one.
- Each message should be 1-2 sentences MAX. Short and punchy.
- 2-4 messages total per response is the sweet spot. Never more than 5.
- Use Telegram HTML only: <b>bold</b> and <i>italic</i> where it adds punch. No markdown asterisks.
- No bullet points, no headers, no lists. Just talk like a person.
- Example of good format:
  "Yeah Mahomes had a rough one last week|||But his numbers over the full season are still elite|||Chiefs are fine, don't panic"
- For complex analysis, still keep each message short — just use more of them."""


def _prune_history(chat_id: str, max_turns: int = 20):
    """Keep at most max_turns messages. Always prune in pairs to avoid orphaned tool blocks."""
    history = conversation_history[chat_id]
    while len(history) > max_turns:
        conversation_history[chat_id] = history[2:]
        history = conversation_history[chat_id]


MOVE_SYSTEM = """You are BILL managing a fantasy football roster on behalf of the user.
The user has given you a direct instruction to make a roster move.
Determine exactly what action to take and return ONLY valid JSON:
{
  "action": "set_lineup"|"waiver_claim"|"propose_trade"|"no_action",
  "reasoning": "brief explanation",
  "starters": [...player_ids...],
  "add_player_id": "...",
  "drop_player_id": "...",
  "trade_give": [...player_ids...],
  "trade_get":  [...player_ids...],
  "trade_target_roster_id": 1,
  "reply": "casual message back to user confirming what you did"
}
If the instruction is unclear or you need more info, set action to no_action and explain in reply."""

def handle_fantasy_command(chat_id: str, user_text: str) -> bool:
    """
    If the user's message looks like a direct roster instruction, execute it.
    Returns True if we handled it, False if it's just a question.
    """
    agent = fantasy_agents.get(chat_id)
    if not agent:
        return False

    # Ask Claude if this is an action command or just a question
    classify = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=10,
        messages=[{"role": "user", "content":
            f"Is this a direct fantasy football roster action instruction (start/drop/add/trade/bench someone)? "
            f"Reply only YES or NO.\n\nMessage: {user_text}"}],
    )
    if "YES" not in classify.content[0].text.upper():
        return False

    # It's a command — get roster context and execute
    try:
        roster = agent.roster_summary()
        projs  = agent.get_projections(agent.get_current_week())
        avail  = agent.available_players(limit=20)
        for p in roster["starters"] + roster["bench"] + avail:
            p["projected_pts"] = projs.get(p["id"], {}).get("pts_ppr", 0)

        prompt = (
            f"User instruction: \"{user_text}\"\n\n"
            f"Current roster:\n{json.dumps(roster, default=str)}\n\n"
            f"Available free agents:\n{json.dumps(avail[:10], default=str)}\n\n"
            "Execute the instruction. Return JSON only."
        )
        resp = client.messages.create(
            model="claude-opus-4-6", max_tokens=600,
            system=MOVE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        d   = json.loads(raw)

        action = d.get("action")
        if action == "set_lineup" and d.get("starters"):
            agent.set_starters(d["starters"])
        elif action == "waiver_claim" and d.get("add_player_id"):
            agent.submit_waiver(d["add_player_id"], d.get("drop_player_id", ""))
        elif action == "propose_trade" and d.get("trade_give") and d.get("trade_get"):
            agent.propose_trade(d["trade_target_roster_id"], d["trade_give"], d["trade_get"])

        reply = d.get("reply", "Done.")
        send_telegram(reply, chat_id)
        return True

    except Exception as e:
        print(f"[Fantasy command error] {e}")
        return False


def run_agent(chat_id: str, user_text: str):
    today = datetime.now(EASTERN).strftime("%B %-d, %Y")
    system = AGENT_SYSTEM_PROMPT.replace("{today}", today)

    conversation_history[chat_id].append({"role": "user", "content": user_text})
    messages = list(conversation_history[chat_id])

    response = None
    for _ in range(6):  # max 6 tool-call rounds
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1500,
            system=system,
            tools=NFL_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        # Dispatch all tool calls in this round
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  [TOOL] {block.name}({json.dumps(block.input)})")
                try:
                    result = TOOL_DISPATCH[block.name](**block.input)
                except Exception as e:
                    result = {"error": str(e)}
                print(f"  [TOOL] → {str(result)[:200]}")
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})

    # Extract final text
    final = next(
        (b.text for b in response.content if hasattr(b, "text") and b.text),
        "No response."
    )

    # Save only the plain-text exchange back to persistent history
    conversation_history[chat_id][-1] = {"role": "assistant", "content": final}
    _prune_history(chat_id)

    # Split on ||| and send each chunk as a separate Telegram message
    parts = [p.strip() for p in final.split("|||") if p.strip()]
    for part in parts:
        send_telegram(part, chat_id)
        time.sleep(0.3)


# ── Telegram polling ──────────────────────────────────────────────────────────
def poll():
    """Long-poll Telegram for incoming commands."""
    offset = None
    print("Polling… commands: /update /nfl /fantasy /draft  |  anything else → NFL agent")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset

            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params,
                timeout=35,
            )
            updates = resp.json().get("result", [])

            for update in updates:
                offset      = update["update_id"] + 1
                msg         = update.get("message", {})
                chat_id     = str(msg.get("chat", {}).get("id", ""))
                raw_text    = msg.get("text", "").strip()
                cmd         = raw_text.lower().split()[0] if raw_text else ""

                if not raw_text:
                    continue

                # Commands only for the owner
                if cmd in ("/update", "/nfl") and chat_id == TELEGRAM_CHAT_ID:
                    send_typing(chat_id)
                    try:    run_digest()
                    except Exception as e: send_telegram(f"<b>Error:</b> {e}")

                elif cmd == "/fantasy" and chat_id == TELEGRAM_CHAT_ID:
                    send_typing(chat_id)
                    try:    run_fantasy_digest()
                    except Exception as e: send_telegram(f"<b>Error:</b> {e}")

                elif cmd == "/draft" and chat_id == TELEGRAM_CHAT_ID:
                    send_typing(chat_id)
                    try:    run_draft_digest()
                    except Exception as e: send_telegram(f"<b>Error:</b> {e}")

                elif not cmd.startswith("/"):
                    # Step 1: onboarding (new users)
                    if handle_onboarding(chat_id, raw_text):
                        continue

                    send_typing(chat_id)

                    # Step 2: direct fantasy roster command?
                    try:
                        if handle_fantasy_command(chat_id, raw_text):
                            continue
                    except Exception as e:
                        print(f"[Fantasy command error] {e}")

                    # Step 3: general NFL Q&A with BILL
                    try:    run_agent(chat_id, raw_text)
                    except Exception as e: send_telegram(f"<b>Error:</b> {e}", chat_id)

        except Exception as e:
            print(f"[WARN] Poll error: {e} — retrying in 5s")
            time.sleep(5)


# ── Entry point ───────────────────────────────────────────────────────────────
def _run_all_agents(method: str):
    """Run a FantasyAgent method for every registered user."""
    for cid, agent in list(fantasy_agents.items()):
        try:
            getattr(agent, method)()
        except Exception as e:
            print(f"[Fantasy:{cid}] {method} error: {e}")


if __name__ == "__main__":
    # Restore previously onboarded users
    _load_users()

    scheduler = BackgroundScheduler(timezone=EASTERN)

    # ── NFL digest (owner only) ──────────────────────────────────────────────
    scheduler.add_job(run_digest, "cron", hour=0, minute=0)

    # ── Fantasy agent (all registered users) ────────────────────────────────
    scheduler.add_job(lambda: _run_all_agents("optimize_lineup"),       "cron", hour=7, minute=0)
    scheduler.add_job(lambda: _run_all_agents("check_waivers"),         "cron", day_of_week="tue", hour=9, minute=0)
    scheduler.add_job(lambda: _run_all_agents("optimize_lineup"),       "cron", day_of_week="wed", hour=9, minute=0)
    scheduler.add_job(lambda: _run_all_agents("optimize_lineup"),       "cron", day_of_week="sun", hour=11, minute=30)
    scheduler.add_job(lambda: _run_all_agents("weekly_recap"),          "cron", day_of_week="tue", hour=9, minute=30)
    scheduler.add_job(lambda: _run_all_agents("notable_performances"),  "cron", day_of_week="sun", hour=21, minute=0)
    scheduler.add_job(lambda: _run_all_agents("notable_performances"),  "cron", day_of_week="mon", hour=23, minute=30)
    scheduler.add_job(lambda: _run_all_agents("check_trades"),          "interval", hours=4)

    scheduler.start()
    print(f"Ready. {len(fantasy_agents)} fantasy agent(s) loaded.")
    poll()
