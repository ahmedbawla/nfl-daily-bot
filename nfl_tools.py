"""
NFL Tools — Data fetching functions and Claude tool schemas for the NFL agent.
All functions return plain dicts suitable for JSON serialization.
"""

import os
import json
import requests
import polars as pl
import nflreadpy as nfl
import pytz

HEADERS   = {"User-Agent": "NFLDailyBot/1.0 (Telegram digest bot)"}
EASTERN   = pytz.timezone("America/New_York")
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")


# ── Web Search ────────────────────────────────────────────────────────────────
def web_search(query: str) -> dict:
    if not TAVILY_KEY:
        return {"error": "TAVILY_API_KEY not set", "results": []}
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key":        TAVILY_KEY,
                "query":          query,
                "search_depth":   "basic",
                "max_results":    5,
                "include_answer": True,   # concise LLM-ready direct answer
            },
            timeout=15,
        )
        resp.raise_for_status()
        data    = resp.json()
        results = [
            {"title": r.get("title", ""), "content": r.get("content", "")[:300], "url": r.get("url", "")}
            for r in data.get("results", [])
        ]
        return {
            "source":        "Tavily Search",
            "query":         query,
            "direct_answer": data.get("answer", ""),
            "results":       results,
        }
    except Exception as e:
        return {"error": str(e), "results": []}


# ── ESPN APIs ─────────────────────────────────────────────────────────────────
def get_nfl_scores(date: str = None) -> dict:
    """date: YYYY-MM-DD. Omit for today."""
    from datetime import datetime
    if not date:
        date = datetime.now(EASTERN).strftime("%Y-%m-%d")
    date_fmt = date.replace("-", "")
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"dates": date_fmt}, headers=HEADERS, timeout=10,
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
                "home": home["team"]["displayName"],
                "away": away["team"]["displayName"],
                "home_score": home.get("score", "—"),
                "away_score": away.get("score", "—"),
                "status": st.get("description", "Scheduled"),
                "completed": st.get("completed", False),
            })
        return {"source": "ESPN", "date": date, "games": games}
    except Exception as e:
        return {"error": str(e), "games": []}


def get_nfl_news(topic: str = None, limit: int = 10) -> dict:
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
            params={"limit": min(limit, 20)}, headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        articles = []
        for a in resp.json().get("articles", []):
            headline    = a.get("headline", "").strip()
            description = a.get("description", "").strip()
            if not headline:
                continue
            if topic and topic.lower() not in headline.lower() and topic.lower() not in description.lower():
                continue
            articles.append({
                "headline": headline,
                "description": description[:200] if description else "",
                "published": a.get("published", ""),
            })
        return {"source": "ESPN", "articles": articles}
    except Exception as e:
        return {"error": str(e), "articles": []}


def get_nfl_injuries(team: str = None, position: str = None) -> dict:
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        injuries = []
        for tb in resp.json().get("injuries", []):
            team_name = tb.get("team", {}).get("displayName", "")
            if team and team.lower() not in team_name.lower():
                continue
            for p in tb.get("injuries", []):
                athlete = p.get("athlete", {})
                name    = athlete.get("displayName", "")
                pos     = athlete.get("position", {}).get("abbreviation", "")
                if position and position.upper() != pos.upper():
                    continue
                status  = p.get("status", "")
                details = p.get("details", {})
                if name and status:
                    injuries.append({
                        "name": name, "position": pos, "team": team_name,
                        "status": status,
                        "injury": f"{details.get('type', '')} {details.get('side', '')}".strip(),
                        "fantasy_outlook": details.get("fantasyStatus", {}).get("description", ""),
                    })
        return {"source": "ESPN", "injuries": injuries}
    except Exception as e:
        return {"error": str(e), "injuries": []}


