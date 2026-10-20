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


def document(title: str, description: str, theme: dict, body: list[str]) -> str:
    return "\n".join([
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title>', f'<desc id="desc">{escape(description)}</desc>',
        f'<rect width="400" height="200" rx="14" fill="{theme["bg"]}"/>',
        f'<rect x="1" y="1" width="398" height="198" rx="13" fill="{theme["surface"]}" stroke="{theme["line"]}"/>',
        *body, "</svg>", "",
    ])


def source_note(date: str, theme: dict) -> str:
    return label(20, 190, f"GitHub REST API · {date} UTC", 11, theme["muted"])


def build_stats(login: str, user: dict, repos: list[dict], date: str, theme: dict) -> str:
    source = [repo for repo in repos if not repo["fork"]]
    values = [
        ("Public repos", integer(user.get("public_repos"), "public repositories"), "includes forks"),
        ("Stars (source)", sum(repo["stargazers_count"] for repo in source), "owned nonfork repos"),
        ("Followers", integer(user.get("followers"), "followers"), "GitHub followers"),
        ("Source repos", len(source), "owned nonfork repos"),
    ]
    body = [label(20, 29, "GitHub at a glance", 18, theme["fg"], 650)]
    for index, (name, value, _) in enumerate(values):
        x, y = 20 + (index % 2) * 192, 57 + (index // 2) * 59
        body.extend([label(x, y, name, 14, theme["muted"]), label(x, y+29, f"{value:,}", 26, theme["accent"], 650)])
    body.extend([
        f'<path d="M20 158H380" stroke="{theme["line"]}"/>',
        label(20, 175, "Public includes forks · source = owned nonforks", 11, theme["muted"]),
        source_note(date, theme),
    ])
    description = "; ".join(f"{name}: {value} ({note})" for name, value, note in values)
    return document(f"GitHub statistics for {login}", description, theme, body)


def build_metrics(login: str, totals: Counter, analyzed: int, with_languages: int, date: str, theme: dict, dark: bool) -> str:
    rows = language_rows(totals)
    total = sum(totals.values())
    colors = DARK_LANGUAGE_COLORS if dark else LANGUAGE_COLORS
    body = [
        label(20, 29, "Repository languages", 18, theme["fg"], 650),
        f'<rect x="20" y="43" width="360" height="12" rx="6" fill="{theme["line"]}"/>',
    ]
    if total:
        body.extend(['<defs><clipPath id="bar"><rect x="20" y="43" width="360" height="12" rx="6"/></clipPath></defs>', '<g clip-path="url(#bar)">'])
        x = 20.0
        for index, (_, amount) in enumerate(rows):
            width = amount / total * 360
            body.append(f'<rect x="{x:.5f}" y="43" width="{width:.5f}" height="12" fill="{colors[index]}"/>')
            x += width
        body.append('</g>')
        for index, (name, amount) in enumerate(rows):
            x, y = 20 + (index % 2) * 192, 82 + (index // 2) * 29
            body.extend([
                f'<circle cx="{x+4}" cy="{y-4}" r="4" fill="{colors[index]}"/>',
                label(x+15, y, name, 13, theme["fg"]),
                label(x+168, y, f"{amount / total:.1%}", 13, theme["muted"], anchor="end"),
            ])
    else:
        body.append(label(20, 91, "No language bytes reported.", 14, theme["muted"]))
    body.extend([
        f'<path d="M20 158H380" stroke="{theme["line"]}"/>',
        label(20, 175, f"{analyzed} public source repos · profile excluded · byte share", 11, theme["muted"]),
        source_note(date, theme),
    ])
    description = f"Language byte share across {analyzed} public owned nonfork repositories ({with_languages} with language data); profile repository excluded. " + ("; ".join(f"{name}: {amount / total:.1%}" for name, amount in rows) if total else "No language bytes reported.")
    return document(f"Repository languages for {login}", description, theme, body)


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
    print(f"Updated four cards from {len(repos)} public repos; analyzed {analyzed} source repos.")
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
