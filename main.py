"""
Unofficial Flashscore soccer HTTP test API for Vercel/FastAPI.
Python 3.9 compatible.

Purpose:
  - Test whether Flashscore soccer can be fetched from Vercel/EC2 without Playwright.
  - Provide a compact verifier-style endpoint for Polymarket soccer winner/draw/total/spread/handicap/halftime markets.

Layout:
  main.py
  app.py          -> from main import app
  requirements.txt
  vercel.json

Local run:
  python3 -m pip install -r requirements.txt
  python3 main.py

Examples:
  curl "http://127.0.0.1:3003/v2/health"
  curl "http://127.0.0.1:3003/v2/soccer?date=2026-05-31&source=auto"
  curl "http://127.0.0.1:3003/v2/soccer/details?date=2026-05-31&tournament=FIFA%20Friendly&home=Poland&away=Ukraine&proposed=Ukraine"
  curl "http://127.0.0.1:3003/v2/soccer/details?date=2026-05-31&home=CS%20Cienciano&away=CS%20Cristal&proposed=Over&question=CS%20Cienciano%20vs.%20CS%20Cristal:%20O/U%202.5"
  curl "http://127.0.0.1:3003/v2/soccer/details?date=2026-05-31&home=CS%20Cienciano&away=CS%20Cristal&proposed=CS%20Cristal&question=Spread:%20CS%20Cienciano%20(-1.5)"
  curl "http://127.0.0.1:3003/v2/debug/fetch?source=feed&date=2026-05-31"
  curl "http://127.0.0.1:3003/v2/debug/search?date=2026-05-31&q=Ukraine"
"""

import html
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

API_PORT = int(os.getenv("FLASHSCORE_API_PORT", "3003"))
DEFAULT_TIMEOUT = float(os.getenv("FLASHSCORE_API_TIMEOUT", "18"))
CACHE_TTL_SECONDS = int(os.getenv("FLASHSCORE_API_CACHE_TTL", "25"))
DEBUG_FETCH_CHARS = int(os.getenv("FLASHSCORE_DEBUG_FETCH_CHARS", "2500"))
MAX_MATCHES_DEFAULT = int(os.getenv("FLASHSCORE_MAX_MATCHES", "300"))

# x-fsign is widely used by Flashscore's browser XHR feed. It may change.
# Keep this env-overridable so the Vercel deploy can be fixed without code changes.
FLASHSCORE_X_FSIGN = os.getenv("FLASHSCORE_X_FSIGN", "SW9D1eZo")

DOMAINS = {
    "com": "https://www.flashscore.com",
    "usa": "https://www.flashscoreusa.com",
}

SPORT_PATHS = {
    "com": "/football/",
    "usa": "/soccer/",
}


def sport_path(domain_key: str) -> str:
    return SPORT_PATHS.get(domain_key, "/football/")

# Known/likely Flashscore feed candidates. Flashscore changes these sometimes;
# the debug endpoint reports each attempted URL so you can see which one answers.
# For soccer, sport id is usually 1. The last numeric segment differs by view/status.
FEED_CANDIDATES = [
    "/x/feed/f_1_0_1_en_1",
    "/x/feed/f_1_0_2_en_1",
    "/x/feed/f_1_0_3_en_1",
    "/x/feed/f_1_0_4_en_1",
    "/x/feed/f_1_0_5_en_1",
    "/x/feed/f_1_0_6_en_1",
    "/x/feed/f_1_0_7_en_1",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Origin": "https://www.flashscore.com",
    "Referer": "https://www.flashscore.com/football/",
    "x-fsign": FLASHSCORE_X_FSIGN,
}

HEADER_PROFILES = [
    ("desktop", HEADERS),
    (
        "flashscoreusa",
        dict(
            HEADERS,
            **{
                "Origin": "https://www.flashscoreusa.com",
                "Referer": "https://www.flashscoreusa.com/soccer/",
            },
        ),
    ),
    (
        "googlebot",
        dict(
            HEADERS,
            **{
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "X-Forwarded-For": "66.249.66.1",
            },
        ),
    ),
]