def get_nfl_standings(season: int = None, conference: str = None) -> dict:
    try:
        params = {"season": season} if season else {}
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/standings",
            params=params, headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        standings = []
        for conf in resp.json().get("children", []):
            conf_name = conf.get("name", "")
            if conference and conference.upper() not in conf_name.upper():
                continue
            for div in conf.get("children", []):
                div_name = div.get("name", "")
                for entry in div.get("standings", {}).get("entries", []):
                    team  = entry.get("team", {}).get("displayName", "")
                    stats = {s["name"]: s.get("displayValue", s.get("value", ""))
                             for s in entry.get("stats", [])}
                    standings.append({
                        "conference": conf_name, "division": div_name, "team": team,
                        "wins": stats.get("wins", ""), "losses": stats.get("losses", ""),
                        "pct": stats.get("winPercent", ""),
                        "home": stats.get("home", ""), "away": stats.get("away", ""),
                        "div": stats.get("vsDiv", ""), "streak": stats.get("streak", ""),
                    })
        return {"source": "ESPN", "standings": standings}
    except Exception as e:
        return {"error": str(e), "standings": []}


def get_nfl_schedule(team: str = None, week: int = None, season: int = None) -> dict:
    try:
        params = {}
        if season and week:
            params["dates"] = f"{season}W{week:02d}"
        elif season:
            params["season"] = season
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params=params, headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        games = []
        for event in resp.json().get("events", []):
            comp  = event["competitions"][0]
            teams = comp["competitors"]
            home  = next(t for t in teams if t["homeAway"] == "home")
            away  = next(t for t in teams if t["homeAway"] == "away")
            home_name = home["team"]["displayName"]
            away_name = away["team"]["displayName"]
            if team and team.lower() not in home_name.lower() and team.lower() not in away_name.lower():
                continue
            games.append({
                "date": event.get("date", ""),
                "home": home_name, "away": away_name,
                "status": comp["status"]["type"].get("description", ""),
                "venue": comp.get("venue", {}).get("fullName", ""),
            })
        return {"source": "ESPN", "games": games}
    except Exception as e:
        return {"error": str(e), "games": []}


def get_nfl_transactions(team: str = None) -> dict:
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/transactions",
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        items = []
        for t in resp.json().get("transactions", [])[:40]:
            team_name = t.get("team", {}).get("displayName", "")
            if team and team.lower() not in team_name.lower():
                continue
            items.append({
                "team": team_name,
                "description": t.get("description", ""),
                "date": t.get("date", ""),
                "type": t.get("type", ""),
            })
        return {"source": "ESPN", "transactions": items}
    except Exception as e:
        return {"error": str(e), "transactions": []}


# ── nflreadpy helpers ─────────────────────────────────────────────────────────
def _find_player(df: pl.DataFrame, name: str) -> pl.DataFrame:
    exact = df.filter(pl.col("player_display_name") == name)
    if not exact.is_empty():
        return exact
    lower = name.lower().replace("'", ".")
    matches = df.filter(pl.col("player_display_name").str.to_lowercase().str.contains(lower))
    if not matches.is_empty():
        return matches
    parts = name.split()
    if len(parts) > 1:
        last = parts[-1].lower()
        matches = df.filter(pl.col("player_display_name").str.to_lowercase().str.contains(last))
        if not matches.is_empty():
            return matches
    return pl.DataFrame()


def _clean(row: dict) -> dict:
    """Remove NaN values from a dict."""
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in row.items()}


# ── nflreadpy Stats ───────────────────────────────────────────────────────────
def get_player_stats(player_name: str, season: int = None, stat_type: str = "all") -> dict:
    try:
        s     = season or nfl.get_current_season()
        stats = nfl.load_player_stats([s], summary_level="reg")
        found = _find_player(stats, player_name)
        if found.is_empty():
            return {"error": f"Player '{player_name}' not found in {s} data."}
        if len(found) > 1:
            found = found.sort("fantasy_points_ppr", descending=True).head(1)
        row = _clean(found.to_pandas().iloc[0].to_dict())
        games    = row.get("games") or 1
        carries  = row.get("carries") or 0
        attempts = row.get("attempts") or 0
        row["fppg"]     = round((row.get("fantasy_points_ppr") or 0) / games, 2)
        row["ypc"]      = round((row.get("rushing_yards") or 0) / carries, 2) if carries else 0
        row["comp_pct"] = round((row.get("completions") or 0) / attempts * 100, 1) if attempts else 0
        return {"source": f"nflreadpy/{s}", "season": s, "player": row}
    except Exception as e:
        return {"error": str(e)}


