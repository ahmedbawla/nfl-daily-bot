"""
NFL Daily Telegram Bot
Runs once a day (Railway cron @ 1:45 AM UTC).
Sources: ESPN Scoreboard · ESPN News · Reddit r/nfl social buzz
"""

import os
import sys
import requests
from anthropic import Anthropic
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

client = Anthropic()   # reads ANTHROPIC_API_KEY automatically

HEADERS = {"User-Agent": "NFLDailyBot/1.0 (Telegram digest bot)"}


# ── ESPN Scoreboard ───────────────────────────────────────────────────────────
def get_nfl_scores(date_str: str) -> list[dict]:
    """
    date_str: YYYYMMDD  (ESPN format)
    Returns a list of game dicts with team names, scores, and status.
    """
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
                "home":      home["team"]["displayName"],
                "away":      away["team"]["displayName"],
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
    """Latest NFL headlines from ESPN's public API."""
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
            params={"limit": limit},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        out = []
        for a in articles:
            headline    = a.get("headline", "").strip()
            description = a.get("description", "").strip()
            if headline:
                entry = headline
                if description and description.lower() != headline.lower():
                    # trim description so it doesn't bloat context
                    desc_short = description[:120] + ("…" if len(description) > 120 else "")
                    entry += f" — {desc_short}"
                out.append(entry)
        return out
    except Exception as e:
        print(f"[WARN] ESPN news: {e}")
        return []


# ── Reddit r/nfl ──────────────────────────────────────────────────────────────
def get_reddit_buzz(limit: int = 15) -> list[str]:
    """
    Top posts from r/nfl (last 24 h) — gives real social/fan pulse.
    No API key needed; uses Reddit's public JSON endpoint.
    """
    try:
        resp = requests.get(
            "https://www.reddit.com/r/nfl/top.json",
            params={"t": "day", "limit": limit},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        posts = resp.json().get("data", {}).get("children", [])
        buzz = []
        for p in posts:
            d = p.get("data", {})
            title = d.get("title", "").strip()
            score = d.get("score", 0)
            flair = d.get("link_flair_text", "")
            # skip megathreads / mod posts unless very popular
            if not title or (flair and "megathread" in flair.lower() and score < 500):
                continue
            tag = f"[{flair}] " if flair else ""
            buzz.append(f"{tag}{title}  (↑{score:,})")
        return buzz[:10]
    except Exception as e:
        print(f"[WARN] Reddit r/nfl: {e}")
        return []


# ── Build context block ───────────────────────────────────────────────────────
def build_context(
    yesterday_games: list,
    upcoming_games:  list,
    news:            list[str],
    reddit_buzz:     list[str],
    report_date:     str,
) -> str:
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
    user_msg = (
        f"Here is today's NFL data for {report_date}:\n\n"
        f"{context}\n\n"
        "Write the daily NFL Telegram digest now. Follow the system rules exactly."
    )
    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1100,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text.strip()


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        # disable link previews so ESPN/Reddit URLs don't spawn giant embeds
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=10)
    data = resp.json()
    if not data.get("ok"):
        print(f"[ERROR] Telegram: {data}", file=sys.stderr)
        sys.exit(1)
    print("✓ Digest sent to Telegram.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now           = datetime.now(timezone.utc)
    yesterday_fmt = (now - timedelta(days=1)).strftime("%Y%m%d")   # ESPN: YYYYMMDD
    today_fmt     = now.strftime("%Y%m%d")
    report_date   = now.strftime("%B %-d, %Y")                      # e.g. "March 16, 2026"

    print(f"[{now.isoformat()}] Starting NFL daily digest…")

    yesterday_games = get_nfl_scores(yesterday_fmt)
    upcoming_games  = get_nfl_scores(today_fmt)
    news            = get_nfl_news(limit=10)
    reddit_buzz     = get_reddit_buzz(limit=15)

    print(f"  Scores (yesterday): {len(yesterday_games)}")
    print(f"  Scores (today):     {len(upcoming_games)}")
    print(f"  News items:         {len(news)}")
    print(f"  Reddit posts:       {len(reddit_buzz)}")

    context = build_context(yesterday_games, upcoming_games, news, reddit_buzz, report_date)

    print("\n--- Context ---")
    print(context)
    print("---------------\n")

    digest = generate_update(context, report_date)

    print("--- Digest ---")
    print(digest)
    print(f"\nChar count: {len(digest)}")
    print("--------------\n")

    send_telegram(digest)


if __name__ == "__main__":
    main()
