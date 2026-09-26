import csv
import asyncio
import html
import math
import os
import re
import sqlite3
import statistics
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
SPORTS = {"NFL": "americanfootball_nfl", "NCAAF": "americanfootball_ncaaf"}
KALSHI_API_URL = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_SPREAD_SERIES = {"NFL": "KXNFLSPREAD", "NCAAF": "KXNCAAFSPREAD"}
KALSHI_MARKET_SERIES = {
  ("NFL", "spread"): "KXNFLSPREAD",
  ("NFL", "total"): "KXNFLTOTAL",
  ("NFL", "moneyline"): "KXNFLGAME",
  ("NCAAF", "spread"): "KXNCAAFSPREAD",
  ("NCAAF", "total"): "KXNCAAFTOTAL",
  ("NCAAF", "moneyline"): "KXNCAAFGAME",
}
BLUECHIP_WEEK_URL = os.getenv("BLUECHIP_WEEK_URL", "https://bluechipanalytics.com/college-football/games/2026/week4/")
BLUECHIP_CACHE_SECONDS = int(os.getenv("BLUECHIP_CACHE_SECONDS", "1800"))
NFL_POWER_RATINGS_URL = os.getenv("NFL_POWER_RATINGS_URL", "https://stats.innerpulse.net/teams")
NFL_RATINGS_CACHE_SECONDS = int(os.getenv("NFL_RATINGS_CACHE_SECONDS", "1800"))
KALSHI_INCLUDE_UNMODELED = os.getenv("KALSHI_INCLUDE_UNMODELED", "true").lower() in {"1", "true", "yes"}
ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("MARKBETS_DB", ROOT / "data" / "markbets.sqlite3"))
PROJECTIONS_PATH = Path(os.getenv("MARKBETS_PROJECTIONS", ROOT / "data" / "projections.csv"))
TEAM_RATINGS_PATH = Path(os.getenv("MARKBETS_TEAM_RATINGS", ROOT.parent / "public" / "data" / "ratings.csv"))

app = FastAPI(title="MarkBets API")
BLUECHIP_CACHE: dict[str, Any] = {"expires_at": 0.0, "games": {}}
NFL_RATINGS_CACHE: dict[str, Any] = {"expires_at": 0.0, "ratings": {}}
TEAM_KEY_ALIASES = {
  "ari_cardinals": "arizona_cardinals",
  "arizona": "arizona_cardinals",
  "atl_falcons": "atlanta_falcons",
  "atlanta": "atlanta_falcons",
  "bal_ravens": "baltimore_ravens",
  "baltimore": "baltimore_ravens",
  "buf_bills": "buffalo_bills",
  "buffalo": "buffalo_bills",
  "car_panthers": "carolina_panthers",
  "carolina": "carolina_panthers",
  "chi_bears": "chicago_bears",
  "chicago": "chicago_bears",
  "cin_bengals": "cincinnati_bengals",
  "cincinnati": "cincinnati_bengals",
  "cle_browns": "cleveland_browns",
  "cleveland": "cleveland_browns",
  "dal_cowboys": "dallas_cowboys",
  "dallas": "dallas_cowboys",
  "den_broncos": "denver_broncos",
  "denver": "denver_broncos",
  "det_lions": "detroit_lions",
  "detroit": "detroit_lions",
  "gb_packers": "green_bay_packers",
  "green_bay": "green_bay_packers",
  "hou_texans": "houston_texans",
  "houston": "houston_texans",
  "ind_colts": "indianapolis_colts",
  "indianapolis": "indianapolis_colts",
  "jac_jaguars": "jacksonville_jaguars",
  "jax_jaguars": "jacksonville_jaguars",
  "jacksonville": "jacksonville_jaguars",
  "kc_chiefs": "kansas_city_chiefs",
  "kansas_city": "kansas_city_chiefs",
  "la_chargers": "los_angeles_chargers",
  "lac_chargers": "los_angeles_chargers",
  "la_rams": "los_angeles_rams",
  "lar_rams": "los_angeles_rams",
  "los_angeles": "los_angeles_chargers",
  "los_angeles_c": "los_angeles_chargers",
  "los_angeles_r": "los_angeles_rams",
  "lv_raiders": "las_vegas_raiders",
  "las_vegas": "las_vegas_raiders",
  "mia_dolphins": "miami_dolphins",
  "miami": "miami_dolphins",
  "min_vikings": "minnesota_vikings",
  "minnesota": "minnesota_vikings",
  "ne_patriots": "new_england_patriots",
  "new_england": "new_england_patriots",
  "no_saints": "new_orleans_saints",
  "new_orleans": "new_orleans_saints",
  "ny_giants": "new_york_giants",
  "ny_jets": "new_york_jets",
  "new_york_g": "new_york_giants",
  "new_york_j": "new_york_jets",
  "nyg_giants": "new_york_giants",
  "nyj_jets": "new_york_jets",
  "phi_eagles": "philadelphia_eagles",
  "philadelphia": "philadelphia_eagles",
  "pit_steelers": "pittsburgh_steelers",
  "pittsburgh": "pittsburgh_steelers",
  "sea_seahawks": "seattle_seahawks",
  "seattle": "seattle_seahawks",
  "sf_49ers": "san_francisco_49ers",
  "san_francisco": "san_francisco_49ers",
  "tb_buccaneers": "tampa_bay_buccaneers",
  "tampa_bay": "tampa_bay_buccaneers",
  "ten_titans": "tennessee_titans",
  "tennessee": "tennessee_titans",
  "was_commanders": "washington_commanders",
  "washington": "washington_commanders",
}


def now_iso() -> str:
  return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_id(sport: str, commence_time: str, away_team: str, home_team: str) -> str:
  date = commence_time[:10].replace("-", "_")
  slug = "_".join([sport.lower(), date, slugify(away_team), slugify(home_team)])
  return slug


def slugify(value: str) -> str:
  return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def matchup_key(away_team: str, home_team: str) -> str:
  return "|".join(sorted([team_key(away_team), team_key(home_team)]))


def team_key(value: str) -> str:
  normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
  normalized = re.sub(r"\bst[.]?\b", "state", normalized, flags=re.I)
  normalized = normalized.replace("&", " and ")
  key = slugify(normalized)
  return TEAM_KEY_ALIASES.get(key, key)