def compare_players(player1: str, player2: str, season: int = None) -> dict:
    r1 = get_player_stats(player1, season)
    r2 = get_player_stats(player2, season)
    if "error" in r1:
        return {"error": f"Player 1: {r1['error']}"}
    if "error" in r2:
        return {"error": f"Player 2: {r2['error']}"}
    s      = season or nfl.get_current_season()
    weekly = nfl.load_player_stats([s], summary_level="week")

    def weekly_summary(name: str) -> dict:
        w = weekly.filter(pl.col("player_display_name") == name).sort("week")
        if w.is_empty():
            return {}
        fps   = [float(x) for x in w["fantasy_points_ppr"].to_list()]
        games = len(fps)
        std   = float(w["fantasy_points_ppr"].std() or 0)
        return {
            "weekly_points":    fps,
            "boom_pct":         round(sum(1 for x in fps if x >= 20) / games * 100, 1),
            "bust_pct":         round(sum(1 for x in fps if x < 8)  / games * 100, 1),
            "consistency_std":  round(std, 2),
        }

    p1 = r1["player"]
    p2 = r2["player"]
    return {
        "source":  f"nflreadpy/{s}",
        "season":  s,
        "player1": {**p1, "weekly": weekly_summary(p1.get("player_display_name", player1))},
        "player2": {**p2, "weekly": weekly_summary(p2.get("player_display_name", player2))},
    }


def get_next_gen_stats(player_name: str, season: int = None, stat_type: str = "passing") -> dict:
    try:
        s   = season or nfl.get_current_season()
        ngs = nfl.load_nextgen_stats([s], stat_type=stat_type)
        found = _find_player(ngs, player_name)
        if found.is_empty():
            return {"error": f"No NGS '{stat_type}' data for '{player_name}' in {s}."}
        row = _clean(found.sort("week" if "week" in found.columns else "player_display_name",
                                descending=True).head(1).to_pandas().iloc[0].to_dict())
        return {"source": f"nflreadpy/NGS/{s}", "season": s, "stat_type": stat_type, "player": row}
    except Exception as e:
        return {"error": str(e)}