app = FastAPI(
    title="Flashscore Soccer HTTP Test API",
    version="1.2.0",
    description="Unofficial Flashscore soccer HTTP verifier/debug API for Vercel.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache: Dict[str, Tuple[float, Any]] = {}


@dataclass
class TeamKey:
    last: str
    initial: str
    norm: str


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag in {"br", "p", "div", "section", "article", "li", "tr", "td", "th", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return collapse_ws("\n".join(self.parts), keep_newlines=True)


class AnchorExtractor(HTMLParser):
    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._stack: List[Dict[str, Any]] = []
        self.anchors: List[Dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        d = dict(attrs)
        self._stack.append({"href": d.get("href") or "", "text": []})

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        for item in self._stack:
            item["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._stack:
            return
        item = self._stack.pop()
        href = (item.get("href") or "").strip()
        text = collapse_ws(" ".join(item.get("text") or []))
        if href or text:
            self.anchors.append({"href": href, "text": text})


def collapse_ws(s: str, keep_newlines: bool = False) -> str:
    s = html.unescape(s or "")
    if keep_newlines:
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in s.splitlines()]
        return "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", s).strip()


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm_text(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9/ .'-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


TEAM_DROP_TOKENS = {
    "fc", "cf", "sc", "afc", "cfc", "ac", "as", "cd", "sd", "ca", "ec", "fk", "bk",
    "club", "football", "soccer", "team", "cs", "de", "da", "do", "del", "la", "the",
    "men", "women", "w", "u18", "u19", "u20", "u21", "u23", "ii", "b", "reserves",
}

TEAM_ALIASES = {
    "utd": "united",
    "intl": "internacional",
    "int": "internacional",
    "inter": "internazionale",
    "ath": "athletic",
    "athl": "athletic",
    "dep": "deportivo",
}


def _team_tokens(name: str) -> List[str]:
    n = norm_text(name).replace(".", " ").replace("&", " and ")
    raw = [x for x in re.split(r"[^a-z0-9]+", n) if x]
    out: List[str] = []
    for tok in raw:
        tok = TEAM_ALIASES.get(tok, tok)
        if tok in TEAM_DROP_TOKENS:
            continue
        out.append(tok)
    return out or raw


def team_key(name: str) -> TeamKey:
    n = norm_text(name).replace(".", " ")
    toks = _team_tokens(name)
    core = " ".join(toks)
    return TeamKey(last=core, initial="", norm=n)


def _token_similarity(a: List[str], b: List[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    jacc = inter / union
    contain = inter / max(1, min(len(sa), len(sb)))
    return max(jacc, contain)


def team_similarity(a_name: str, b_name: str) -> float:
    a = team_key(a_name)
    b = team_key(b_name)
    if not a.last or not b.last:
        return 0.0
    if a.last == b.last or a.norm == b.norm:
        return 1.0
    if len(a.last) >= 4 and len(b.last) >= 4 and (a.last in b.last or b.last in a.last):
        return 0.96
    token_score = _token_similarity(_team_tokens(a_name), _team_tokens(b_name))
    seq_score = SequenceMatcher(None, a.last, b.last).ratio()
    return max(token_score, seq_score)


def same_team(target_name: str, flash_name: str) -> bool:
    return team_similarity(target_name, flash_name) >= 0.84


def pair_match_score(home_target: str, away_target: str, flash_home: str, flash_away: str) -> Tuple[int, bool]:
    normal_home = team_similarity(home_target, flash_home)
    normal_away = team_similarity(away_target, flash_away)
    rev_home = team_similarity(home_target, flash_away)
    rev_away = team_similarity(away_target, flash_home)

    normal_score = int(round(((normal_home + normal_away) / 2.0) * 100))
    reversed_score = int(round(((rev_home + rev_away) / 2.0) * 100))

    if normal_score >= 84 and normal_score >= reversed_score:
        return normal_score, False
    if reversed_score >= 84:
        return reversed_score, True
    return max(normal_score, reversed_score), reversed_score > normal_score

def parse_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def is_final_status(status: str) -> bool:
    s = norm_text(status)
    return any(x in s for x in ("finished", "final", "after penalties", "ret", "walkover", "w o", "awarded", "abandoned"))


def status_from_fields(fields: Dict[str, str]) -> str:
    # AC/AB values vary by sport/status. Preserve raw values for debugging.
    ac = fields.get("AC", "")
    ab = fields.get("AB", "")
    stage = fields.get("AO", "") or fields.get("AX", "") or fields.get("BA", "")
    pieces = []
    if ac:
        pieces.append("AC=" + ac)
    if ab:
        pieces.append("AB=" + ab)
    if stage and not re.fullmatch(r"\d+", stage):
        pieces.append(stage)
    raw = " ".join(pieces).strip()
    # Common Flashscore status code observed in many feeds: AC=3 is finished.
    if ac == "3":
        return "finished " + raw
    if ac == "1":
        return "live " + raw
    if ac in {"10", "11", "12"}:
        return "finished/ended " + raw
    return raw or "unknown"


def determine_winner(match: Dict[str, Any]) -> Tuple[Optional[str], str]:
    home = match.get("home") or ""
    away = match.get("away") or ""
    status = match.get("status") or ""
    hs = parse_int(match.get("home_score"))
    away_s = parse_int(match.get("away_score"))

    if match.get("home_winner_class") and not match.get("away_winner_class"):
        return home, "winner marker"
    if match.get("away_winner_class") and not match.get("home_winner_class"):
        return away, "winner marker"

    if hs is None or away_s is None:
        return None, "no score proof"
    if not is_final_status(status):
        return None, "not final yet"
    if hs > away_s:
        return home, "final score"
    if away_s > hs:
        return away, "final score"
    return "Draw", "final score draw"


def is_draw_outcome(value: str) -> bool:
    n = norm_text(value)
    return n in {"draw", "tie", "x"} or "draw" in n


def is_yes(value: str) -> bool:
    return norm_text(value) in {"yes", "y", "true", "1"}


def is_no(value: str) -> bool:
    return norm_text(value) in {"no", "n", "false", "0"}


def norm_market_type(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm_text(value or "")).strip("_")


def infer_market_type(question: str, proposed: str, home: str, away: str, requested: str = "auto") -> str:
    requested_norm = norm_market_type(requested or "auto")
    supported = {
        "moneyline",
        "draw_binary",
        "home_win_binary",
        "away_win_binary",
        "total",
        "spread",
        "handicap",
        "halftime_leader_binary",
    }
    if requested_norm in supported and requested_norm != "handicap":
        return requested_norm
    if requested_norm == "handicap":
        return "spread"

    raw_q = question or ""
    raw_q_l = raw_q.lower()
    q = norm_text(raw_q)
    p = norm_text(proposed or "")

    # One generic endpoint is enough. Keep market_type=auto and infer from the
    # title/question. We only need a final score for total/spread/handicap, and
    # first-half period score for halftime-leading markets.
    if re.search(r"\b(spread|handicap|asian\s+handicap|run\s*line|goal\s*line)\b", q):
        return "spread"
    if (
        "o/u" in raw_q_l
        or "o / u" in raw_q_l
        or "over/under" in raw_q_l
        or "over under" in q
        or re.search(r"\btotal\b", q)
        or re.search(r"\b(goals?|points?)\b", q) and p in {"over", "under"}
        or p in {"over", "under"}
    ):
        return "total"
    if (
        "leading at halftime" in q
        or "lead at halftime" in q
        or "leading at half time" in q
        or "lead at half time" in q
        or ("halftime" in q and ("leading" in q or "leader" in q or "lead" in q))
        or ("half time" in q and ("leading" in q or "leader" in q or "lead" in q))
    ):
        return "halftime_leader_binary"

    proposed_yes_no = is_yes(proposed) or is_no(proposed)
    if proposed_yes_no and ("draw" in q or " tie" in (" " + q)):
        return "draw_binary"

    if proposed_yes_no and any(w in q for w in ("win", "winner", "beat", "defeat")):
        home_score = team_similarity(home, q)
        away_score = team_similarity(away, q)
        if home_score >= 0.70 and home_score >= away_score:
            return "home_win_binary"
        if away_score >= 0.70:
            return "away_win_binary"

    return "moneyline"


def proposed_matches_winner(
    proposed: str,
    winner: str,
    home: str,
    away: str,
    market_type: str = "moneyline",
) -> Tuple[Optional[bool], str]:
    mt = norm_market_type(market_type or "moneyline")
    winner_is_draw = is_draw_outcome(winner)

    if mt == "draw_binary":
        if is_yes(proposed):
            return winner_is_draw, "YES means the match ended in a draw"
        if is_no(proposed):
            return (not winner_is_draw), "NO means the match did not end in a draw"
        return None, "draw_binary market needs proposed=Yes or proposed=No"

    if mt == "home_win_binary":
        home_won = (not winner_is_draw) and same_team(winner, home)
        if is_yes(proposed):
            return home_won, "YES means the home team won"
        if is_no(proposed):
            return (not home_won), "NO means the home team did not win"
        return None, "home_win_binary market needs proposed=Yes or proposed=No"

    if mt == "away_win_binary":
        away_won = (not winner_is_draw) and same_team(winner, away)
        if is_yes(proposed):
            return away_won, "YES means the away team won"
        if is_no(proposed):
            return (not away_won), "NO means the away team did not win"
        return None, "away_win_binary market needs proposed=Yes or proposed=No"

    if is_draw_outcome(proposed):
        return winner_is_draw, "proposed side is draw"
    if norm_text(proposed) in {"home", "team 1", "1"}:
        return (not winner_is_draw) and same_team(winner, home), "proposed side aliases the home team"
    if norm_text(proposed) in {"away", "team 2", "2"}:
        return (not winner_is_draw) and same_team(winner, away), "proposed side aliases the away team"
    if winner_is_draw:
        return False, "match ended in a draw"
    return same_team(proposed, winner), "proposed side matched against final winner"


def _clean_numeric_text(s: str) -> str:
    return (s or "").replace("−", "-").replace("–", "-").replace("—", "-")


def _parse_float(value: str) -> Optional[float]:
    m = re.search(r"[+-]?\d+(?:\.\d+)?", _clean_numeric_text(value or ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def score_pair(match: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    hs = parse_int(match.get("home_score"))
    aw = parse_int(match.get("away_score"))
    if hs is None or aw is None:
        return None
    return hs, aw


def first_half_pair(match: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    hp = match.get("home_parts") or []
    ap = match.get("away_parts") or []
    if not hp or not ap:
        return None
    hs = parse_int(hp[0])
    aw = parse_int(ap[0])
    if hs is None or aw is None:
        return None
    return hs, aw


def extract_total_line(question: str) -> Optional[float]:
    q = _clean_numeric_text(question or "")
    patterns = [
        r"\bO\s*/\s*U\s*([0-9]+(?:\.[0-9]+)?)",
        r"\bOver\s*/\s*Under\s*([0-9]+(?:\.[0-9]+)?)",
        r"\bTotal\s*(?:Goals?|Points?)?\s*[:=-]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"\bTotal\s*(?:Goals?|Points?)?\s*[:=-]?\s*(?:Over|Under)\s*([0-9]+(?:\.[0-9]+)?)",
        r"\b(?:Over|Under)\s*([0-9]+(?:\.[0-9]+)?)(?:\s*(?:Goals?|Points?))?",
        r"\b(?:Goals?|Points?)\s*[:=-]?\s*(?:Over|Under)\s*([0-9]+(?:\.[0-9]+)?)",
        r"\b(?:Goals?|Points?)\s*[:=-]?\s*O\s*/\s*U\s*([0-9]+(?:\.[0-9]+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, q, flags=re.I)
        if m:
            return _parse_float(m.group(1))
    return None


def extract_spread(question: str, home: str = "", away: str = "") -> Optional[Tuple[str, float]]:
    q = _clean_numeric_text(question or "")
    patterns = [
        # Spread: Team (-1.5), Handicap: Team +1.5, Asian Handicap: Team (-0.25)
        r"\b(?:Spread|Handicap|Asian\s+Handicap|Goal\s*Line|Run\s*Line)\s*:\s*(.+?)\s*\(?\s*([+-]?\d+(?:\.\d+)?)\s*\)?(?:\b|$)",
        # Team -1.5 spread / Team +1.5 handicap
        r"^\s*(.+?)\s*\(?\s*([+-]\d+(?:\.\d+)?)\s*\)?\s*(?:spread|handicap|asian\s+handicap|goal\s*line)\b",
    ]
    for pat in patterns:
        m = re.search(pat, q, flags=re.I)
        if not m:
            continue
        team = collapse_ws(m.group(1))
        line = _parse_float(m.group(2))
        if team and line is not None:
            return team, line

    # If the line is present but the team is only implied by the market title,
    # map the title to whichever supplied team name appears most strongly.
    m_line = re.search(r"([+-]\d+(?:\.\d+)?)", q)
    if m_line:
        line = _parse_float(m_line.group(1))
        h = team_similarity(home, q) if home else 0.0
        a = team_similarity(away, q) if away else 0.0
        if line is not None and max(h, a) >= 0.70:
            return (home if h >= a else away), line
    return None


def _side_from_team_name(name: str, home: str, away: str) -> Optional[str]:
    if not name:
        return None
    h = team_similarity(name, home)
    a = team_similarity(name, away)
    if h >= 0.70 and h >= a:
        return "home"
    if a >= 0.70:
        return "away"
    return None


def _opposite_side(side: str) -> str:
    return "away" if side == "home" else "home"


def _side_name(side: str, home: str, away: str) -> str:
    return home if side == "home" else away


def evaluate_total_market(match: Dict[str, Any], proposed: str, question: str) -> Tuple[Optional[bool], str]:
    scores = score_pair(match)
    if not scores:
        return None, "total market needs a parseable final score"
    if not is_final_status(str(match.get("status") or "")):
        return None, "matched Flashscore row, but total market is not final yet"
    line = extract_total_line(question)
    if line is None:
        return None, "total market needs an O/U line in the question/title"
    hs, aw = scores
    total = hs + aw
    p = norm_text(proposed)
    if p not in {"over", "under"}:
        return None, "total market needs proposed=Over or proposed=Under"
    if abs(float(total) - line) < 1e-9:
        return None, "total landed exactly on the line; treating as push/unsupported"
    won = total > line if p == "over" else total < line
    return won, "final score %s-%s gives total=%s vs line %.3g; proposed=%s" % (hs, aw, total, line, proposed)


def evaluate_spread_market(match: Dict[str, Any], proposed: str, question: str) -> Tuple[Optional[bool], str]:
    scores = score_pair(match)
    if not scores:
        return None, "spread market needs a parseable final score"
    if not is_final_status(str(match.get("status") or "")):
        return None, "matched Flashscore row, but spread market is not final yet"
    parsed = extract_spread(question, home=str(match.get("home") or ""), away=str(match.get("away") or ""))
    if not parsed:
        return None, "spread/handicap market needs a line in the question/title, e.g. Spread: Team (-1.5)"
    spread_team, line = parsed
    home = str(match.get("home") or "")
    away = str(match.get("away") or "")
    spread_side = _side_from_team_name(spread_team, home, away)
    if not spread_side:
        return None, "could not map spread team %r to Flashscore home/away teams" % spread_team
    proposed_side = _side_from_team_name(proposed, home, away)
    if not proposed_side:
        if is_yes(proposed):
            proposed_side = spread_side
        elif is_no(proposed):
            proposed_side = _opposite_side(spread_side)
        else:
            return None, "could not map proposed side %r to Flashscore home/away teams" % proposed
    hs, aw = scores
    spread_score = hs if spread_side == "home" else aw
    other_score = aw if spread_side == "home" else hs
    adjusted_margin = float(spread_score) + float(line) - float(other_score)
    if abs(adjusted_margin) < 1e-9:
        return None, "spread landed exactly on the line; treating as push/unsupported"
    spread_covers = adjusted_margin > 0
    proposed_won = spread_covers if proposed_side == spread_side else (not spread_covers)
    return proposed_won, "%s %.3g cover=%s from final score %s-%s; proposed side=%s" % (
        _side_name(spread_side, home, away), line, spread_covers, hs, aw, _side_name(proposed_side, home, away)
    )


def extract_halftime_target(question: str, home: str, away: str) -> Optional[str]:
    raw = question or ""
    m = re.search(r"(.+?)\s+(?:leading|lead)\s+at\s+half\s*time", raw, flags=re.I)
    target = collapse_ws(m.group(1)) if m else ""
    side = _side_from_team_name(target, home, away) if target else None
    if side:
        return side
    # Fallback: whichever team name is more present in the full question.
    h = team_similarity(home, raw)
    a = team_similarity(away, raw)
    if h >= 0.70 and h >= a:
        return "home"
    if a >= 0.70:
        return "away"
    return None


def evaluate_halftime_leader_market(match: Dict[str, Any], proposed: str, question: str) -> Tuple[Optional[bool], str]:
    fh = first_half_pair(match)
    if not fh:
        return None, "halftime leader market needs first-half score proof from Flashscore HTTP periods"
    home = str(match.get("home") or "")
    away = str(match.get("away") or "")
    target_side = extract_halftime_target(question, home, away)
    if not target_side:
        return None, "could not map halftime-leading target team from question/title"
    if not (is_yes(proposed) or is_no(proposed)):
        return None, "halftime leader market needs proposed=Yes or proposed=No"
    h1, a1 = fh
    target_leading = h1 > a1 if target_side == "home" else a1 > h1
    proposed_won = target_leading if is_yes(proposed) else (not target_leading)
    return proposed_won, "first-half score %s-%s; target=%s leading=%s; proposed=%s" % (
        h1, a1, _side_name(target_side, home, away), target_leading, proposed
    )


def evaluate_score_market(match: Dict[str, Any], proposed: str, question: str, market_type: str) -> Tuple[Optional[bool], str]:
    mt = norm_market_type(market_type)
    if mt == "total":
        return evaluate_total_market(match, proposed, question)
    if mt == "spread":
        return evaluate_spread_market(match, proposed, question)
    if mt == "halftime_leader_binary":
        return evaluate_halftime_leader_market(match, proposed, question)
    return None, "not a score-prop market"


def _cache_get(key: str) -> Optional[Any]:
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: Any) -> Any:
    _cache[key] = (time.time(), val)
    return val


def build_feed_urls(domain_key: str = "com", date: Optional[str] = None) -> List[str]:
    base = DOMAINS.get(domain_key, DOMAINS["com"])
    out = []
    for path in FEED_CANDIDATES:
        url = base.rstrip("/") + path
        if date:
            # Flashscore sometimes accepts date as query param. If ignored, debug output still shows today's feed.
            url += "?d=" + quote(date)
        out.append(url)
    return out


def build_html_urls(domain_key: str = "com", date: Optional[str] = None) -> List[str]:
    urls = []
    for dk in ([domain_key] if domain_key in DOMAINS else ["com", "usa"]):
        base = DOMAINS[dk]
        url = base.rstrip("/") + sport_path(dk)
        if date:
            url += "?d=" + quote(date)
        urls.append(url)
    return urls


async def fetch_text(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            r = await client.get(url, headers=headers or HEADERS)
        text = r.text or ""
        return {
            "ok": 200 <= r.status_code < 300,
            "status_code": r.status_code,
            "url": str(r.url),
            "requested_url": url,
            "headers_used": {"user-agent": (headers or HEADERS).get("User-Agent", "")[:80], "x-fsign": (headers or HEADERS).get("x-fsign", "")},
            "content_type": r.headers.get("content-type", ""),
            "content_length": len(text),
            "text": text,
        }
    except Exception as e:
        return {
            "ok": False,
            "status_code": None,
            "url": url,
            "requested_url": url,
            "error": type(e).__name__ + ": " + str(e),
            "content_length": 0,
            "text": "",
        }


def parse_feed_fields(section: str) -> Dict[str, str]:
    section = section.replace("\u00ac", "¬").replace("\u00f7", "÷")
    fields: Dict[str, str] = {}
    for piece in section.split("¬"):
        if "÷" not in piece:
            continue
        key, value = piece.split("÷", 1)
        key = key.strip()
        value = html.unescape(value.strip())
        if key:
            fields[key] = value
    return fields


def _looks_like_team_name(v: str) -> bool:
    n = norm_text(v)
    if not n or len(n) < 2:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", n):
        return False
    bad = {"live", "finished", "scheduled", "loading", "soccer", "football", "odds", "draw", "standings", "table", "fixtures", "results"}
    if n in bad:
        return False
    # Names usually contain letters and are not very long.
    return bool(re.search(r"[a-z]", n)) and len(n) <= 90


def _first_present(fields: Dict[str, str], keys: Iterable[str]) -> str:
    for k in keys:
        v = fields.get(k)
        if v not in (None, ""):
            return v
    return ""


def parse_flashscore_feed(raw: str, source_url: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    text = (raw or "").replace("\u00ac", "¬").replace("\u00f7", "÷")
    sections = [s for s in text.split("~") if s.strip()]
    matches: List[Dict[str, Any]] = []
    current_tournament = ""
    debug_sections = []

    for sec in sections:
        fields = parse_feed_fields(sec)
        if not fields:
            continue

        if len(debug_sections) < 12:
            debug_sections.append({k: fields[k] for k in list(fields.keys())[:20]})

        # Tournament/category rows carry ZA/ZAF/ZL. Match rows carry AE/AF team names.
        # Keep the current tournament/competition from ZA rows and use it only as a ranking bonus.
        tournament_row_name = _first_present(fields, ["ZA", "TNAME", "OAI"])
        tournament_row_category = _first_present(fields, ["ZAF", "ZL"])
        is_match_row = bool(fields.get("AA") or fields.get("AE") or fields.get("AF"))
        if tournament_row_name and not is_match_row:
            current_tournament = collapse_ws(tournament_row_name)
            continue
        if tournament_row_name and not current_tournament:
            current_tournament = collapse_ws(tournament_row_name)

        # Common Flashscore mapping: AE/AF teams, AG/AH aggregate scores.
        home = _first_present(fields, ["AE", "WN", "WJ", "HOME_NAME", "H"])
        away = _first_present(fields, ["AF", "WM", "WK", "AWAY_NAME", "A"])

        # Fallback: pick the first two name-like values from the section.
        if not home or not away:
            likely = []
            for k, v in fields.items():
                if k in {"ZA", "ZB", "ZC", "CX", "OAI", "AA", "AD", "ADE", "AC", "AB", "AG", "AH"}:
                    continue
                if _looks_like_team_name(v):
                    likely.append((k, v))
            if len(likely) >= 2:
                home = home or likely[0][1]
                away = away or likely[1][1]

        if not home or not away:
            continue
        if not _looks_like_team_name(home) or not _looks_like_team_name(away):
            continue

        # Aggregate score and period score parts. Common pairs are BA/BB, BC/BD, ... but
        # Flashscore has changed names before, so keep raw_fields in debug endpoints.
        home_score = _first_present(fields, ["AG", "HG", "HOME_SCORE"])
        away_score = _first_present(fields, ["AH", "IG", "AWAY_SCORE"])
        set_pairs = [("BA", "BB"), ("BC", "BD"), ("BE", "BF"), ("BG", "BH"), ("BI", "BJ"), ("BK", "BL")]
        home_parts = []
        away_parts = []
        for hk, ak in set_pairs:
            hv = fields.get(hk, "")
            av = fields.get(ak, "")
            if hv != "" or av != "":
                home_parts.append(hv)
                away_parts.append(av)

        status = status_from_fields(fields)
        item = {
            "source": "xfeed",
            "source_url": source_url,
            "id": fields.get("AA") or fields.get("ID") or "",
            "timestamp": parse_int(fields.get("AD")),
            "tournament": collapse_ws(current_tournament),
            "status": status,
            "home": collapse_ws(home),
            "away": collapse_ws(away),
            "home_score": collapse_ws(home_score),
            "away_score": collapse_ws(away_score),
            "home_parts": home_parts,
            "away_parts": away_parts,
            "href": ("https://www.flashscore.com/match/" + fields.get("AA", "") + "/") if fields.get("AA") else "",
            "raw_status_fields": {k: v for k, v in fields.items() if k in {"AC", "AB", "AO", "AX", "AD", "ADE"}},
            "raw_fields_sample": {k: fields[k] for k in list(fields.keys())[:30]},
        }
        winner, basis = determine_winner(item)
        item["winner"] = winner
        item["winner_basis"] = basis
        matches.append(item)

    matches = dedupe_matches(matches)
    meta = {
        "parser": "flashscore_xfeed",
        "section_count": len(sections),
        "match_count": len(matches),
        "debug_sections": debug_sections,
    }
    return matches, meta


def parse_flashscore_html(raw: str, source_url: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    # This is intentionally conservative. Static Flashscore HTML usually has SEO links
    # and tournament names, but not full rendered scoreboard rows. Use it as a fallback
    # to prove reachability / find candidate match links, not as a trusted score verifier.
    extractor = TextExtractor()
    try:
        extractor.feed(raw or "")
    except Exception:
        pass
    text = extractor.text()

    anchors = AnchorExtractor()
    try:
        anchors.feed(raw or "")
    except Exception:
        pass

    matches: List[Dict[str, Any]] = []
    seen = set()
    # SEO lines can look like "01.06. Cobolli Flavio - Svajda Zachary".
    pattern = re.compile(r"(?:\b\d{1,2}\.\d{1,2}\.\s*)?([A-Z][A-Za-zÀ-ÿ' .-]{2,60})\s+-\s+([A-Z][A-Za-zÀ-ÿ' .-]{2,60})")
    for m in pattern.finditer(text):
        home = collapse_ws(m.group(1))
        away = collapse_ws(m.group(2))
        key = (norm_text(home), norm_text(away))
        if key in seen:
            continue
        seen.add(key)
        if len(home) > 60 or len(away) > 60:
            continue
        matches.append({
            "source": "html_seo",
            "source_url": source_url,
            "id": "",
            "timestamp": None,
            "tournament": "",
            "status": "unknown/html-seo-only",
            "home": home,
            "away": away,
            "home_score": "",
            "away_score": "",
            "home_parts": [],
            "away_parts": [],
            "winner": None,
            "winner_basis": "html seo has no winner proof",
            "href": "",
        })
        if len(matches) >= MAX_MATCHES_DEFAULT:
            break

    meta = {
        "parser": "flashscore_html_seo",
        "visible_text_chars": len(text),
        "anchor_count": len(anchors.anchors),
        "sample_text": text[:DEBUG_FETCH_CHARS],
        "sample_anchors": anchors.anchors[:25],
        "match_count": len(matches),
    }
    return matches, meta


def dedupe_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for m in matches:
        key = m.get("id") or "|".join([
            norm_text(str(m.get("tournament") or "")),
            norm_text(str(m.get("home") or "")),
            norm_text(str(m.get("away") or "")),
            str(m.get("timestamp") or ""),
        ])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


async def fetch_flashscore_matches(
    date: Optional[str] = None,
    source: str = "auto",
    domain: str = "com",
    max_results: int = MAX_MATCHES_DEFAULT,
    stop_after_first_success: bool = True,
) -> Dict[str, Any]:
    cache_key = json.dumps({
        "date": date,
        "source": source,
        "domain": domain,
        "max_results": max_results,
        "stop_after_first_success": stop_after_first_success,
    }, sort_keys=True)
    cached = _cache_get(cache_key)
    if cached is not None:
        result = dict(cached)
        result["cached"] = True
        return result

    attempts: List[Dict[str, Any]] = []
    all_matches: List[Dict[str, Any]] = []
    parser_meta: List[Dict[str, Any]] = []

    sources_to_try = []
    if source in ("auto", "feed", "xfeed"):
        sources_to_try.append("feed")
    if source in ("auto", "html"):
        sources_to_try.append("html")

    domains_to_try = [domain] if domain in DOMAINS else ["com", "usa"]

    for src in sources_to_try:
        for dk in domains_to_try:
            urls = build_feed_urls(dk, date) if src == "feed" else build_html_urls(dk, date)
            for url in urls:
                profiles = HEADER_PROFILES if src == "feed" else [("desktop", dict(HEADERS, Accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")), ("googlebot", HEADER_PROFILES[2][1])]
                for profile_name, headers in profiles:
                    # Adjust origin/referer to selected domain.
                    base = DOMAINS.get(dk, DOMAINS["com"])
                    h = dict(headers)
                    h["Origin"] = base
                    h["Referer"] = base.rstrip("/") + sport_path(dk)
                    got = await fetch_text(url, headers=h)
                    text = got.pop("text", "")
                    attempt = dict(got)
                    attempt.update({"source": src, "domain": dk, "profile": profile_name, "sample": text[:300]})
                    attempts.append(attempt)
                    if not got.get("ok") or not text:
                        continue

                    if src == "feed":
                        matches, meta = parse_flashscore_feed(text, source_url=got.get("url") or url)
                    else:
                        matches, meta = parse_flashscore_html(text, source_url=got.get("url") or url)
                    meta.update({"source": src, "domain": dk, "profile": profile_name, "url": got.get("url") or url})
                    parser_meta.append(meta)
                    if matches:
                        all_matches.extend(matches)
                        # Normal list/debug mode stops after the first useful feed to keep Vercel cheap.
                        # Details/verifier mode can set stop_after_first_success=False to try every
                        # feed candidate because finished/live/scheduled soccer rows can live in a
                        # different f_1_0_X bucket than the first successful feed.
                        if src == "feed" and stop_after_first_success:
                            all_matches = dedupe_matches(all_matches)[:max_results]
                            result = {
                                "ok": True,
                                "cached": False,
                                "source": src,
                                "domain": dk,
                                "date": date,
                                "match_count": len(all_matches),
                                "matches": all_matches,
                                "attempts": attempts,
                                "parser_meta": parser_meta,
                            }
                            return _cache_set(cache_key, result)

    all_matches = dedupe_matches(all_matches)[:max_results]
    result = {
        "ok": bool(all_matches),
        "cached": False,
        "source": source,
        "domain": domain,
        "date": date,
        "match_count": len(all_matches),
        "matches": all_matches,
        "attempts": attempts,
        "parser_meta": parser_meta,
        "note": "If match_count is 0, inspect /v2/debug/fetch. Flashscore may have blocked serverless HTTP or changed the feed format.",
    }
    return _cache_set(cache_key, result)


def choose_match(matches: List[Dict[str, Any]], home: str, away: str, tournament: str = "") -> Tuple[Optional[Dict[str, Any]], List[Tuple[int, Dict[str, Any]]]]:
    """Pick the requested soccer row.

    Competition text is a bonus, not a hard gate. Flashscore sometimes exposes
    league/cup labels differently than Polymarket. Exact team-pair matches
    should still show up in diagnostics even when competition text is absent/wrong.
    """
    tournament_norm = norm_text(tournament)
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    for m in matches:
        pair_score, reversed_order = pair_match_score(home, away, str(m.get("home") or ""), str(m.get("away") or ""))
        mt = norm_text(str(m.get("tournament") or ""))
        tournament_ok = bool(tournament_norm and mt and (tournament_norm in mt or mt in tournament_norm))

        # Keep all non-zero team candidates.  Strong pair matches are 84+; competition
        # match adds 10 so a league-specific row wins over a duplicate row.
        score = pair_score + (10 if tournament_ok else 0)
        if score <= 0:
            continue

        mm = dict(m)
        if reversed_order:
            mm["_target_order_reversed"] = True
        if tournament_norm:
            mm["_tournament_filter_matched"] = tournament_ok
        candidates.append((score, mm))

    candidates.sort(key=lambda x: x[0], reverse=True)
    if candidates and candidates[0][0] >= 84:
        return candidates[0][1], candidates
    return None, candidates


def compact_match(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": m.get("source"),
        "tournament": m.get("tournament"),
        "status": m.get("status"),
        "home": m.get("home"),
        "away": m.get("away"),
        "score": "%s-%s" % (m.get("home_score") or "", m.get("away_score") or ""),
        "home_periods": " ".join([str(x) for x in m.get("home_parts") or [] if str(x) != ""]),
        "away_periods": " ".join([str(x) for x in m.get("away_parts") or [] if str(x) != ""]),
        "winner": m.get("winner"),
        "winner_basis": m.get("winner_basis"),
        "href": m.get("href"),
        "id": m.get("id"),
    }


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flashscore Soccer API Test</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; color: #111; }
    input, select, button { padding: 8px; margin: 4px; font-size: 14px; }
    pre { background: #f4f4f4; padding: 12px; border-radius: 8px; overflow: auto; }
    .row { margin-bottom: 8px; }
  </style>
</head>
<body>
  <h2>Flashscore Soccer HTTP Test API</h2>
  <p>Tests the same kind of verifier endpoint you can call from propose.py without using Playwright.</p>
  <div class="row"><input id="date" value="2026-05-31" /> date</div>
  <div class="row"><input id="tournament" value="FIFA Friendly" /> tournament</div>
  <div class="row"><input id="home" value="Poland" /> home/team 1</div>
  <div class="row"><input id="away" value="Ukraine" /> away/team 2</div>
  <div class="row"><input id="proposed" value="Ukraine" /> proposed side</div>
  <div class="row"><input id="question" value="Poland vs. Ukraine" /> optional question/title</div>
  <div class="row"><input id="market_type" value="auto" /> market_type: auto is usually enough; optional override: moneyline, draw_binary, home_win_binary, away_win_binary, total, spread/handicap, halftime_leader_binary</div>
  <div class="row">
    <select id="source"><option>auto</option><option>feed</option><option>html</option></select>
    <select id="domain"><option>com</option><option>both</option><option>usa</option></select>
    <label><input type="checkbox" id="exhaustive" checked /> exhaustive</label>
    <button onclick="runDetails()">Verify details</button>
    <button onclick="runList()">List matches</button>
    <button onclick="runDebug()">Debug fetch</button>
    <button onclick="runSearch()">Raw search teams</button>
  </div>
  <pre id="out">Ready.</pre>
  <script>
    async function getJson(path) {
      const r = await fetch(path);
      const j = await r.json();
      document.getElementById('out').textContent = JSON.stringify(j, null, 2);
    }
    function qs() {
      const p = new URLSearchParams();
      for (const id of ['date','tournament','home','away','proposed','question','market_type','source','domain']) p.set(id, document.getElementById(id).value);
      p.set('exhaustive', document.getElementById('exhaustive').checked ? 'true' : 'false');
      return p.toString();
    }
    function runDetails() { getJson('/v2/soccer/details?' + qs()); }
    function runList() { getJson('/v2/soccer?date=' + encodeURIComponent(date.value) + '&source=' + source.value + '&domain=' + domain.value + '&exhaustive=' + (exhaustive.checked ? 'true' : 'false')); }
    function runDebug() { getJson('/v2/debug/fetch?date=' + encodeURIComponent(date.value) + '&source=' + source.value + '&domain=' + domain.value + '&exhaustive=' + (exhaustive.checked ? 'true' : 'false')); }
    function runSearch() { getJson('/v2/debug/search?date=' + encodeURIComponent(date.value) + '&source=feed&domain=' + domain.value + '&q=' + encodeURIComponent(home.value.split(' ').slice(-1)[0])); }
  </script>
</body>
</html>
"""


@app.get("/v2/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "flashscore-soccer-http-test-api",
        "version": "1.2.0",
        "python_compatible": "3.9+",
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "x_fsign_set": bool(FLASHSCORE_X_FSIGN),
        "endpoints": [
            "/v2/soccer",
            "/v2/soccer/details",
            "/v2/debug/fetch",
            "/v2/debug/raw",
            "/v2/debug/search",
        ],
        "fixes": [
            "competition parser preserves ZA tournament/league rows",
            "details endpoint can exhaust all f_1_0_X feed candidates",
            "competition filter is diagnostic/bonus instead of hiding exact team-pair matches",
            "CS/Sporting club-name aliases are normalized for soccer matching",
            "market_type=auto detects totals, spreads/handicaps, and halftime-leading binary markets from question/title",
        ],
    }


@app.get("/v2/soccer")
async def soccer(
    date: Optional[str] = Query(None, description="Optional YYYY-MM-DD. If omitted, Flashscore default/today is used."),
    source: str = Query("auto", description="auto, feed, or html"),
    domain: str = Query("com", description="com, usa, or both"),
    max_results: int = Query(MAX_MATCHES_DEFAULT, ge=1, le=1000),
    exhaustive: bool = Query(False, description="Try all feed candidates instead of stopping after first successful feed."),
) -> Dict[str, Any]:
    if source not in {"auto", "feed", "xfeed", "html"}:
        raise HTTPException(400, "source must be auto, feed, xfeed, or html")
    if domain not in {"com", "usa", "both"}:
        raise HTTPException(400, "domain must be com, usa, or both")
    return await fetch_flashscore_matches(date=date, source=source, domain=domain, max_results=max_results, stop_after_first_success=not exhaustive)


@app.get("/v2/soccer/details")
async def soccer_details(
    home: str = Query(..., description="Market home/team 1 full name, e.g. Poland"),
    away: str = Query(..., description="Market away/team 2 full name, e.g. Ukraine"),
    proposed: str = Query(..., description="Proposed side: home team, away team, Draw, Yes, or No"),
    tournament: str = Query("", description="Optional competition/league filter, e.g. FIFA Friendly"),
    question: str = Query("", description="Optional full Polymarket question/title for auto-detecting Yes/No draw/team markets."),
    market_type: str = Query("auto", description="auto is usually enough; optional override: moneyline, draw_binary, home_win_binary, away_win_binary, total, spread/handicap, or halftime_leader_binary"),
    date: Optional[str] = Query(None, description="Optional YYYY-MM-DD"),
    source: str = Query("auto", description="auto, feed, or html"),
    domain: str = Query("com", description="com, usa, or both"),
    max_results: int = Query(MAX_MATCHES_DEFAULT, ge=1, le=1000),
    exhaustive: bool = Query(True, description="Try every feed bucket before returning not_found."),
) -> Dict[str, Any]:
    data = await fetch_flashscore_matches(
        date=date,
        source=source,
        domain=domain,
        max_results=max_results,
        stop_after_first_success=not exhaustive,
    )
    matches = data.get("matches") or []
    match, candidates = choose_match(matches, home, away, tournament=tournament)
    nearest = []
    for score, m in candidates[:12]:
        c = compact_match(m)
        c["pair_score"] = score
        nearest.append(c)

    if not match:
        return {
            "verdict": "not_found",
            "verified": False,
            "safe_to_buy_yes": False,
            "reason": "No Flashscore HTTP row matched the requested teams/competition.",
            "target": {"home": home, "away": away, "proposed": proposed, "question": question, "market_type": market_type, "tournament": tournament, "date": date},
            "fetch_ok": data.get("ok"),
            "match_count": data.get("match_count"),
            "nearest": nearest,
            "attempts": data.get("attempts", [])[:10],
            "parser_meta": data.get("parser_meta", [])[:3],
        }

    matched = compact_match(match)
    resolved_market_type = infer_market_type(question=question, proposed=proposed, home=home, away=away, requested=market_type)

    if resolved_market_type in {"total", "spread", "halftime_leader_binary"}:
        proposed_won, proposal_basis = evaluate_score_market(
            match=match,
            proposed=proposed,
            question=question,
            market_type=resolved_market_type,
        )
        if proposed_won is None:
            return {
                "verdict": "unsupported_or_not_ready",
                "verified": False,
                "safe_to_buy_yes": False,
                "reason": proposal_basis,
                "target": {"home": home, "away": away, "proposed": proposed, "question": question, "market_type": market_type, "resolved_market_type": resolved_market_type, "tournament": tournament, "date": date},
                "matched": matched,
                "nearest": nearest,
                "attempts": data.get("attempts", [])[:10],
            }
        return {
            "verdict": "verified_win" if proposed_won else "verified_loss",
            "verified": True,
            "safe_to_buy_yes": bool(proposed_won),
            "reason": "Flashscore HTTP matched soccer row. market_type=%s; %s." % (resolved_market_type, proposal_basis),
            "target": {"home": home, "away": away, "proposed": proposed, "question": question, "market_type": market_type, "resolved_market_type": resolved_market_type, "tournament": tournament, "date": date},
            "matched": matched,
            "nearest": nearest,
            "attempts": data.get("attempts", [])[:10],
        }

    winner, basis = determine_winner(match)
    if not winner:
        return {
            "verdict": "not_final_or_no_winner_proof",
            "verified": False,
            "safe_to_buy_yes": False,
            "reason": "Matched the Flashscore row, but could not prove a final winner from HTTP data yet.",
            "target": {"home": home, "away": away, "proposed": proposed, "question": question, "market_type": market_type, "resolved_market_type": resolved_market_type, "tournament": tournament, "date": date},
            "matched": matched,
            "nearest": nearest,
            "attempts": data.get("attempts", [])[:10],
        }

    proposed_won, proposal_basis = proposed_matches_winner(
        proposed=proposed,
        winner=winner,
        home=matched.get("home") or home,
        away=matched.get("away") or away,
        market_type=resolved_market_type,
    )
    if proposed_won is None:
        return {
            "verdict": "unsupported_proposed_side",
            "verified": False,
            "safe_to_buy_yes": False,
            "reason": proposal_basis,
            "target": {"home": home, "away": away, "proposed": proposed, "question": question, "market_type": market_type, "resolved_market_type": resolved_market_type, "tournament": tournament, "date": date},
            "matched": matched,
            "nearest": nearest,
            "attempts": data.get("attempts", [])[:10],
        }

    return {
        "verdict": "verified_win" if proposed_won else "verified_loss",
        "verified": True,
        "safe_to_buy_yes": bool(proposed_won),
        "reason": "Flashscore HTTP matched soccer row. Winner=%r by %s; proposed=%r; market_type=%s; %s." % (winner, basis, proposed, resolved_market_type, proposal_basis),
        "target": {"home": home, "away": away, "proposed": proposed, "question": question, "market_type": market_type, "resolved_market_type": resolved_market_type, "tournament": tournament, "date": date},
        "matched": matched,
        "nearest": nearest,
        "attempts": data.get("attempts", [])[:10],
    }


@app.get("/v2/debug/fetch")
async def debug_fetch(
    date: Optional[str] = Query(None),
    source: str = Query("feed", description="feed, html, or auto"),
    domain: str = Query("com", description="com, usa, or both"),
    exhaustive: bool = Query(False, description="Try every feed candidate."),
) -> Dict[str, Any]:
    # Same fetch path as normal endpoint, but it returns attempts and parser metadata.
    return await fetch_flashscore_matches(date=date, source=source, domain=domain, max_results=50, stop_after_first_success=not exhaustive)


@app.get("/v2/debug/raw")
async def debug_raw(
    url: Optional[str] = Query(None, description="Optional exact URL to fetch"),
    date: Optional[str] = Query(None),
    source: str = Query("feed", description="feed or html"),
    domain: str = Query("com", description="com or usa"),
    profile: str = Query("desktop", description="desktop, flashscoreusa, or googlebot"),
) -> Dict[str, Any]:
    if url:
        urls = [url]
    else:
        urls = build_feed_urls(domain, date)[:1] if source in {"feed", "xfeed"} else build_html_urls(domain, date)[:1]
    profiles = dict(HEADER_PROFILES)
    headers = dict(profiles.get(profile, HEADERS))
    base = DOMAINS.get(domain, DOMAINS["com"])
    headers["Origin"] = base
    headers["Referer"] = base.rstrip("/") + sport_path(domain)
    got = await fetch_text(urls[0], headers=headers)
    text = got.pop("text", "")
    return {
        **got,
        "source": source,
        "profile": profile,
        "sample_start": text[:DEBUG_FETCH_CHARS],
        "sample_end": text[-DEBUG_FETCH_CHARS:] if len(text) > DEBUG_FETCH_CHARS else "",
        "contains_delimiters": {"tilde": "~" in text, "section": "¬" in text, "field": "÷" in text},
    }


@app.get("/v2/debug/search")
async def debug_search(
    q: str = Query(..., description="Raw text to search for, e.g. Ukraine or FIFA Friendly"),
    date: Optional[str] = Query(None),
    source: str = Query("feed", description="feed or html"),
    domain: str = Query("com", description="com, usa, or both"),
    profile: str = Query("desktop", description="desktop, flashscoreusa, or googlebot"),
    context_chars: int = Query(220, ge=20, le=1000),
) -> Dict[str, Any]:
    """Search raw Flashscore responses before parsing.

    This is the fastest way to answer: did Flashscore return this competition/team
    at all, or did our parser/filter miss it?
    """
    domains_to_try = [domain] if domain in DOMAINS else ["com", "usa"]
    profiles = dict(HEADER_PROFILES)
    headers_template = dict(profiles.get(profile, HEADERS))
    needle = q or ""
    needle_l = strip_accents(needle).lower()
    attempts: List[Dict[str, Any]] = []
    hits: List[Dict[str, Any]] = []

    for dk in domains_to_try:
        urls = build_feed_urls(dk, date) if source in {"feed", "xfeed"} else build_html_urls(dk, date)
        base = DOMAINS.get(dk, DOMAINS["com"])
        for url in urls:
            h = dict(headers_template)
            h["Origin"] = base
            h["Referer"] = base.rstrip("/") + sport_path(dk)
            got = await fetch_text(url, headers=h)
            text = got.pop("text", "")
            text_norm = strip_accents(text).lower()
            idxs = []
            start = 0
            while needle_l and True:
                idx = text_norm.find(needle_l, start)
                if idx < 0:
                    break
                idxs.append(idx)
                start = idx + max(1, len(needle_l))
                if len(idxs) >= 20:
                    break
            attempt = dict(got)
            attempt.update({"source": source, "domain": dk, "profile": profile, "hit_count": len(idxs)})
            attempts.append(attempt)
            for idx in idxs[:10]:
                lo = max(0, idx - context_chars)
                hi = min(len(text), idx + len(needle) + context_chars)
                hits.append({
                    "url": got.get("url") or url,
                    "domain": dk,
                    "profile": profile,
                    "index": idx,
                    "context": text[lo:hi],
                })

    return {
        "ok": bool(hits),
        "query": q,
        "date": date,
        "source": source,
        "domain": domain,
        "hit_count": len(hits),
        "hits": hits,
        "attempts": attempts,
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT, reload=False)
