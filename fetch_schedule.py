"""Pull today-plus window from the NBA schedule file and write schedule.json.

GitHub Actions runs this at 10:00, 16:00, and 21:00 UTC, and once on demand.
A failed pull leaves the previous schedule.json in place.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

NBA_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
ESPN_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates="
)
OUT = Path(__file__).with_name("schedule.json")

CODES = {
    "ATL": "ATX",
    "BOS": "BTN",
    "BKN": "BKL",
    "CHA": "CHT",
    "CHI": "CGO",
    "CLE": "CLV",
    "DAL": "DLS",
    "DEN": "DVR",
    "DET": "DTR",
    "GSW": "SFR",
    "GS": "SFR",
    "HOU": "HST",
    "IND": "IPS",
    "LAC": "LAX",
    "LAL": "LAG",
    "MEM": "MPS",
    "MIA": "MMI",
    "MIL": "MLK",
    "MIN": "MPL",
    "NOP": "NOL",
    "NO": "NOL",
    "NYK": "NYC",
    "NY": "NYC",
    "OKC": "OKL",
    "ORL": "ORL",
    "PHI": "PHD",
    "PHX": "PXN",
    "PHO": "PXN",
    "POR": "PTL",
    "SAC": "SMT",
    "SAS": "SAT",
    "SA": "SAT",
    "TOR": "TNT",
    "UTA": "SLC",
    "UTAH": "SLC",
    "WAS": "WDC",
    "WSH": "WDC",
}

ZONES = {
    "ATX": "America/New_York",
    "BTN": "America/New_York",
    "BKL": "America/New_York",
    "CHT": "America/New_York",
    "CGO": "America/Chicago",
    "CLV": "America/New_York",
    "DLS": "America/Chicago",
    "DVR": "America/Denver",
    "DTR": "America/Detroit",
    "SFR": "America/Los_Angeles",
    "HST": "America/Chicago",
    "IPS": "America/Indiana/Indianapolis",
    "LAX": "America/Los_Angeles",
    "LAG": "America/Los_Angeles",
    "MPS": "America/Chicago",
    "MMI": "America/New_York",
    "MLK": "America/Chicago",
    "MPL": "America/Chicago",
    "NOL": "America/Chicago",
    "NYC": "America/New_York",
    "OKL": "America/Chicago",
    "ORL": "America/New_York",
    "PHD": "America/New_York",
    "PXN": "America/Phoenix",
    "PTL": "America/Los_Angeles",
    "SMT": "America/Los_Angeles",
    "SAT": "America/Chicago",
    "TNT": "America/Toronto",
    "SLC": "America/Denver",
    "WDC": "America/New_York",
}


def main() -> int:
    now = datetime.now(timezone.utc)
    start, end = window(now)
    try:
        games = load(start, end)
    except Exception as error:  # noqa: BLE001 - keep the last good file
        print(f"schedule pull failed: {error}", file=sys.stderr)
        return 0 if OUT.exists() else 1

    payload = {
        "generatedAt": iso(now),
        "windowStart": iso(start),
        "windowEnd": iso(end),
        "games": games,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(games)} games")
    return 0


def window(now: datetime) -> tuple[datetime, datetime]:
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def load(start: datetime, end: datetime) -> list[dict]:
    nba_error = None
    try:
        body = get(NBA_URL, referer="https://www.nba.com/")
        return from_nba(json.loads(body), start, end)
    except Exception as error:  # noqa: BLE001
        nba_error = error

    try:
        games = []
        seen = set()
        day = start
        while day < end:
            body = get(ESPN_URL + day.strftime("%Y%m%d"))
            for game in from_espn(json.loads(body), start, end):
                if game["id"] not in seen:
                    seen.add(game["id"])
                    games.append(game)
            day += timedelta(days=1)
        games.sort(key=lambda game: game["dateTime"])
        return games
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(f"nba: {nba_error}; espn: {error}") from error


def get(url: str, referer: str | None = None) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return response.read().decode("utf-8")


def from_nba(payload: dict, start: datetime, end: datetime) -> list[dict]:
    league = payload.get("leagueSchedule")
    dates = league.get("gameDates") if isinstance(league, dict) else None
    if not isinstance(dates, list):
        raise RuntimeError("nba schedule shape")
    games = []
    seen = set()
    for day in dates:
        rows = day.get("games") if isinstance(day, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            game = nba_game(row, start, end)
            if game and game["id"] not in seen:
                seen.add(game["id"])
                games.append(game)
    games.sort(key=lambda game: game["dateTime"])
    return games


def from_espn(payload: dict, start: datetime, end: datetime) -> list[dict]:
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise RuntimeError("espn scoreboard shape")
    games = []
    for event in events:
        game = espn_event(event, start, end)
        if game:
            games.append(game)
    return games


def nba_game(row: object, start: datetime, end: datetime) -> dict | None:
    if not isinstance(row, dict):
        return None
    instant = parse_instant(row.get("gameDateTimeUTC"))
    if instant is None or not (start <= instant < end):
        return None
    if is_final(row.get("gameStatusText"), row.get("gameStatus")):
        return None
    home = short(team_code(row.get("homeTeam")))
    away = short(team_code(row.get("awayTeam")))
    zone = ZONES.get(home or "")
    if not home or not away or home == away or not zone:
        return None
    return game_row(
        home,
        away,
        instant,
        zone,
        venue(row.get("arenaName"), row.get("arenaCity")),
        "live" if row.get("gameStatus") == 2 else "scheduled",
    )


def espn_event(event: object, start: datetime, end: datetime) -> dict | None:
    if not isinstance(event, dict):
        return None
    instant = parse_instant(event.get("date"))
    if instant is None or not (start <= instant < end):
        return None
    status = event.get("status")
    kind = status.get("type") if isinstance(status, dict) else None
    state = kind.get("state") if isinstance(kind, dict) else None
    if state == "post" or is_final(kind, None):
        return None
    competitions = event.get("competitions")
    if not isinstance(competitions, list) or not competitions:
        return None
    competition = competitions[0]
    if not isinstance(competition, dict):
        return None
    home = away = None
    for side in competition.get("competitors") or []:
        if not isinstance(side, dict):
            continue
        code = short(team_code(side.get("team")))
        if side.get("homeAway") == "home":
            home = code
        elif side.get("homeAway") == "away":
            away = code
    zone = ZONES.get(home or "")
    if not home or not away or home == away or not zone:
        return None
    place = competition.get("venue")
    name = place.get("fullName") if isinstance(place, dict) else None
    return game_row(
        home,
        away,
        instant,
        zone,
        name.strip() if isinstance(name, str) and name.strip() else "NBA",
        "live" if state == "in" else "scheduled",
    )


def game_row(home: str, away: str, instant: datetime, zone: str, place: str, status: str) -> dict:
    stamp = iso(instant)
    return {
        "id": f"g-{home}-{away}-{stamp}",
        "home": home,
        "away": away,
        "dateTime": stamp,
        "arenaTimeZone": zone,
        "competition": "NBA",
        "venue": place,
        "status": status,
    }


def team_code(team: object) -> str | None:
    if not isinstance(team, dict):
        return None
    raw = team.get("teamTricode") or team.get("abbreviation")
    if not isinstance(raw, str):
        return None
    return raw.strip().upper()


def short(code: str | None) -> str | None:
    if not code:
        return None
    return CODES.get(code)


def venue(arena: object, city: object) -> str:
    name = arena.strip() if isinstance(arena, str) else ""
    place = city.strip() if isinstance(city, str) else ""
    if name and place:
        return f"{name} · {place}"
    return name or place or "NBA"


def is_final(text: object, code: object) -> bool:
    if code == 3 or code == "3":
        return True
    if isinstance(text, dict):
        state = text.get("state")
        if state == "post":
            return True
        return is_final(text.get("description") or text.get("shortDetail"), code)
    if isinstance(text, str) and "final" in text.lower():
        return True
    return False


def parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


if __name__ == "__main__":
    raise SystemExit(main())
