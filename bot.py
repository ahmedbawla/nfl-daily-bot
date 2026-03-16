"""
NFL Daily Telegram Bot
- Sends a daily digest at midnight EDT automatically
- Responds to /update in Telegram for an on-demand update
Sources: ESPN Scoreboard · ESPN News · Reddit r/nfl social buzz
"""

import os
import sys
import time
import requests
from anthropic import Anthropic
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

client  = Anthropic()
HEADERS = {"User-Agent": "NFLDailyBot/1.0 (Telegram digest bot)"}
EASTERN = pytz.timezone("America/New_York")


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


# ── Reddit r/nfl ──────────────────────────────────────────────────────────────
def get_reddit_buzz(limit: int = 15) -> list[str]:
    try:
        resp = requests.get(
            "https://www.reddit.com/r/nfl/top.json",
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
        print(f"[WARN] Reddit r/nfl: {e}")
        return []


# ── Build context ─────────────────────────────────────────────────────────────
def build_context(yesterday_games, upcoming_games, news, reddit_buzz, report_date) -> str:
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

    return "\n".join(lines)


# ── Claude ────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a sharp, knowledgeable NFL analyst writing a daily Telegram digest.
Your job: distill a lot of information into one tight, satisfying message.

RULES:
- ONE message only. No multi-part replies.
- Absolute max: 550 words / ~3,000 characters (Telegram limit is 4,096).
- Use Telegram HTML only: <b>bold</b>, <i>italic</i>. No markdown, no asterisks.
- Structure with clear emoji section headers so it's skimmable at a glance.
- Be selective — pick the 3-4 most interesting news items and 2-3 social highlights.
- If it's the off-season, lean hard into free agency, trades, draft, coaching moves.
- Write like a smart friend who follows the NFL obsessively, not a press release.
- No fluff, no filler. Every sentence should earn its place."""

def generate_update(context: str, report_date: str) -> str:
    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1100,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f"Here is today's NFL data for {report_date}:\n\n{context}\n\n"
            "Write the daily NFL Telegram digest now. Follow the system rules exactly."
        )}],
    )
    return resp.content[0].text.strip()


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    data = resp.json()
    if not data.get("ok"):
        print(f"[ERROR] Telegram: {data}", file=sys.stderr)
    else:
        print("✓ Digest sent.")


def send_typing():
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
        json={"chat_id": TELEGRAM_CHAT_ID, "action": "typing"},
        timeout=5,
    )


# ── Core digest job ───────────────────────────────────────────────────────────
def run_digest():
    now           = datetime.now(EASTERN)
    yesterday_fmt = (now - timedelta(days=1)).strftime("%Y%m%d")
    today_fmt     = now.strftime("%Y%m%d")
    report_date   = now.strftime("%B %-d, %Y")

    print(f"[{now.isoformat()}] Running NFL digest…")

    yesterday_games = get_nfl_scores(yesterday_fmt)
    upcoming_games  = get_nfl_scores(today_fmt)
    news            = get_nfl_news(limit=10)
    reddit_buzz     = get_reddit_buzz(limit=15)

    context = build_context(yesterday_games, upcoming_games, news, reddit_buzz, report_date)
    digest  = generate_update(context, report_date)

    print(digest)
    send_telegram(digest)


# ── Telegram polling ──────────────────────────────────────────────────────────
def poll():
    """Long-poll Telegram for incoming messages. Handles /update and /nfl commands."""
    offset = None
    print("Polling for Telegram commands…")

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
                offset = update["update_id"] + 1
                msg     = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip().lower().split()[0] if msg.get("text") else ""

                # Only respond to the configured chat
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text in ("/update", "/nfl"):
                    print(f"[CMD] {text} received — running digest…")
                    send_typing()
                    try:
                        run_digest()
                    except Exception as e:
                        send_telegram(f"<b>Error running update:</b> {e}")

        except Exception as e:
            print(f"[WARN] Poll error: {e} — retrying in 5s")
            time.sleep(5)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Schedule nightly digest at midnight Eastern (auto-handles EST/EDT)
    scheduler = BackgroundScheduler(timezone=EASTERN)
    scheduler.add_job(run_digest, "cron", hour=0, minute=0)
    scheduler.start()
    print("Scheduler started — nightly digest at 12:00 AM ET.")

    # Block on Telegram polling
    poll()