def connect() -> sqlite3.Connection:
  DB_PATH.parent.mkdir(parents=True, exist_ok=True)
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  conn.execute(
    """
    CREATE TABLE IF NOT EXISTS odds_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game_id TEXT NOT NULL,
      provider_event_id TEXT NOT NULL,
      sport TEXT NOT NULL,
      commence_time TEXT NOT NULL,
      home_team TEXT NOT NULL,
      away_team TEXT NOT NULL,
      book TEXT NOT NULL,
      market TEXT NOT NULL,
      side TEXT NOT NULL,
      line REAL,
      price INTEGER,
      captured_at TEXT NOT NULL
    )
    """
  )
  conn.execute("CREATE INDEX IF NOT EXISTS idx_odds_game ON odds_snapshots(game_id, captured_at)")
  conn.commit()
  return conn


def american_to_probability(odds: int | None) -> float | None:
  if odds is None:
    return None
  return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def dollars_to_float(value: Any) -> float | None:
  if value in (None, ""):
    return None
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def fp_to_float(value: Any) -> float:
  parsed = dollars_to_float(value)
  return parsed if parsed is not None else 0


def clamp(value: float, minimum: float, maximum: float) -> float:
  return max(minimum, min(maximum, value))


def extract_matchup(market: dict[str, Any]) -> tuple[str, str]:
  rules = market.get("rules_primary") or ""
  match = re.search(r"in the (.+?) vs (.+?) (?:college football|Pro Football)", rules)
  if match:
    return match.group(1), match.group(2)
  match = re.search(r"wins the (.+?) vs (.+?) (?:college football|Pro Football) game", rules)
  if match:
    return match.group(1), match.group(2)
  title = (market.get("title") or "").replace("?", "")
  team = title.split(" wins", 1)[0] or market.get("yes_sub_title") or "Kalshi"
  return team, "Market"


def clean_text(value: str | None) -> str | None:
  if value is None:
    return None
  return html.unescape(value).replace("\ufffd", "°").strip()


def signed_line(team: str | None, spread: float | None) -> str | None:
  if not team or spread is None:
    return None
  return f"{team} {spread:+.1f}".replace("+", "")


def plain_text(page: str) -> str:
  text = re.sub(r"<[^>]+>", " ", page)
  return clean_text(re.sub(r"\s+", " ", text)) or ""


def betting_value(page: str, label: str) -> str | None:
  match = re.search(
    rf'<dt[^>]*class="bb-label"[^>]*>{re.escape(label)}</dt>\s*<dd[^>]*class="bb-value"[^>]*>(.*?)</dd>',
    page,
    re.I | re.S,
  )
  if not match:
    return None
  return clean_text(re.sub(r"<[^>]+>", " ", match.group(1)).replace("&ndash;", "-"))


def parse_team_line(value: str | None) -> tuple[str | None, float | None]:
  if not value:
    return None, None
  match = re.search(r"(.+?)\s+([+-]?\d+(?:\.\d+)?)$", clean_text(value) or "")
  if not match:
    return None, None
  return clean_text(match.group(1)), float(match.group(2))


def spread_gap_and_edge(away_team: str, home_team: str, market_team: str | None, market_spread: float | None, model_team: str | None, model_spread: float | None) -> tuple[float | None, str | None]:
  if not market_team or market_spread is None or not model_team or model_spread is None:
    return None, None
  market_margin = -market_spread
  model_margin_for_market_team = -model_spread if team_key(model_team) == team_key(market_team) else model_spread
  gap = round(abs(model_margin_for_market_team - market_margin), 1)
  if abs(model_margin_for_market_team - market_margin) < 0.05:
    return gap, None
  if model_margin_for_market_team > market_margin:
    return gap, market_team
  return gap, opponent_team(away_team, home_team, market_team)


def opponent_team(away_team: str, home_team: str, team: str | None) -> str | None:
  if not team:
    return None
  key = team_key(team)
  if key == team_key(away_team):
    return home_team
  if key == team_key(home_team):
    return away_team
  return None


def contract_side_team(market: dict[str, Any]) -> str | None:
  label = clean_text(market.get("yes_sub_title")) or clean_text(market.get("title")) or ""
  match = re.match(r"(.+?) wins\b", label.replace("?", ""), re.I)
  if match:
    return clean_text(match.group(1))
  return label or None


def contract_model_gap(bet_type: str, market: dict[str, Any], bluechip: dict[str, Any] | None, impact: dict[str, Any] | None) -> float | None:
  threshold = dollars_to_float(market.get("floor_strike"))
  if threshold is None and bet_type != "moneyline":
    return None
  if bet_type == "spread" and bluechip:
    model_spread = bluechip.get("model_spread")
    model_team = bluechip.get("model_team")
    side_team = contract_side_team(market)
    if model_spread is None or not model_team or not side_team:
      return bluechip.get("gap")
    model_margin = abs(float(model_spread))
    side_margin = model_margin if team_key(side_team) == team_key(model_team) else -model_margin
    return round(side_margin - threshold, 1)
  if bet_type == "total" and impact and impact.get("adjusted_total") is not None:
    adjusted_total = float(impact["adjusted_total"])
    label = f"{market.get('yes_sub_title') or ''} {market.get('title') or ''}".lower()
    return round(threshold - adjusted_total, 1) if "under" in label else round(adjusted_total - threshold, 1)
  if bet_type == "moneyline" and bluechip:
    model_spread = bluechip.get("model_spread")
    model_team = bluechip.get("model_team")
    side_team = contract_side_team(market)
    if model_spread is None or not model_team or not side_team:
      return None
    model_margin = abs(float(model_spread))
    return round(model_margin if team_key(side_team) == team_key(model_team) else -model_margin, 1)
  return None


def board_group_key(row: dict[str, Any]) -> str:
  return "|".join(
    [
      row.get("sport") or "",
      matchup_key(row.get("away_team") or "", row.get("home_team") or ""),
      row.get("bet_type") or "spread",
    ]
  )