def get_sleeper_candidates(position: str = None, weeks: int = 4, season: int = None) -> dict:
    try:
        s            = season or nfl.get_current_season()
        weekly_stats = nfl.load_player_stats([s], summary_level="week")
        try:
            snaps = nfl.load_snap_counts([s])
        except Exception:
            snaps = None
        schedule     = nfl.load_schedules([s])

        completed = schedule.filter(
            (pl.col("season") == s) & (pl.col("game_type") == "REG") & (pl.col("result").is_not_null())
        )
        if completed.is_empty():
            return {"error": f"No completed games found for {s}."}
        max_week    = int(completed["week"].max())
        start_week  = max(1, max_week - weeks + 1)
        prior_end   = max(1, start_week - 1)
        prior_start = max(1, prior_end - weeks + 1)

        positions = [p.strip().upper() for p in position.split(",")] if position else None

        def filter_pos(df):
            return df.filter(pl.col("position").is_in(positions)) if positions else df

        recent = filter_pos(weekly_stats.filter(
            (pl.col("week") >= start_week) & (pl.col("week") <= max_week) & (pl.col("season_type") == "REG")
        ))
        prior  = filter_pos(weekly_stats.filter(
            (pl.col("week") >= prior_start) & (pl.col("week") <= prior_end) & (pl.col("season_type") == "REG")
        ))

        team_col = "recent_team" if "recent_team" in recent.columns else "team"
        recent_agg = recent.group_by(["player_id", "player_display_name", "position", team_col]).agg([
            pl.col("fantasy_points_ppr").mean().alias("recent_fppg"),
            pl.col("fantasy_points_ppr").count().alias("recent_games"),
            pl.col("targets").mean().alias("recent_tpg"),
            pl.col("carries").mean().alias("recent_cpg"),
            pl.col("target_share").mean().alias("recent_tgt_share"),
            pl.col("wopr").mean().alias("recent_wopr"),
        ])
        if team_col != "recent_team":
            recent_agg = recent_agg.rename({team_col: "recent_team"})

        prior_agg = prior.group_by("player_id").agg([
            pl.col("fantasy_points_ppr").mean().alias("prior_fppg"),
            pl.col("targets").mean().alias("prior_tpg"),
            pl.col("carries").mean().alias("prior_cpg"),
            pl.col("target_share").mean().alias("prior_tgt_share"),
        ])

        combined = (
            recent_agg.join(prior_agg, on="player_id", how="left")
            .with_columns([pl.col(c).fill_null(0) for c in ["prior_fppg", "prior_tpg", "prior_cpg", "prior_tgt_share"]])
            .with_columns([
                (pl.col("recent_fppg") - pl.col("prior_fppg")).alias("fp_delta"),
                ((pl.col("recent_tpg") + pl.col("recent_cpg")) - (pl.col("prior_tpg") + pl.col("prior_cpg"))).alias("opp_delta"),
                (pl.col("recent_tgt_share") - pl.col("prior_tgt_share")).alias("tgt_share_delta"),
            ])
            .with_columns((
                pl.col("fp_delta") * 3.0
                + pl.col("opp_delta") * 2.0
                + pl.col("tgt_share_delta") * 50.0
                + pl.col("recent_wopr").fill_null(0) * 10.0
                + pl.col("recent_fppg") * 0.5
            ).alias("sleeper_score"))
            .filter((pl.col("recent_games") >= 2) & (pl.col("sleeper_score") > 0))
            .sort("sleeper_score", descending=True)
            .head(15)
        )

        candidates = [
            {
                "player":       str(row["player_display_name"]),
                "position":     str(row["position"]),
                "team":         str(row["recent_team"]),
                "recent_fppg":  round(float(row["recent_fppg"]), 1),
                "fp_delta":     round(float(row["fp_delta"]), 1),
                "target_share": round(float(row["recent_tgt_share"] or 0) * 100, 1),
                "wopr":         round(float(row["recent_wopr"] or 0), 2),
                "sleeper_score":round(float(row["sleeper_score"]), 1),
            }
            for row in combined.to_pandas().itertuples()
        ]
        return {"source": f"nflreadpy/{s}", "season": s, "weeks_analyzed": weeks, "candidates": candidates}
    except Exception as e:
        return {"error": str(e)}


