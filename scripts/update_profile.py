#!/usr/bin/env python3
"""Generate the public builder dashboard SVGs for the MIA_Ether profile repository.

Data sources, in order of preference:

1. GitHub GraphQL API (used when a token is available, e.g. GITHUB_TOKEN inside
   GitHub Actions) to read the real contribution calendar for the last 12 weeks.
2. Public GitHub REST API, used as a fallback, aggregating public events into
   weekly buckets.

Only the Python standard library is used.

Usage:
    python scripts/update_profile.py [--login mia03ther] [--output-dir assets/generated]

Environment:
    GITHUB_TOKEN / GH_TOKEN   Optional token used for the GraphQL contribution query
                               and to lift REST rate limits.
    DASHBOARD_TIMESTAMP        Pins the rendered timestamp (ISO-8601 UTC). Used to
                               verify byte-for-byte idempotency of the generator.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOGIN = "mia03ther"
API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
TIMEOUT = 30
WEEKS = 12
SHIP_LOG_ITEMS = 5

BG = "#0b0d10"
BORDER = "#303740"
GRID = "#20262d"
GRID_SOFT = "#1d252b"
FG = "#f5f2ea"
MUTED = "#7d8792"
CYAN = "#63f3e4"
PURPLE = "#b58cff"
LIME = "#b7f36b"
PANEL = "#12171c"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# Event types that represent building rather than passive browsing.
BUILD_EVENT_TYPES = {
    "PushEvent",
    "CreateEvent",
    "PullRequestEvent",
    "IssuesEvent",
    "IssueCommentEvent",
    "ReleaseEvent",
    "ForkEvent",
    "DeleteEvent",
    "PublicEvent",
    "MemberEvent",
    "CommitCommentEvent",
}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "User-Agent": "mia03ther-profile-dashboard",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, token: str | None) -> object:
    request = urllib.request.Request(url, headers=_headers(token))
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict, token: str) -> object:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={**_headers(token), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def warn(message: str) -> None:
    print(f"[dashboard] {message}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #
def fetch_user(token: str | None) -> dict:
    data = _get_json(f"{API}/users/{LOGIN}", token)
    if not isinstance(data, dict):
        raise RuntimeError("unexpected user payload")
    return data


def fetch_repos(token: str | None) -> list[dict]:
    repos: list[dict] = []
    for page in range(1, 4):
        batch = _get_json(
            f"{API}/users/{LOGIN}/repos?per_page=100&sort=pushed&page={page}", token
        )
        if not isinstance(batch, list) or not batch:
            break
        repos.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return repos


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def week_starts() -> list[dt.date]:
    """Monday of each of the last WEEKS weeks, oldest first."""
    today = dt.datetime.now(dt.timezone.utc).date()
    this_monday = today - dt.timedelta(days=today.weekday())
    first = this_monday - dt.timedelta(days=7 * (WEEKS - 1))
    return [first + dt.timedelta(days=7 * i) for i in range(WEEKS)]


def bucket_index(day: dt.date) -> int | None:
    """Return the week bucket index for a day, or None when out of range."""
    starts = week_starts()
    for index, start in enumerate(starts):
        if start <= day <= start + dt.timedelta(days=6):
            return index
    return None


def fetch_weekly_activity_rest(token: str | None) -> tuple[list[int], str]:
    """Bucket public events into ISO weeks for the last WEEKS weeks."""
    events: list[dict] = []
    for page in range(1, 4):
        batch = _get_json(f"{API}/users/{LOGIN}/events/public?per_page=100&page={page}", token)
        if not isinstance(batch, list) or not batch:
            break
        events.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
        if len(events) >= 300:
            break

    values = [0] * WEEKS
    for event in events:
        if event.get("type") not in BUILD_EVENT_TYPES:
            continue
        try:
            day = _parse_time(str(event["created_at"])).date()
        except (KeyError, ValueError):
            continue
        index = bucket_index(day)
        if index is not None:
            values[index] += 1

    return values, "public events (REST)"


def fetch_weekly_activity_graphql(token: str) -> tuple[list[int], str] | None:
    """Read the real contribution calendar and bucket it per week."""
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=WEEKS * 7 + 7)
    query = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date contributionCount }
        }
      }
    }
  }
}
"""
    payload = {
        "query": query,
        "variables": {
            "login": LOGIN,
            "from": f"{start.isoformat()}T00:00:00Z",
            "to": f"{end.isoformat()}T23:59:59Z",
        },
    }
    try:
        result = _post_json(GRAPHQL, payload, token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
        warn(f"GraphQL contribution query failed ({error}); falling back to REST")
        return None

    if isinstance(result, dict) and result.get("errors"):
        warn(f"GraphQL returned errors; falling back to REST: {result['errors']}")
        return None

    weeks = (
        result.get("data", {})
        .get("user", {})
        .get("contributionsCollection", {})
        .get("contributionCalendar", {})
        .get("weeks", [])
        if isinstance(result, dict)
        else []
    )
    if not weeks:
        return None

    values = [0] * WEEKS
    for week in weeks:
        days = [day for day in week.get("contributionDays", []) if isinstance(day, dict)]
        if not days:
            continue
        try:
            day = dt.date.fromisoformat(str(days[0]["date"]))
        except (KeyError, ValueError):
            continue
        index = bucket_index(day)
        if index is None:
            continue
        values[index] += sum(int(day.get("contributionCount", 0)) for day in days)

    return values, "contribution calendar (GraphQL)"


def collect_activity(token: str | None) -> tuple[list[int], str, int]:
    """Return (weekly values, source label, build events in the last 30 days)."""
    weekly: list[int] | None = None
    source = ""
    if token:
        graphql = fetch_weekly_activity_graphql(token)
        if graphql is not None:
            weekly, source = graphql

    if weekly is None:
        weekly, source = fetch_weekly_activity_rest(token)

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    recent = 0
    try:
        batch = _get_json(f"{API}/users/{LOGIN}/events/public?per_page=100", token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        batch = []
    if isinstance(batch, list):
        for event in batch:
            if event.get("type") not in BUILD_EVENT_TYPES:
                continue
            try:
                when = _parse_time(str(event["created_at"]))
            except (KeyError, ValueError):
                continue
            if when >= cutoff:
                recent += 1

    return weekly, source, recent


# --------------------------------------------------------------------------- #
# SVG helpers
# --------------------------------------------------------------------------- #
def escape(text: object) -> str:
    value = str(text)
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def mono(size: int, fill: str, weight: str = "400", spacing: str | None = None) -> str:
    extra = f' letter-spacing="{spacing}"' if spacing else ""
    return (
        f'font-family="{MONO}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}"{extra}'
    )


def text(
    x: float,
    y: float,
    content: object,
    size: int,
    fill: str,
    weight: str = "400",
    anchor: str = "start",
    spacing: str | None = None,
) -> str:
    extra = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return (
        f'<text x="{round(x, 2)}" y="{round(y, 2)}" {mono(size, fill, weight, spacing)}'
        f'{extra}>{escape(content)}</text>'
    )


def svg_document(width: int, height: int, title: str, desc: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-labelledby="t d">\n'
        f"<title id=\"t\">{escape(title)}</title>\n"
        f"<desc id=\"d\">{escape(desc)}</desc>\n"
        "<defs>\n"
        '<pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">'
        f'<path d="M32 0H0V32" fill="none" stroke="{GRID_SOFT}" stroke-width="1"/>'
        "</pattern>\n"
        '<linearGradient id="sig" x1="0" x2="1">'
        f'<stop offset="0" stop-color="{CYAN}"/>'
        f'<stop offset="1" stop-color="{PURPLE}"/>'
        "</linearGradient>\n"
        "</defs>\n"
        f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>\n'
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="9" '
        f'fill="url(#grid)" stroke="{BORDER}"/>\n'
        f"{body}\n</svg>\n"
    )


def frame(width: int, height: int, label: str, stamp: str, right_label: str) -> str:
    parts = [
        text(42, 44, label, 12, MUTED, "400", spacing="2"),
        text(width - 42, 44, right_label, 11, PURPLE, "400", anchor="end"),
        f'<path d="M42 62H{width - 42}" stroke="{GRID}"/>',
        text(42, height - 24, f"GENERATED {stamp}", 11, MUTED),
    ]
    return "\n".join(parts)


def short_stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def build_live_builder(
    user: dict, repos: list[dict], weekly: list[int], activity_source: str, recent: int, stamp: dt.datetime
) -> str:
    width, height = 1200, 372
    owned = [repo for repo in repos if not repo.get("fork")]
    public_repos = int(user.get("public_repos") or len(owned) or 0)
    stars = sum(int(repo.get("stargazers_count") or 0) for repo in owned)
    followers = int(user.get("followers") or 0)
    total_activity = sum(weekly)

    metrics = [
        ("REPOSITORIES", public_repos, CYAN, "public"),
        ("STARS", stars, PURPLE, "owned repos"),
        ("FOLLOWERS", followers, LIME, "public"),
        ("ACTIVITY", recent, FG, "build events / 30d"),
    ]

    body = [frame(width, height, "LIVE BUILDER SIGNAL", short_stamp(stamp), "SYSTEM / 02")]
    card_width, gap, left = 262, 26, 42
    top = 92
    for index, (label, value, colour, note) in enumerate(metrics):
        x = left + index * (card_width + gap)
        body.append(
            f'<rect x="{x}" y="{top}" width="{card_width}" height="130" rx="8" '
            f'fill="{PANEL}" stroke="{BORDER}"/>'
        )
        body.append(f'<rect x="{x}" y="{top}" width="3" height="130" rx="1.5" fill="{colour}"/>')
        body.append(text(x + 22, top + 34, label, 12, MUTED, "400", spacing="2"))
        body.append(text(x + 22, top + 94, value, 46, colour, "700"))
        body.append(text(x + 22, top + 116, note, 11, MUTED))

    rule = top + 158
    body.append(f'<path d="M42 {rule}H1158" stroke="{GRID}"/>')
    body.append(text(42, rule + 30, "12-WEEK TOTAL", 12, MUTED, "400", spacing="2"))
    body.append(text(42, rule + 64, total_activity, 26, CYAN, "700"))
    body.append(text(146, rule + 64, "contributions", 13, MUTED))
    body.append(text(470, rule + 64, "SOURCE", 12, MUTED, "400", spacing="2"))
    body.append(text(540, rule + 64, activity_source, 13, FG))
    body.append(text(1158, rule + 64, f"github.com/{LOGIN}", 12, MUTED, anchor="end"))

    desc = (
        f"Live builder signal for {LOGIN}: {public_repos} public repositories, "
        f"{stars} stars, {followers} followers, {recent} public build events in the last 30 days."
    )
    return svg_document(width, height, f"LIVE BUILDER SIGNAL — {LOGIN}", desc, "\n".join(body))


def build_activity_chart(weekly: list[int], activity_source: str, stamp: dt.datetime) -> str:
    width, height = 1200, 400
    left, right = 96, 1158
    plot_top, plot_bottom = 118, 300

    labels = [start.strftime("%b %d") for start in week_starts()]

    peak = max(weekly) if weekly else 0
    ceiling = peak if peak > 0 else 1
    step = 10 ** (len(str(ceiling)) - 1)
    top_value = ((ceiling // step) + 1) * step

    def y_for(value: int) -> float:
        span = plot_bottom - plot_top
        return plot_bottom - (value / top_value) * span if top_value else plot_bottom

    def x_for(index: int) -> float:
        if len(weekly) <= 1:
            return left
        return left + index * ((right - left) / (len(weekly) - 1))

    body = [
        frame(
            width,
            height,
            "BUILD ACTIVITY · LAST 12 WEEKS",
            short_stamp(stamp),
            "SYSTEM / 03",
        ),
        text(42, 86, f"SOURCE {activity_source} · 12-WEEK TOTAL {sum(weekly)}", 12, MUTED),
    ]

    for step_index in range(5):
        value = top_value * step_index // 4
        y = plot_bottom - (plot_bottom - plot_top) * step_index / 4
        body.append(f'<path d="M{left} {round(y, 2)}H{right}" stroke="{GRID}"/>')
        body.append(text(left - 14, round(y + 4, 2), value, 11, MUTED, anchor="end"))

    points = [(round(x_for(i), 2), round(y_for(value), 2)) for i, value in enumerate(weekly)]
    path = "M" + "L".join(f"{x} {y}" for x, y in points)
    area = (
        f"M{points[0][0]} {plot_bottom}L"
        + "L".join(f"{x} {y}" for x, y in points)
        + f"L{points[-1][0]} {plot_bottom}Z"
    )

    body.append(f'<path d="{area}" fill="{CYAN}" fill-opacity="0.06"/>')
    body.append(f'<path d="{path}" fill="none" stroke="url(#sig)" stroke-width="2.5" stroke-linejoin="round"/>')

    for index, ((x, y), value) in enumerate(zip(points, weekly)):
        colour = LIME if index == len(weekly) - 1 else CYAN
        body.append(f'<circle cx="{x}" cy="{y}" r="3.5" fill="{BG}" stroke="{colour}" stroke-width="2"/>')
        label_x = min(max(x, left + 24), right - 24)
        body.append(text(label_x, plot_bottom + 26, labels[index], 10, MUTED, anchor="middle"))
        if index == len(weekly) - 1:
            body.append(text(x, y - 14, value, 12, LIME, "700", anchor="middle"))

    body.append(text(left, 372, "OLDEST", 11, MUTED, "400", spacing="2"))
    body.append(text(right, 372, "LATEST", 11, MUTED, "400", anchor="end", spacing="2"))

    desc = (
        "Line chart of weekly build activity over the last 12 weeks, "
        f"12-week total {sum(weekly)}, source {activity_source}."
    )
    return svg_document(
        width, height, "BUILD ACTIVITY · LAST 12 WEEKS", desc, "\n".join(body)
    )


def build_ship_log(repos: list[dict], stamp: dt.datetime) -> str:
    width, height = 1200, 420
    owned = [
        repo
        for repo in repos
        if not repo.get("fork") and repo.get("name") != LOGIN and repo.get("pushed_at")
    ]
    owned.sort(key=lambda repo: repo.get("pushed_at") or "", reverse=True)
    selected = owned[:SHIP_LOG_ITEMS]

    body = [frame(width, height, "LATEST SHIP", short_stamp(stamp), "SYSTEM / 04")]
    body.append(text(42, 86, "LATEST PUSHES ON PUBLIC REPOSITORIES", 12, MUTED, "400", spacing="2"))

    body.append(text(1158, 86, "PRIMARY LANGUAGE", 12, MUTED, "400", anchor="end", spacing="2"))

    row_height = 58
    top = 112
    for index, repo in enumerate(selected):
        y = top + index * row_height
        pushed = _parse_time(str(repo["pushed_at"])).strftime("%Y.%m.%d")
        name = str(repo.get("name") or "unknown")
        language = str(repo.get("language") or "—")
        description = repo.get("description") or ""
        note = description.strip().replace("\n", " ")
        if not note:
            note = "no repository description"

        body.append(f'<rect x="42" y="{y}" width="1116" height="48" rx="6" fill="{PANEL}" stroke="{BORDER}"/>')
        body.append(text(60, y + 30, pushed, 13, LIME, "700"))
        body.append(text(158, y + 30, name, 15, FG, "700"))
        body.append(text(158, y + 45, note[:88], 11, MUTED))
        body.append(text(1140, y + 30, language, 11, CYAN, anchor="end"))

    if not selected:
        body.append(text(60, top + 30, "NO PUBLIC PUSH DATA AVAILABLE", 13, MUTED))

    desc = "Latest public repository pushes for " + LOGIN + ": " + (
        ", ".join(str(repo.get("name")) for repo in selected) or "none"
    )
    return svg_document(width, height, "LATEST SHIP", desc, "\n".join(body))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    global LOGIN

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", default=LOGIN)
    parser.add_argument("--output-dir", default="assets/generated")
    args = parser.parse_args()

    LOGIN = args.login

    token = _token()
    raw_stamp = os.environ.get("DASHBOARD_TIMESTAMP", "").strip()
    if raw_stamp:
        stamp = _parse_time(raw_stamp).replace(microsecond=0)
    else:
        stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    user = fetch_user(token)
    repos = fetch_repos(token)
    weekly, source, recent = collect_activity(token)
    warn(f"activity source: {source}; 12-week total {sum(weekly)}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    files = {
        output_dir / "live-builder.svg": build_live_builder(
            user, repos, weekly, source, recent, stamp
        ),
        output_dir / "build-activity.svg": build_activity_chart(weekly, source, stamp),
        output_dir / "ship-log.svg": build_ship_log(repos, stamp),
    }

    for path, content in files.items():
        path.write_text(content, encoding="utf-8", newline="\n")
        warn(f"wrote {path}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
        warn(f"GitHub request failed: {error}")
        raise SystemExit(1)