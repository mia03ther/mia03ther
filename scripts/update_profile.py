#!/usr/bin/env python3
"""Generate local light/dark profile cards from public GitHub REST data.

Usage: python scripts/update_profile.py [--login mia03ther] [--output-dir assets/generated]
Optional GITHUB_TOKEN / GH_TOKEN raises the API rate limit. DASHBOARD_TIMESTAMP
can pin the UTC source timestamp for reproducible rendering. Standard library only.
All API requests and rendering finish before existing assets are replaced.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
from html import escape
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TIMEOUT = 30
PAGE_SIZE = 100
MAX_REPO_PAGES = 100
THEMES = {
    "light": dict(bg="#f6f7fb", surface="#ffffff", fg="#20243b", muted="#626b83", line="#e0e4ef", accent="#7165d8"),
    "dark": dict(bg="#10121b", surface="#171b29", fg="#eef0fb", muted="#a3abc1", line="#2b3044", accent="#aaa0ff"),
}
LANGUAGE_COLORS = ["#7165d8", "#4e98be", "#52a391", "#c38a55", "#a877b3", "#8892aa"]
DARK_LANGUAGE_COLORS = ["#aaa0ff", "#75bce0", "#7bc6b3", "#e3b17e", "#d3a0dc", "#8993ad"]
FONT = "-apple-system, BlinkMacSystemFont, Segoe UI, Arial, sans-serif"


def integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid nonnegative integer: {label}")
    return value


def get_json(path: str, token: str | None) -> object:
    headers = {"User-Agent": "github-profile-cards", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_repos(login: str, token: str | None, max_pages: int = MAX_REPO_PAGES) -> list[dict]:
    """Read every public owned repository; never accept a truncated snapshot."""
    repos, seen = [], set()
    for page in range(1, max_pages + 1):
        batch = get_json(f"/users/{login}/repos?type=owner&per_page={PAGE_SIZE}&sort=full_name&direction=asc&page={page}", token)
        if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
            raise ValueError("Invalid repository list response")
        for repo in batch:
            if not isinstance(repo, dict):
                raise ValueError("Invalid repository record")
            repo_id = integer(repo.get("id"), "repository ID")
            if repo_id in seen:
                raise ValueError("Repository pagination changed during collection; retry later")
            owner = repo.get("owner")
            if not isinstance(owner, dict) or str(owner.get("login", "")).casefold() != login.casefold():
                raise ValueError("Unexpected repository owner")
            if repo.get("private") is not False or type(repo.get("fork")) is not bool:
                raise ValueError("Expected public repository with fork status")
            if not isinstance(repo.get("name"), str) or not repo["name"]:
                raise ValueError("Missing repository name")
            integer(repo.get("stargazers_count"), "repository stars")
            seen.add(repo_id)
            repos.append(repo)
        if len(batch) < PAGE_SIZE:
            return repos
    raise RuntimeError(f"Repository pagination exceeded {max_pages} pages; no cards were updated")


def fetch_languages(login: str, repos: list[dict], token: str | None) -> tuple[Counter, int, int]:
    """Sum GitHub language bytes across public nonforks, excluding the profile repo."""
    total: Counter = Counter()
    analyzed, with_languages = 0, 0
    for repo in repos:
        if repo["fork"] or repo["name"].casefold() == login.casefold():
            continue
        name = urllib.parse.quote(repo["name"], safe="")
        languages = get_json(f"/repos/{login}/{name}/languages", token)
        if not isinstance(languages, dict):
            raise ValueError("Invalid repository language response")
        for language, amount in languages.items():
            if not isinstance(language, str) or not language:
                raise ValueError("Invalid language name")
            integer(amount, "language bytes")
        total.update({language: amount for language, amount in languages.items() if amount > 0})
        analyzed += 1
        with_languages += any(languages.values())
    return total, analyzed, with_languages


def language_rows(totals: Counter) -> list[tuple[str, int]]:
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0].casefold()))
    rows = ordered[:5]
    remainder = sum(amount for _, amount in ordered[5:])
    if remainder:
        rows.append(("Other", remainder))
    return rows


def label(x: float, y: float, value: object, size: int, fill: str, weight: int = 400, anchor: str = "start") -> str:
    return f'<text x="{x:g}" y="{y:g}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(str(value))}</text>'


def document(height: int, title: str, description: str, theme: dict, body: list[str], width: int = 800) -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title>', f'<desc id="desc">{escape(description)}</desc>',
        f'<rect width="{width}" height="{height}" rx="18" fill="{theme["bg"]}"/>',
        f'<rect x="1" y="1" width="{width-2}" height="{height-2}" rx="17" fill="{theme["surface"]}" stroke="{theme["line"]}"/>',
        *body, "</svg>", "",
    ])


def source_note(y: int, date: str, theme: dict, mobile: bool = False) -> str:
    return label(22 if mobile else 28, y, f"Source: GitHub REST API · {date} UTC", 12 if mobile else 14, theme["muted"])


def build_stats(login: str, user: dict, repos: list[dict], date: str, theme: dict, mobile: bool = False) -> str:
    source = [repo for repo in repos if not repo["fork"]]
    values = [
        ("Public repos", integer(user.get("public_repos"), "public repositories"), "includes forks"),
        ("Stars", sum(repo["stargazers_count"] for repo in source), "owned source repos"),
        ("Followers", integer(user.get("followers"), "followers"), "GitHub followers"),
        ("Source repos", len(source), "owned · nonforks"),
    ]
    description = "; ".join(f"{name}: {value} ({note})" for name, value, note in values)
    if mobile:
        body = [label(22, 40, "GitHub at a glance", 22, theme["fg"], 650)]
        for index, (name, value, note) in enumerate(values):
            x, y = 22 + (index % 2) * 181, 80 + (index // 2) * 106
            body.extend([label(x, y, name, 16, theme["muted"]), label(x, y+40, f"{value:,}", 32, theme["accent"], 650), label(x, y+64, note, 12, theme["muted"])])
        body.extend([f'<path d="M22 272H378" stroke="{theme["line"]}"/>', source_note(300, date, theme, True)])
        return document(322, f"GitHub statistics for {login}", description, theme, body, 400)
    body = [label(28, 42, "GitHub at a glance", 24, theme["fg"], 650)]
    for index, (name, value, note) in enumerate(values):
        x = 28 + index * 190
        body.extend([label(x, 87, name, 17, theme["muted"]), label(x, 139, f"{value:,}", 42, theme["accent"], 650), label(x, 166, note, 14, theme["muted"])])
    body.append(f'<path d="M28 187H772" stroke="{theme["line"]}"/>')
    body.append(source_note(215, date, theme))
    return document(236, f"GitHub statistics for {login}", description, theme, body)


def build_metrics(login: str, totals: Counter, analyzed: int, with_languages: int, date: str, theme: dict, dark: bool, mobile: bool = False) -> str:
    rows = language_rows(totals)
    total = sum(totals.values())
    colors = DARK_LANGUAGE_COLORS if dark else LANGUAGE_COLORS
    row_count = max(len(rows), 1)
    left, right, bar_y, first_row, row_gap = (22, 378, 140, 190, 32) if mobile else (28, 772, 122, 177, 35)
    bar_width = right - left
    footer_y = 183 + row_count * row_gap
    height = footer_y + 58
    body = [
        label(28, 42, "Languages across my repositories", 24, theme["fg"], 650),
        label(28, 72, "Share of GitHub language bytes · public source repos", 16, theme["muted"]),
        label(28, 97, f"{analyzed} repos analyzed · {with_languages} with language data · profile repo excluded", 14, theme["muted"]),
        f'<rect x="28" y="122" width="744" height="16" rx="8" fill="{theme["line"]}"/>',
    ]
    if mobile:
        body = [
            label(22, 40, "Repository languages", 22, theme["fg"], 650),
            label(22, 70, "Share of GitHub language bytes", 14, theme["muted"]),
            label(22, 92, "Public source repos · profile repo excluded", 14, theme["muted"]),
            label(22, 117, f"{analyzed} repos analyzed · {with_languages} with language data", 12, theme["muted"]),
            f'<rect x="22" y="140" width="356" height="16" rx="8" fill="{theme["line"]}"/>',
        ]
    if total:
        body.extend([f'<defs><clipPath id="bar"><rect x="{left}" y="{bar_y}" width="{bar_width}" height="16" rx="8"/></clipPath></defs>', '<g clip-path="url(#bar)">'])
        x = float(left)
        for index, (_, amount) in enumerate(rows):
            width = amount / total * bar_width
            body.append(f'<rect x="{x:.5f}" y="{bar_y}" width="{width:.5f}" height="16" fill="{colors[index]}"/>')
            x += width
        body.append('</g>')
        for index, (name, amount) in enumerate(rows):
            y = first_row + index * row_gap
            body.extend([f'<circle cx="{left+7}" cy="{y-6}" r="5" fill="{colors[index]}"/>', label(left+23, y, name, 16 if mobile else 17, theme["fg"]), label(right, y, f"{amount / total:.1%}", 16 if mobile else 17, theme["muted"], anchor="end")])
    else:
        body.append(label(left, first_row, "No language bytes reported." if mobile else "No language bytes reported in the analyzed repositories.", 16 if mobile else 17, theme["muted"]))
    body.extend([f'<path d="M{left} {footer_y-14}H{right}" stroke="{theme["line"]}"/>', source_note(footer_y+15, date, theme, mobile)])
    description = f"Language byte share across {analyzed} public owned nonfork repositories; profile repository excluded. " + ("; ".join(f"{name}: {amount / total:.1%}" for name, amount in rows) if total else "No language bytes reported.")
    return document(height, f"Repository languages for {login}", description, theme, body, 400 if mobile else 800)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", default="mia03ther")
    parser.add_argument("--output-dir", type=Path, default=Path("assets/generated"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", args.login):
        parser.error("--login must be a GitHub username")
    login = args.login
    token = next((os.environ[key].strip() for key in ("GITHUB_TOKEN", "GH_TOKEN") if os.environ.get(key, "").strip()), None)
    raw_stamp = os.environ.get("DASHBOARD_TIMESTAMP", "").strip()
    stamp = dt.datetime.fromisoformat(raw_stamp.replace("Z", "+00:00")) if raw_stamp else dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("DASHBOARD_TIMESTAMP must include a timezone")
    date = stamp.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    user = get_json(f"/users/{login}", token)
    if not isinstance(user, dict):
        raise ValueError("Invalid user response")
    repos = fetch_repos(login, token)
    if integer(user.get("public_repos"), "public repositories") != len(repos):
        raise RuntimeError("Repository count changed during collection; retry later")
    totals, analyzed, with_languages = fetch_languages(login, repos, token)
    assets = {}
    for name, theme in THEMES.items():
        assets[f"stats-{name}.svg"] = build_stats(login, user, repos, date, theme)
        assets[f"metrics-{name}.svg"] = build_metrics(login, totals, analyzed, with_languages, date, theme, name == "dark")
        assets[f"stats-mobile-{name}.svg"] = build_stats(login, user, repos, date, theme, True)
        assets[f"metrics-mobile-{name}.svg"] = build_metrics(login, totals, analyzed, with_languages, date, theme, name == "dark", True)
    # Complete collection and rendering before touching any currently published SVG.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for name, content in assets.items():
            temporary = args.output_dir / f".{name}.tmp"
            temporary.write_text(content, encoding="utf-8", newline="\n")
            staged.append((temporary, args.output_dir / name))
        for temporary, target in staged:
            temporary.replace(target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    print(f"Updated eight cards from {len(repos)} public repos; analyzed {analyzed} source repos.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as error:
        print(f"GitHub API returned HTTP {error.code}; existing cards preserved.", file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError, OSError):
        print("Profile refresh failed; existing cards preserved. Check API access, rate limits, and response shape.", file=sys.stderr)
        raise SystemExit(1)