def get_referee_analytics(referee_name: str = None, season: int = None) -> dict:
    """NOTE: loads full PBP data (~800 MB). May take 30-60 s on cold start."""
    try:
        s        = season or nfl.get_current_season()
        schedule = nfl.load_schedules([s])
        pbp      = nfl.load_pbp([s])

        games = schedule.filter(
            (pl.col("game_type") == "REG") & (pl.col("result").is_not_null()) &
            (pl.col("referee").is_not_null()) & (pl.col("referee") != "")
        ).select(["game_id", "season", "week", "referee", "home_team", "away_team",
                  "home_score", "away_score", "total", "total_line", "spread_line", "result"])

        pen = pbp.filter(pl.col("penalty") == 1)
        total_pen = pen.group_by("game_id").agg(
            pl.col("penalty").sum().alias("total_penalties"),
            pl.col("penalty_yards").sum().alias("total_penalty_yards"),
        )
        home_pen = pen.filter(pl.col("penalty_team") == pl.col("home_team")).group_by("game_id").agg(
            pl.col("penalty").sum().alias("home_penalties"),
        )
        away_pen = pen.filter(pl.col("penalty_team") == pl.col("away_team")).group_by("game_id").agg(
            pl.col("penalty").sum().alias("away_penalties"),
        )

        ref_games = (
            games.join(total_pen, on="game_id", how="left")
                 .join(home_pen,  on="game_id", how="left")
                 .join(away_pen,  on="game_id", how="left")
            .with_columns([pl.col(c).fill_null(0) for c in
                           ["total_penalties", "total_penalty_yards", "home_penalties", "away_penalties"]])
            .with_columns([
                (pl.col("total") > pl.col("total_line")).cast(pl.Int32).alias("went_over"),
                (pl.col("result") > pl.col("spread_line")).cast(pl.Int32).alias("home_covered"),
                pl.col("total").alias("game_total"),
            ])
        )

        if referee_name:
            sub = ref_games.filter(pl.col("referee").str.to_lowercase().str.contains(referee_name.lower()))
            if sub.is_empty():
                avail = ref_games["referee"].unique().sort().to_list()
                return {"error": f"Referee '{referee_name}' not found.", "available": avail[:20]}
            rows = sub.to_pandas()
            return {
                "source": f"nflreadpy/{s}", "referee": str(rows["referee"].iloc[0]),
                "games": len(rows),
                "avg_penalties_per_game": round(float(rows["total_penalties"].mean()), 2),
                "avg_penalty_yards":      round(float(rows["total_penalty_yards"].mean()), 1),
                "avg_points_per_game":    round(float(rows["game_total"].mean()), 1),
                "over_rate_pct":          round(float(rows["went_over"].mean() * 100), 1),
                "home_cover_pct":         round(float(rows["home_covered"].mean() * 100), 1),
                "home_penalty_bias":      round(float((rows["home_penalties"] - rows["away_penalties"]).mean()), 2),
            }

        summary = (
            ref_games.group_by("referee").agg([
                pl.col("game_id").count().alias("games"),
                pl.col("total_penalties").mean().alias("avg_pen"),
                pl.col("total_penalty_yards").mean().alias("avg_pen_yds"),
                pl.col("game_total").mean().alias("avg_pts"),
                (pl.col("went_over").mean() * 100).alias("over_pct"),
                (pl.col("home_covered").mean() * 100).alias("home_cover_pct"),
                (pl.col("home_penalties") - pl.col("away_penalties")).mean().alias("home_bias"),
            ])
            .filter(pl.col("games") >= 5)
            .sort("avg_pen", descending=True)
        )
        refs = [
            {
                "referee": r.referee, "games": r.games,
                "avg_penalties": round(float(r.avg_pen), 2),
                "avg_pen_yards": round(float(r.avg_pen_yds), 1),
                "avg_total_pts": round(float(r.avg_pts), 1),
                "over_pct":      round(float(r.over_pct), 1),
                "home_cover_pct":round(float(r.home_cover_pct), 1),
                "home_pen_bias": round(float(r.home_bias), 2),
            }
            for r in summary.to_pandas().itertuples()
        ]
        return {"source": f"nflreadpy/{s}", "season": s, "referees": refs}
    except Exception as e:
        return {"error": str(e)}