def best_board_row(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
  if current is None:
    return candidate
  current_rating = current.get("rating") or {}
  candidate_rating = candidate.get("rating") or {}
  current_metrics = current.get("metrics") or {}
  candidate_metrics = candidate.get("metrics") or {}
  current_contract = current.get("contract") or {}
  candidate_contract = candidate.get("contract") or {}
  current_score = (
    current.get("edge_score") or 0,
    current_rating.get("probability") or 0,
    abs(current_metrics.get("model_market_gap") or 0),
    abs(current_contract.get("price_move") or 0),
  )
  candidate_score = (
    candidate.get("edge_score") or 0,
    candidate_rating.get("probability") or 0,
    abs(candidate_metrics.get("model_market_gap") or 0),
    abs(candidate_contract.get("price_move") or 0),
  )
  return candidate if candidate_score > current_score else current


def collapse_board_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  grouped: dict[str, dict[str, Any]] = {}
  for row in rows:
    key = board_group_key(row)
    grouped[key] = best_board_row(grouped.get(key), row)
  return sorted(
    grouped.values(),
    key=lambda row: (
      row.get("edge_score") or 0,
      (row.get("metrics") or {}).get("model_market_gap") or 0,
      abs((row.get("contract") or {}).get("price_move") or 0),
    ),
    reverse=True,
  )


def inferred_precipitation(condition: str | None) -> tuple[float, float]:
  text = (condition or "").lower()
  rain_pct = 0.0
  snow_in = 0.0
  if any(word in text for word in ["rain", "drizzle", "shower", "thunder"]):
    rain_pct = 60.0 if "patchy" in text or "nearby" in text else 80.0
  if "snow" in text or "sleet" in text or "blizzard" in text:
    snow_in = 1.0 if "light" in text or "patchy" in text else 2.0
  return rain_pct, snow_in


def weather_category(score: float) -> str:
  if score < 15:
    return "Minimal"
  if score < 30:
    return "Mild"
  if score < 50:
    return "Moderate"
  if score < 70:
    return "Severe"
  return "Extreme"


def weather_impact(bluechip: dict[str, Any] | None, baseline_total: float | None) -> dict[str, Any] | None:
  if not bluechip:
    return None
  weather = bluechip.get("weather") or {}
  condition = weather.get("condition")
  temp = dollars_to_float(weather.get("temperature_f"))
  wind = dollars_to_float(weather.get("wind_mph"))
  if temp is None and wind is None and not condition:
    return None

  temp_f = temp if temp is not None else 65.0
  wind_mph = max(wind if wind is not None else 0.0, 0.0)
  gust_mph = wind_mph
  rain_pct, snow_in = inferred_precipitation(condition)

  wind_component = clamp((wind_mph - 5) / 20, 0, 1) * 35
  gust_component = clamp((gust_mph - 10) / 30, 0, 1) * 15
  rain_component = clamp(rain_pct / 100, 0, 1) * 15
  snow_component = clamp(snow_in / 4, 0, 1) * 20
  cold_component = clamp((40 - temp_f) / 35, 0, 1) * 10
  score = round(clamp(wind_component + gust_component + rain_component + snow_component + cold_component, 0, 100), 1)

  total_adjustment = -(score / 100) * 8.0
  total_adjustment -= max(0, wind_mph - 12) * 0.10
  total_adjustment -= max(0, gust_mph - 25) * 0.04
  total_adjustment -= (rain_pct / 100) * 0.8
  total_adjustment -= min(snow_in, 4) * 0.35
  if temp_f < 25:
    total_adjustment -= 0.5

  adjusted_total = None if baseline_total is None else round(baseline_total + total_adjustment, 2)
  model_spread = bluechip.get("model_spread")
  adjusted_spread = None if model_spread is None else round(float(model_spread), 2)
  projected = None
  if adjusted_total is not None and adjusted_spread is not None:
    projected = {
      "team_a_points": round((adjusted_total + adjusted_spread) / 2, 2),
      "team_b_points": round((adjusted_total - adjusted_spread) / 2, 2),
    }

  return {
    "score": score,
    "category": weather_category(score),
    "wind_impact": round(wind_component + gust_component, 1),
    "precipitation_impact": round(rain_component + snow_component, 1),
    "temperature_impact": round(cold_component, 1),
    "spread_adjustment": 0.0,
    "total_adjustment": round(total_adjustment, 2),
    "adjusted_total": adjusted_total,
    "adjusted_spread": adjusted_spread,
    "projected_score": projected,
    "confidence": round(clamp(50 + score * 0.45, 50, 95), 1),
    "assumptions": {
      "rain_pct": rain_pct,
      "snow_in": snow_in,
      "gust_mph": gust_mph,
    },
  }


def projection_summary(away_team: str, home_team: str, bluechip: dict[str, Any] | None, impact: dict[str, Any] | None) -> dict[str, Any] | None:
  if not bluechip:
    return None
  model_team = bluechip.get("model_team")
  model_spread = dollars_to_float(bluechip.get("model_spread"))
  market_team = bluechip.get("market_team")
  market_spread = dollars_to_float(bluechip.get("market_spread"))
  book_total = dollars_to_float(bluechip.get("book_total"))
  projected_total = dollars_to_float((impact or {}).get("adjusted_total")) if impact else None
  if projected_total is None:
    projected_total = book_total

  model_winner = None
  model_margin = None
  if model_team and model_spread is not None:
    model_margin = abs(model_spread)
    model_winner = model_team if model_spread <= 0 else opponent_team(away_team, home_team, model_team)
  if not model_winner and model_team:
    model_winner = model_team

  projected_score = None
  if projected_total is not None and model_winner and model_margin is not None:
    home_margin = model_margin if team_key(model_winner) == team_key(home_team) else -model_margin
    home_points = (projected_total + home_margin) / 2
    away_points = (projected_total - home_margin) / 2
    projected_score = {
      "away_team": away_team,
      "away_points": round(away_points, 1),
      "home_team": home_team,
      "home_points": round(home_points, 1),
      "label": f"{away_team} {away_points:.1f}, {home_team} {home_points:.1f}",
    }

  total_edge = None if projected_total is None or book_total is None else round(projected_total - book_total, 1)
  total_lean = None
  if total_edge is not None:
    if total_edge >= 0.5:
      total_lean = "Over"
    elif total_edge <= -0.5:
      total_lean = "Under"
    else:
      total_lean = "No clear total edge"

  model_label = None if model_winner is None or model_margin is None else f"{model_winner} by {model_margin:.1f}"
  return {
    "model_winner": model_winner,
    "model_margin": None if model_margin is None else round(model_margin, 1),
    "model_label": model_label,
    "model_line": signed_line(model_team, model_spread),
    "market_line": signed_line(market_team, market_spread),
    "edge_team": bluechip.get("edge_team"),
    "spread_edge": bluechip.get("gap"),
    "book_total": book_total,
    "projected_total": projected_total,
    "total_edge": total_edge,
    "total_lean": total_lean,
    "projected_score": projected_score,
  }


def gap_confidence(model_gap: float | None) -> int:
  if model_gap is None:
    return 46
  if model_gap >= 4:
    return 88
  if model_gap >= 2.5:
    return 76
  if model_gap >= 1:
    return 64
  return 52


def data_quality_multiplier(score: int | float | None) -> float:
  if score is None:
    return 0.65
  if score >= 90:
    return 1.00
  if score >= 80:
    return 0.95
  if score >= 70:
    return 0.90
  if score >= 60:
    return 0.80
  return 0.65


def parsed_timestamp(value: str | None) -> datetime | None:
  if not value:
    return None
  try:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError:
    return None


def market_data_quality(
  sport: str,
  bet_type: str,
  model_context: dict[str, Any] | None,
  impact: dict[str, Any] | None,
  market: dict[str, Any],
  recommended_price: float | None,
  captured_at: str,
) -> int:
  score = 35
  if model_context:
    score += 25
    if model_context.get("source") == "Blue Chip Analytics":
      score += 8
    elif sport == "NFL":
      score += 6
  if recommended_price is not None:
    score += 10
  if market.get("updated_time"):
    updated = parsed_timestamp(market.get("updated_time"))
    captured = parsed_timestamp(captured_at)
    if updated and captured:
      age_hours = max((captured - updated).total_seconds() / 3600, 0)
      if age_hours <= 1:
        score += 12
      elif age_hours <= 12:
        score += 8
      elif age_hours <= 48:
        score += 4
  if sport == "NCAAF" and model_context and (model_context.get("weather") or {}).get("condition"):
    score += 6
  if impact:
    score += 4
  if bet_type == "total" and not (impact and impact.get("adjusted_total") is not None):
    score = min(score, 58)
  return round(clamp(score, 0, 100))


def confidence_label(model_gap: float | None, data_quality: int) -> str:
  if model_gap is None:
    return "Low"
  if model_gap >= 4 and data_quality >= 80:
    return "High"
  if model_gap >= 2 and data_quality >= 65:
    return "Medium"
  return "Low"


def rating_grade(probability: float | None, edge: float | None, has_model: bool) -> str:
  if probability is None or not has_model:
    return "Even"
  if edge is not None and edge < 0:
    return "Even"
  if probability >= 68 and (edge is None or edge >= 4):
    return "Excellent"
  if probability >= 60 and (edge is None or edge >= 2):
    return "Great"
  if probability >= 54 and (edge is None or edge >= 0):
    return "Good"
  return "Even"


def probability_from_gap(model_gap: float | None) -> float | None:
  if model_gap is None:
    return None
  return round(clamp(50 + model_gap * 4, 5, 95), 1)


def probability_from_margin(model_margin: float | None) -> float | None:
  if model_margin is None:
    return None
  return round(clamp(100 / (1 + math.exp(-model_margin / 7)), 5, 95), 1)


def rating_for_market(bet_type: str, market: dict[str, Any], bluechip: dict[str, Any] | None, impact: dict[str, Any] | None, model_gap: float | None, market_probability: float | None) -> dict[str, Any]:
  has_model = model_gap is not None
  probability = probability_from_margin(model_gap) if bet_type == "moneyline" else probability_from_gap(model_gap)
  if probability is None:
    probability = 50.0
  edge = None if not has_model or probability is None or market_probability is None else round(probability - market_probability * 100, 1)
  grade = rating_grade(probability, edge, has_model)
  reasons: list[str] = []
  line = market.get("yes_sub_title") or market.get("title") or "This contract"

  if has_model:
    if bet_type == "moneyline":
      reasons.append(f"Model margin gives this side an estimated {probability:.1f}% win chance.")
    else:
      reasons.append(f"Model gap is {model_gap:+.1f} points against this exact line.")
  else:
    reasons.append("No model projection is attached yet, so this is treated as even until the model feed covers it.")

  if market_probability is not None and probability is not None and has_model:
    reasons.append(f"Market odds imply about {market_probability * 100:.1f}%, versus model estimate {probability:.1f}%.")
  elif market_probability is not None:
    reasons.append(f"Market odds imply about {market_probability * 100:.1f}%; no model edge is counted yet.")

  if bluechip and bluechip.get("model_line"):
    reasons.append(f"{bluechip.get('source') or 'Model'}: {bluechip['model_line']} vs market {bluechip.get('market_line') or 'line unavailable'}.")

  if impact and abs(impact.get("total_adjustment") or 0) >= 0.1:
    reasons.append(f"Weather adjusts the total by {impact['total_adjustment']:+.1f} points.")

  return {
    "probability": probability,
    "grade": grade,
    "edge": edge,
    "summary": f"{grade}: {probability:.1f}% model lean on {line}" if probability is not None and has_model else f"{grade}: model lean unavailable for {line}",
    "reasons": reasons,
  }


def total_edge_side(market: dict[str, Any], model_gap: float) -> str:
  label = f"{market.get('yes_sub_title') or ''} {market.get('title') or ''}".lower()
  yes_side = "Under" if "under" in label else "Over"
  if model_gap >= 0:
    return yes_side
  return "Over" if yes_side == "Under" else "Under"


def recommended_market_side(bet_type: str, market: dict[str, Any], away_team: str, home_team: str, model_gap: float | None) -> tuple[str | None, str | None]:
  if model_gap is None:
    return None, None
  side_team = contract_side_team(market)
  if bet_type == "total":
    edge_side = total_edge_side(market, model_gap)
    yes_side = "Under" if edge_side == "Under" and model_gap >= 0 else "Over" if edge_side == "Over" and model_gap >= 0 else None
    contract_side = "Yes" if yes_side else "No"
    return edge_side, contract_side
  if not side_team:
    return None, None
  if model_gap >= 0:
    return side_team, "Yes"
  return opponent_team(away_team, home_team, side_team), "No"


def parse_bluechip_game(page: str, url: str) -> dict[str, Any] | None:
  text = plain_text(page)
  answer_match = re.search(r'<div class="answer-capsule".*?</div>', page, re.I | re.S)
  answer_text = plain_text(answer_match.group(0)) if answer_match else text
  title_match = re.search(r"<title>(.*?)\s+Prediction,", page, re.I | re.S)
  if not title_match:
    return None
  teams = clean_text(re.sub(r"\s+", " ", title_match.group(1))).split(" vs ")
  if len(teams) != 2:
    return None
  away_team, home_team = teams

  line_match = re.search(
    r"The market has (?P<market_team>.+?) (?P<market_spread>[+-]?\d+(?:\.\d+)?) and the Blue Chip model makes it (?P<model_team>.+?) (?P<model_spread>[+-]?\d+(?:\.\d+)?) - a gap of (?P<gap>\d+(?:\.\d+)?) points toward (?P<edge_team>.+?)(?:,|\.)",
    text,
    re.I | re.S,
  )
  if not line_match:
    line_match = re.search(
      r"The model makes it (?P<model_team>.+?) (?P<model_spread>[+-]?\d+(?:\.\d+)?) against a book line of (?P<market_team>.+?) (?P<market_spread>[+-]?\d+(?:\.\d+)?); it sees .*? (?P<gap>\d+(?:\.\d+)?) points? off the spread",
      text,
      re.I | re.S,
    )
  summary_line_match = re.search(
    r"market line is (?P<market_team>.+?) (?P<market_spread>[+-]?\d+(?:\.\d+)?) and our model shows (?P<model_team>.+?) (?P<model_spread>[+-]?\d+(?:\.\d+)?)\s*; (?P<verdict>.+?)\.",
    answer_text,
    re.I | re.S,
  )
  weather_match = re.search(
    r"The forecast for (?P<venue>.+?) shows (?P<condition>.+?), (?P<temp>\d+(?:\.\d+)?)\s*[°\ufffd]F with winds of (?P<wind>\d+(?:\.\d+)?) mph",
    text,
    re.I | re.S,
  )
  desc_match = re.search(r'<meta name="description" content="([^"]+)"', page, re.I)
  image_match = re.search(r'<meta property="og:image"\s+content="([^"]+)"', page, re.I)
  modified_match = re.search(r'"dateModified":\s*"([^"]+)"', page, re.I)

  spread_text = betting_value(page, "Spread")
  total_text = betting_value(page, "Total")
  implied_score = betting_value(page, "Odds implied score")
  capsule_model_match = re.search(r'id="capsuleModelLine"[^>]*>(.*?)</span>', page, re.I | re.S)
  capsule_model_team, capsule_model_spread = parse_team_line(re.sub(r"<[^>]+>", " ", capsule_model_match.group(1)) if capsule_model_match else None)
  betting_source_match = re.search(r'<p class="bb-source">(.*?)</p>', page, re.I | re.S)
  betting_source = clean_text(re.sub(r"<[^>]+>", " ", betting_source_match.group(1))) if betting_source_match else None
  spread_source = summary_line_match or line_match

  market_team = clean_text(spread_source.group("market_team")) if spread_source else None
  model_team = capsule_model_team or (clean_text(spread_source.group("model_team")) if spread_source else None)
  market_spread = float(spread_source.group("market_spread")) if spread_source else None
  model_spread = capsule_model_spread if capsule_model_spread is not None else (float(spread_source.group("model_spread")) if spread_source else None)
  desc_text = clean_text(desc_match.group(1)) if desc_match else None

  if desc_text and (market_spread is None or model_spread is None):
    fallback = re.search(
      r"line (?P<market_team>.+?) (?P<market_spread>[+-]?\d+(?:\.\d+)?)\.\s+Power ratings favor (?P<model_team>.+?) by (?P<model_margin>\d+(?:\.\d+)?)",
      desc_text,
      re.I,
    )
    if fallback:
      market_team = clean_text(fallback.group("market_team"))
      market_spread = float(fallback.group("market_spread"))
      model_team = clean_text(fallback.group("model_team"))
      model_margin = float(fallback.group("model_margin"))
      market_opponent = opponent_team(away_team, home_team, market_team)
      model_spread = -model_margin if model_team == market_team else model_margin
      if market_spread and market_team and market_opponent and market_spread > 0:
        market_team = market_opponent
        market_spread = -market_spread

  gap = float(line_match.group("gap")) if not summary_line_match and line_match and "gap" in line_match.groupdict() and line_match.group("gap") else None

  edge_team = clean_text(line_match.group("edge_team")) if line_match and "edge_team" in line_match.groupdict() else None
  if not edge_team and summary_line_match:
    verdict = clean_text(summary_line_match.group("verdict")) or ""
    lean_match = re.search(r"lean toward (.+)$", verdict, re.I)
    edge_team = clean_text(lean_match.group(1)) if lean_match else None
  normalized_gap, normalized_edge_team = spread_gap_and_edge(away_team, home_team, market_team, market_spread, model_team, model_spread)
  if normalized_gap is not None:
    gap = normalized_gap
  if not edge_team:
    edge_team = normalized_edge_team

  return {
    "away_team": away_team,
    "home_team": home_team,
    "market_line": signed_line(market_team, market_spread),
    "model_line": signed_line(model_team, model_spread),
    "market_team": market_team,
    "model_team": model_team,
    "market_spread": market_spread,
    "model_spread": model_spread,
    "gap": gap,
    "edge_team": edge_team,
    "book_spread": spread_text,
    "book_total": dollars_to_float(total_text),
    "odds_implied_score": implied_score,
    "betting_source": betting_source,
    "summary": desc_text,
    "weather": {
      "venue": clean_text(weather_match.group("venue")) if weather_match else None,
      "condition": clean_text(weather_match.group("condition")) if weather_match else None,
      "temperature_f": float(weather_match.group("temp")) if weather_match else None,
      "wind_mph": float(weather_match.group("wind")) if weather_match else None,
      "source": "WeatherAPI.com",
      "map_url": clean_text(image_match.group(1)) if image_match else None,
    },
    "source": "Blue Chip Analytics",
    "url": url,
    "updated_at": clean_text(modified_match.group(1)) if modified_match else None,
  }


async def fetch_bluechip_games() -> dict[str, dict[str, Any]]:
  if not BLUECHIP_WEEK_URL:
    return {}
  now = time.time()
  if BLUECHIP_CACHE["expires_at"] > now:
    return BLUECHIP_CACHE["games"]

  async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
    response = await client.get(BLUECHIP_WEEK_URL)
    response.raise_for_status()
    week_page = response.text
    urls = sorted(
      {
        url if url.startswith("http") else f"https://bluechipanalytics.com{url}"
        for url in re.findall(r'https://bluechipanalytics\.com/college-football/games/2026/week\d+/2026-[^"]+?/|href="(/college-football/games/2026/week\d+/2026-[^"]+?/)"', week_page)
        for url in ((url,) if isinstance(url, str) else url)
        if url
      }
    )
    if not urls:
      urls = sorted(set(re.findall(r"https://bluechipanalytics\.com/college-football/games/2026/week\d+/2026-[^\" ]+?/", week_page)))

    semaphore = asyncio.Semaphore(8)

    async def fetch_one(url: str) -> dict[str, Any] | None:
      async with semaphore:
        try:
          game_response = await client.get(url)
          game_response.raise_for_status()
          return parse_bluechip_game(game_response.text, url)
        except Exception:
          return None

    games = [game for game in await asyncio.gather(*(fetch_one(url) for url in urls)) if game]
    keyed = {matchup_key(game["away_team"], game["home_team"]): game for game in games}
    BLUECHIP_CACHE.update({"expires_at": now + BLUECHIP_CACHE_SECONDS, "games": keyed})
    return keyed


async def fetch_nfl_power_ratings() -> dict[str, dict[str, Any]]:
  if not NFL_POWER_RATINGS_URL:
    return {}
  now = time.time()
  if NFL_RATINGS_CACHE["expires_at"] > now:
    return NFL_RATINGS_CACHE["ratings"]

  try:
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
      response = await client.get(NFL_POWER_RATINGS_URL)
      response.raise_for_status()
      page = response.text
  except Exception:
    return NFL_RATINGS_CACHE.get("ratings") or {}

  page_text = plain_text(page)
  updated_match = re.search(r"Updated\s+([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2},\s+[^.]+?EDT)", page_text, re.I)
  if not updated_match:
    updated_match = re.search(r"updated\s+([^<.]+?\s+ET)\s+Power rankings", page_text, re.I)
  updated_at = clean_text(updated_match.group(1)) if updated_match else now_iso()
  ratings: dict[str, dict[str, Any]] = {}
  for match in re.finditer(r'<tr data-href="/team/(?P<abbr>[^"]+)">(?P<body>.*?)</tr>', page, re.I | re.S):
    body = match.group("body")
    team_match = re.search(r'alt="(?P<team>[^"]+)"', body, re.I)
    rating_match = re.search(r'class="num sc"[^>]*data-sort="(?P<rating>[+-]?\d+(?:\.\d+)?)"', body, re.I)
    if not team_match or not rating_match:
      continue

    team = clean_text(team_match.group("team")) or ""
    rating = dollars_to_float(rating_match.group("rating"))
    if not team or rating is None:
      continue

    last_word = team.split()[-1]
    record_match = re.search(r'<td class="txt"[^>]*data-sort="(?P<record>[^"]+)"', body, re.I)
    entry = {
      "team": team,
      "rating": rating,
      "raw_rating": rating,
      "adjustment": 0.0,
      "source": "Innerpulse NFL power ratings",
      "url": NFL_POWER_RATINGS_URL,
      "updated_at": updated_at,
      "record": clean_text(record_match.group("record")) if record_match else None,
    }
    keys = {
      team_key(team),
      team_key(match.group("abbr")),
      team_key(f"{match.group('abbr')} {last_word}"),
      team_key(last_word),
    }
    for key in keys:
      ratings[key] = entry

  if ratings:
    NFL_RATINGS_CACHE.update({"expires_at": now + NFL_RATINGS_CACHE_SECONDS, "ratings": ratings})
  return ratings


def load_projections() -> dict[str, dict[str, Any]]:
  if not PROJECTIONS_PATH.exists():
    return {}
  with PROJECTIONS_PATH.open(newline="", encoding="utf-8") as f:
    rows = csv.DictReader(f)
    return {
      row["game_id"]: {
        "fair_spread": float(row["fair_spread"]),
        "source": row.get("source") or "csv_projection",
        "updated_at": row.get("updated_at") or None,
      }
      for row in rows
      if row.get("game_id") and row.get("fair_spread")
    }


def load_team_ratings() -> dict[str, dict[str, dict[str, Any]]]:
  if not TEAM_RATINGS_PATH.exists():
    return {}
  ratings: dict[str, dict[str, dict[str, Any]]] = {}
  with TEAM_RATINGS_PATH.open(newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
      sport = (row.get("sport") or "").upper()
      team = row.get("team") or ""
      rating = dollars_to_float(row.get("power_rating"))
      if not sport or not team or rating is None:
        continue
      adjustment = fp_to_float(row.get("injury_adj")) + fp_to_float(row.get("form_adj"))
      ratings.setdefault(sport, {})[team_key(team)] = {
        "team": team,
        "rating": rating + adjustment,
        "raw_rating": rating,
        "adjustment": adjustment,
      }
  return ratings


def ratings_model_game(sport: str, away_team: str, home_team: str, ratings: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any] | None:
  sport_ratings = ratings.get(sport.upper()) or {}
  away = sport_ratings.get(team_key(away_team))
  home = sport_ratings.get(team_key(home_team))
  if not away or not home:
    return None
  home_field = 1.5 if sport.upper() == "NFL" else 2.5
  home_margin = round(float(home["rating"]) + home_field - float(away["rating"]), 1)
  if home_margin >= 0:
    model_team = home_team
    model_spread = -home_margin
  else:
    model_team = away_team
    model_spread = home_margin
  source = home.get("source") or away.get("source") or "Team power ratings"
  source_url = home.get("url") or away.get("url")
  updated_at = home.get("updated_at") or away.get("updated_at") or now_iso()
  return {
    "away_team": away_team,
    "home_team": home_team,
    "market_line": None,
    "model_line": signed_line(model_team, model_spread),
    "market_team": None,
    "model_team": model_team,
    "market_spread": None,
    "model_spread": model_spread,
    "gap": None,
    "edge_team": None,
    "book_spread": None,
    "book_total": None,
    "odds_implied_score": None,
    "betting_source": None,
    "summary": f"{sport} rating model makes this {signed_line(model_team, model_spread)} from {away['team']} {float(away['rating']):+.1f} and {home['team']} {float(home['rating']):+.1f}.",
    "weather": {
      "venue": None,
      "condition": None,
      "temperature_f": None,
      "wind_mph": None,
      "source": "not attached",
      "map_url": None,
    },
    "source": source,
    "url": source_url,
    "updated_at": updated_at,
  }


async def fetch_odds() -> tuple[list[dict[str, Any]], str]:
  api_key = os.getenv("ODDS_API_KEY")
  if not api_key:
    return [], "ODDS_API_KEY is not set on the backend"

  rows: list[dict[str, Any]] = []
  captured_at = now_iso()
  async with httpx.AsyncClient(timeout=25) as client:
    for sport_name, sport_key in SPORTS.items():
      response = await client.get(
        API_URL.format(sport=sport_key),
        params={
          "apiKey": api_key,
          "regions": os.getenv("ODDS_REGION", "us"),
          "markets": "spreads",
          "oddsFormat": "american",
        },
      )
      response.raise_for_status()
      for event in response.json():
        game_id = canonical_id(sport_name, event["commence_time"], event["away_team"], event["home_team"])
        for book in event.get("bookmakers", []):
          spread_market = next((market for market in book.get("markets", []) if market.get("key") == "spreads"), None)
          if not spread_market:
            continue
          for outcome in spread_market.get("outcomes", []):
            rows.append(
              {
                "game_id": game_id,
                "provider_event_id": event["id"],
                "sport": sport_name,
                "commence_time": event["commence_time"],
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "book": book["title"],
                "market": "spreads",
                "side": outcome["name"],
                "line": outcome.get("point"),
                "price": outcome.get("price"),
                "captured_at": captured_at,
              }
            )
  return rows, f"Fetched {len(rows)} sportsbook lines"


async def fetch_kalshi_board() -> tuple[list[dict[str, Any]], str]:
  limit = int(os.getenv("KALSHI_MARKET_LIMIT", "1000"))
  max_pages = int(os.getenv("KALSHI_MAX_PAGES", "10"))
  captured_at = now_iso()
  board: list[dict[str, Any]] = []
  bluechip_games = await fetch_bluechip_games()
  team_ratings = load_team_ratings()
  nfl_power_ratings = await fetch_nfl_power_ratings()
  if nfl_power_ratings:
    team_ratings.setdefault("NFL", {}).update(nfl_power_ratings)
  raw_count = 0
  async with httpx.AsyncClient(timeout=25) as client:
    for (sport_name, bet_type), series_ticker in KALSHI_MARKET_SERIES.items():
      markets: list[dict[str, Any]] = []
      cursor = ""
      for _ in range(max_pages):
        params = {"series_ticker": series_ticker, "status": "open", "limit": limit}
        if cursor:
          params["cursor"] = cursor
        response = await client.get(f"{KALSHI_API_URL}/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        markets.extend(payload.get("markets", []))
        cursor = payload.get("cursor") or ""
        if not cursor:
          break

      for market in markets:
        raw_count += 1
        away_team, home_team = extract_matchup(market)
        bluechip = bluechip_games.get(matchup_key(away_team, home_team)) if sport_name == "NCAAF" else None
        model_context = bluechip or ratings_model_game(sport_name, away_team, home_team, team_ratings)
        if sport_name == "NCAAF" and bluechip_games and not bluechip:
          continue
        if sport_name != "NCAAF" and not KALSHI_INCLUDE_UNMODELED and not model_context:
          continue
        yes_bid = dollars_to_float(market.get("yes_bid_dollars"))
        yes_ask = dollars_to_float(market.get("yes_ask_dollars"))
        no_bid = dollars_to_float(market.get("no_bid_dollars"))
        no_ask = dollars_to_float(market.get("no_ask_dollars"))
        last_price = dollars_to_float(market.get("last_price_dollars"))
        previous_price = dollars_to_float(market.get("previous_price_dollars"))
        price_move = None
        if last_price is not None and previous_price is not None and previous_price > 0:
          price_move = round((last_price - previous_price) * 100, 1)
        baseline_total = dollars_to_float(market.get("floor_strike")) if bet_type == "total" else dollars_to_float((model_context or {}).get("book_total"))
        impact = weather_impact(bluechip, baseline_total)
        model_gap = contract_model_gap(bet_type, market, model_context, impact)
        display_gap = None if model_gap is None else abs(model_gap)
        edge_side, recommended_side = recommended_market_side(bet_type, market, away_team, home_team, model_gap)
        recommended_price = (yes_bid if yes_bid is not None else last_price) if recommended_side != "No" else (no_bid if no_bid is not None else None)
        if model_context and model_gap is not None:
          model_context = dict(model_context)
          model_context["gap"] = display_gap
          model_context["edge_team"] = edge_side
        projection = projection_summary(away_team, home_team, model_context, impact)
        rating = rating_for_market(bet_type, market, model_context, impact, display_gap, recommended_price)
        if edge_side and rating.get("probability") is not None:
          rating = dict(rating)
          market_phrase = "to cover" if bet_type == "spread" else "to win" if bet_type == "moneyline" else edge_side
          target = f"{edge_side} {market_phrase}" if bet_type != "total" else edge_side
          rating["summary"] = f"{rating['grade']}: {float(rating['probability']):.1f}% model lean on {target}"
        weather_score = impact.get("score", 0) if impact else 0
        positive_gap = display_gap or 0
        positive_edge = max(rating.get("edge") or 0, 0)
        edge_score = 0 if rating.get("grade") == "Even" else round(positive_gap * 10 + positive_edge * 10 + weather_score / 10, 2)
        data_quality = market_data_quality(sport_name, bet_type, model_context, impact, market, recommended_price, captured_at)
        adjusted_gap = None if display_gap is None else round(display_gap * data_quality_multiplier(data_quality), 2)

        board.append(
          {
            "game_id": market["ticker"],
            "data_source": "kalshi",
            "sport": sport_name,
            "bet_type": bet_type,
            "edge_score": edge_score,
            "commence_time": market.get("occurrence_datetime") or market.get("expected_expiration_time"),
            "away_team": away_team,
            "home_team": home_team,
            "market": {
              "consensus_spread": market.get("floor_strike"),
              "best_favorite_line": yes_bid,
              "best_underdog_line": yes_ask,
              "opening_spread": previous_price,
              "book_count": 1,
              "latest_book": "Kalshi",
              "latest_timestamp": market.get("updated_time") or captured_at,
            },
            "contract": {
              "ticker": market["ticker"],
              "title": (market.get("title") or "").replace("?", ""),
              "side_label": market.get("yes_sub_title") or market.get("title"),
              "yes_bid": yes_bid,
              "yes_ask": yes_ask,
              "no_bid": no_bid,
              "no_ask": no_ask,
              "last_price": last_price,
              "previous_price": previous_price,
              "price_move": price_move,
              "recommended_side": recommended_side,
              "recommended_label": edge_side,
              "recommended_price": recommended_price,
              "volume": fp_to_float(market.get("volume_fp")),
              "volume_24h": fp_to_float(market.get("volume_24h_fp")),
              "open_interest": fp_to_float(market.get("open_interest_fp")),
              "status": market.get("status"),
            },
            "model": {
              "fair_spread": model_context.get("model_spread") if model_context else None,
              "source": model_context.get("source") if model_context else None,
              "updated_at": model_context.get("updated_at") if model_context else None,
            },
            "bluechip": model_context,
            "weather_impact": impact,
            "projection": projection,
            "rating": rating,
            "metrics": {
              "model_market_gap": display_gap,
              "raw_projection_gap": display_gap,
              "adjusted_projection_gap": adjusted_gap,
              "line_move": price_move,
              "confidence_score": gap_confidence(display_gap),
              "data_quality": data_quality,
              "confidence_label": confidence_label(display_gap, data_quality),
            },
            "updated_at": market.get("updated_time") or captured_at,
          }
        )

  ranked_rows = collapse_board_rows(board)
  return ranked_rows, f"Fetched {raw_count} Kalshi contracts; using {len(bluechip_games)} Blue Chip NCAAF games and {len(nfl_power_ratings)} NFL rating keys; showing {len(ranked_rows)} ranked lines"


def store_snapshots(rows: list[dict[str, Any]]) -> None:
  if not rows:
    return
  with connect() as conn:
    conn.executemany(
      """
      INSERT INTO odds_snapshots (
        game_id, provider_event_id, sport, commence_time, home_team, away_team,
        book, market, side, line, price, captured_at
      ) VALUES (
        :game_id, :provider_event_id, :sport, :commence_time, :home_team, :away_team,
        :book, :market, :side, :line, :price, :captured_at
      )
      """,
      rows,
    )
    conn.commit()


def current_snapshots() -> list[sqlite3.Row]:
  with connect() as conn:
    return conn.execute(
      """
      SELECT o.*
      FROM odds_snapshots o
      JOIN (
        SELECT game_id, book, side, MAX(captured_at) AS captured_at
        FROM odds_snapshots
        GROUP BY game_id, book, side
      ) latest
      ON o.game_id = latest.game_id
        AND o.book = latest.book
        AND o.side = latest.side
        AND o.captured_at = latest.captured_at
      ORDER BY o.commence_time
      """
    ).fetchall()


def opening_spread(conn: sqlite3.Connection, game_id: str, home_team: str) -> float | None:
  row = conn.execute(
    """
    SELECT line FROM odds_snapshots
    WHERE game_id = ? AND side = ?
    ORDER BY captured_at ASC, id ASC
    LIMIT 1
    """,
    (game_id, home_team),
  ).fetchone()
  return None if row is None else row["line"]


def build_board() -> list[dict[str, Any]]:
  projections = load_projections()
  snapshots = current_snapshots()
  grouped: dict[str, list[sqlite3.Row]] = {}
  for row in snapshots:
    grouped.setdefault(row["game_id"], []).append(row)

  board: list[dict[str, Any]] = []
  with connect() as conn:
    for game_id, rows in grouped.items():
      sample = rows[0]
      home_team = sample["home_team"]
      away_team = sample["away_team"]
      home_rows = [row for row in rows if row["side"] == home_team and row["line"] is not None]
      away_rows = [row for row in rows if row["side"] == away_team and row["line"] is not None]
      if not home_rows:
        continue

      consensus = statistics.median([row["line"] for row in home_rows])
      opener = opening_spread(conn, game_id, home_team)
      line_move = None if opener is None else consensus - opener
      latest = max(rows, key=lambda row: row["captured_at"])
      projection = projections.get(game_id, {})
      fair_spread = projection.get("fair_spread")
      gap = None if fair_spread is None else fair_spread - consensus
      home_favorite = consensus <= 0
      favorite_pool = home_rows if home_favorite else away_rows
      underdog_pool = away_rows if home_favorite else home_rows
      confidence = min(96, 54 + len({row["book"] for row in rows}) * 5 + (12 if fair_spread is not None else 0))

      board.append(
        {
          "game_id": game_id,
          "data_source": "sportsbook",
          "sport": sample["sport"],
          "commence_time": sample["commence_time"],
          "away_team": away_team,
          "home_team": home_team,
          "market": {
            "consensus_spread": consensus,
            "best_favorite_line": max([row["line"] for row in favorite_pool], default=None),
            "best_underdog_line": max([row["line"] for row in underdog_pool], default=None),
            "opening_spread": opener,
            "book_count": len({row["book"] for row in rows}),
            "latest_book": latest["book"],
            "latest_timestamp": latest["captured_at"],
          },
          "model": {
            "fair_spread": fair_spread,
            "source": projection.get("source"),
            "updated_at": projection.get("updated_at"),
          },
          "metrics": {
            "model_market_gap": gap,
            "line_move": line_move,
            "confidence_score": confidence,
          },
          "updated_at": latest["captured_at"],
        }
      )

  return sorted(
    board,
    key=lambda row: abs(row["metrics"]["model_market_gap"] or row["metrics"]["line_move"] or 0),
    reverse=True,
  )


@app.get("/api/health")
def health() -> dict[str, Any]:
  return {
    "ok": True,
    "generated_at": now_iso(),
    "odds_api_configured": bool(os.getenv("ODDS_API_KEY")),
    "kalshi_public_data": True,
  }


@app.get("/api/board")
async def board() -> dict[str, Any]:
  status = "Using stored snapshots"
  try:
    rows, status = await fetch_odds()
    store_snapshots(rows)
  except Exception as exc:
    status = f"Odds refresh failed; using stored snapshots: {exc}"

  rows = build_board()
  source = "sportsbook"
  if not rows:
    try:
      rows, status = await fetch_kalshi_board()
      source = "kalshi"
    except Exception as exc:
      status = f"Kalshi refresh failed and no sportsbook snapshots are stored: {exc}"
      source = "live"

  return {
    "generated_at": now_iso(),
    "source": source,
    "status": status,
    "rows": rows,
  }


def is_pregame_row(row: dict[str, Any]) -> bool:
  commence = parsed_timestamp(row.get("commence_time"))
  return commence is None or commence > datetime.now(timezone.utc)


@app.get("/api/top-projection-gaps")
async def top_projection_gaps(
  week: str = "current",
  limit: int = 10,
  leagues: str = "NFL,NCAAF",
  markets: str = "spread",
  min_gap: float = 1.0,
  min_confidence: int = 0,
) -> dict[str, Any]:
  rows, status = await fetch_kalshi_board()
  allowed_leagues = {league.strip().upper() for league in leagues.split(",") if league.strip()}
  allowed_markets = {market.strip().lower() for market in markets.split(",") if market.strip()}
  if "all" in allowed_markets:
    allowed_markets = {"spread", "total", "moneyline"}
  if "ALL" in allowed_leagues:
    allowed_leagues = {"NFL", "NCAAF"}

  candidates = [
    row
    for row in rows
    if row.get("sport") in allowed_leagues
    and (row.get("bet_type") or "spread") in allowed_markets
    and is_pregame_row(row)
    and ((row.get("metrics") or {}).get("raw_projection_gap") or 0) >= min_gap
    and ((row.get("metrics") or {}).get("confidence_score") or 0) >= min_confidence
  ]
  candidates.sort(
    key=lambda row: (
      (row.get("metrics") or {}).get("adjusted_projection_gap") or 0,
      (row.get("metrics") or {}).get("raw_projection_gap") or 0,
      (row.get("metrics") or {}).get("confidence_score") or 0,
      row.get("updated_at") or "",
    ),
    reverse=True,
  )

  return {
    "generated_at": now_iso(),
    "source": "kalshi",
    "status": f"{status}; top projection gaps week={week}",
    "week": week,
    "limit": limit,
    "rows": candidates[: max(1, min(limit, 50))],
  }