# ── Tool Schemas ──────────────────────────────────────────────────────────────
NFL_TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for NFL/football information. Use for: pre-1999 history, "
            "Super Bowl records, rule questions, coaching history, draft history, "
            "all-time records, or anything not covered by the structured tools. "
            "Returns a direct answer plus supporting sources."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Specific search query (e.g. 'Tom Brady career Super Bowl wins')"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_nfl_scores",
        "description": "Get NFL game scores for a specific date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD. Omit for today."}
            },
            "required": [],
        },
    },
    {
        "name": "get_nfl_news",
        "description": "Get latest NFL news. Optionally filter by topic, player, or team name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Optional filter (player name, team, topic)"},
                "limit": {"type": "integer", "description": "Number of articles (default 10)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_nfl_injuries",
        "description": "Get current NFL injury report. Optionally filter by team or position.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team":     {"type": "string", "description": "Optional team name"},
                "position": {"type": "string", "description": "Optional position (QB, RB, WR, TE, etc.)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_nfl_standings",
        "description": "Get NFL standings for current or past season.",
        "input_schema": {
            "type": "object",
            "properties": {
                "season":     {"type": "integer", "description": "Season year. Omit for current."},
                "conference": {"type": "string",  "description": "AFC or NFC. Omit for both."},
            },
            "required": [],
        },
    },
    {
        "name": "get_nfl_schedule",
        "description": "Get NFL game schedule for a team, week, or season.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team":   {"type": "string",  "description": "Optional team name"},
                "week":   {"type": "integer", "description": "Optional week number"},
                "season": {"type": "integer", "description": "Optional season year"},
            },
            "required": [],
        },
    },
    {
        "name": "get_nfl_transactions",
        "description": "Get recent NFL transactions: signings, cuts, trades, IR moves.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Optional team name filter"}
            },
            "required": [],
        },
    },
    {
        "name": "get_player_stats",
        "description": (
            "Get detailed season stats for any NFL player (1999–present). "
            "ALWAYS call this before stating specific stats about a player."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {"type": "string",  "description": "Player full name (e.g. 'Patrick Mahomes')"},
                "season":      {"type": "integer", "description": "Season year. Omit for current."},
                "stat_type":   {"type": "string",  "description": "passing, rushing, receiving, or all"},
            },
            "required": ["player_name"],
        },
    },
    {
        "name": "compare_players",
        "description": "Compare two NFL players side by side: stats, weekly trends, boom/bust rates, advanced metrics.",
        "input_schema": {
            "type": "object",
            "properties": {
                "player1": {"type": "string",  "description": "First player full name"},
                "player2": {"type": "string",  "description": "Second player full name"},
                "season":  {"type": "integer", "description": "Season year. Omit for current."},
            },
            "required": ["player1", "player2"],
        },
    },
    {
        "name": "get_next_gen_stats",
        "description": "Get advanced Next Gen Stats: air yards, separation, CPOE (QB), route efficiency (WR/TE), breakaway rate (RB).",
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {"type": "string",  "description": "Player full name"},
                "season":      {"type": "integer", "description": "Season year. Omit for current."},
                "stat_type":   {"type": "string",  "description": "passing, rushing, or receiving"},
            },
            "required": ["player_name"],
        },
    },
    {
        "name": "get_sleeper_candidates",
        "description": "Get top fantasy football sleeper/breakout candidates based on usage trends, target share, and efficiency.",
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {"type": "string",  "description": "Position filter: QB, RB, WR, TE (or comma-separated). Omit for all."},
                "weeks":    {"type": "integer", "description": "Lookback window in weeks (default 4)"},
                "season":   {"type": "integer", "description": "Season year. Omit for current."},
            },
            "required": [],
        },
    },
    {
        "name": "get_referee_analytics",
        "description": (
            "Get NFL referee penalty rates, over/under tendencies, home cover rates, and penalty bias. "
            "WARNING: loads large PBP data — may take up to 60s on first call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "referee_name": {"type": "string",  "description": "Specific referee name. Omit for all referees."},
                "season":       {"type": "integer", "description": "Season year. Omit for current."},
            },
            "required": [],
        },
    },
]

TOOL_DISPATCH = {
    "web_search":             web_search,
    "get_nfl_scores":         get_nfl_scores,
    "get_nfl_news":           get_nfl_news,
    "get_nfl_injuries":       get_nfl_injuries,
    "get_nfl_standings":      get_nfl_standings,
    "get_nfl_schedule":       get_nfl_schedule,
    "get_nfl_transactions":   get_nfl_transactions,
    "get_player_stats":       get_player_stats,
    "compare_players":        compare_players,
    "get_next_gen_stats":     get_next_gen_stats,
    "get_sleeper_candidates": get_sleeper_candidates,
    "get_referee_analytics":  get_referee_analytics,
}
