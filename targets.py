#!/usr/bin/env python3
"""Configuration-driven GitHub bounty target loader.

Users add repository or issue URLs to targets.json. Radar query generation stays
in code so the configuration remains small and safe.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ISSUE_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?$")

DEFAULTS = {
    "enabled": True,
    "priority": "normal",
    "minimum_bounty_usd": 50,
    "labels": [],
    "search_bounty_text": True,
    "search_bounty_label": True,
    "search_help_wanted": False,
}


class TargetConfigError(ValueError):
    pass


def parse_github_url(url: str) -> tuple[str, int | None]:
    raw = url.strip()
    if not raw.startswith(("https://github.com/", "http://github.com/")):
        raise TargetConfigError(f"unsupported GitHub URL: {url}")
    parsed = urlparse(raw)
    match = ISSUE_PATH_RE.match(parsed.path)
    if match:
        return f"{match.group(1)}/{match.group(2)}", int(match.group(3))
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) == 2 and REPO_RE.fullmatch("/".join(parts)):
        return "/".join(parts), None
    raise TargetConfigError(f"expected repository or issue URL: {url}")


def load_targets(path: Path = Path("targets.json")) -> list[dict]:
    if not path.exists():
        raise TargetConfigError(f"{path} does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TargetConfigError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("targets"), list):
        raise TargetConfigError("targets.json must contain a 'targets' array")

    out = []
    seen = set()
    global_min = float(data.get("global", {}).get("minimum_bounty_usd", 50))
    for index, raw in enumerate(data["targets"], 1):
        if not isinstance(raw, dict) or not raw.get("url"):
            raise TargetConfigError(f"target #{index} needs a url")
        repo, issue = parse_github_url(raw["url"])
        key = f"{repo}#{issue}" if issue else repo
        if key in seen:
            raise TargetConfigError(f"duplicate target: {key}")
        seen.add(key)
        item = {**DEFAULTS, **raw, "repo": repo, "issue_number": issue}
        item["minimum_bounty_usd"] = float(raw.get("minimum_bounty_usd", global_min))
        if item["minimum_bounty_usd"] < 0:
            raise TargetConfigError(f"target #{index}: minimum_bounty_usd cannot be negative")
        if item["priority"] not in {"normal", "high"}:
            raise TargetConfigError(f"target #{index}: priority must be normal or high")
        if not isinstance(item["labels"], list) or not all(isinstance(x, str) and x.strip() for x in item["labels"]):
            raise TargetConfigError(f"target #{index}: labels must be a list of non-empty strings")
        out.append(item)
    if not out:
        raise TargetConfigError("targets.json contains no targets")
    return out


def build_queries(targets: list[dict]) -> list[dict]:
    """Turn target config into deterministic GitHub search queries.

    Issue URLs become exact watches. Repository URLs generate a small set of
    complementary searches; no global random-repository search is performed.
    """
    queries = []
    for target in targets:
        if not target.get("enabled", True):
            continue
        repo = target["repo"]
        issue = target["issue_number"]
        prefix = f"target:{repo}" + (f"#{issue}" if issue else "")
        common = f"repo:{repo} is:issue is:open"
        if issue:
            queries.append({
                "name": prefix,
                "q": f"{common} #{issue}",
                "weight": 10 if target["priority"] == "high" else 5,
                "alert_new": "any",
                "opportunity_labels": target["labels"],
                "high_requires_opportunity_label": False,
                "assignee_matters": False,
                "target_minimum_bounty_usd": target["minimum_bounty_usd"],
                "target_key": prefix,
                "repo": repo,
                "exact_issue": issue,
            })
            continue

        if target["labels"] and target["search_bounty_label"]:
            for label in target["labels"]:
                queries.append({
                    "name": f"{prefix} label:{label}",
                    "q": f'{common} label:"{label}"',
                    "weight": 8 if target["priority"] == "high" else 5,
                    "alert_new": "any",
                    "opportunity_labels": target["labels"],
                    "high_requires_opportunity_label": False,
                    "assignee_matters": False,
                    "target_minimum_bounty_usd": target["minimum_bounty_usd"],
                    "target_key": prefix,
                })

        if target["search_bounty_text"]:
            queries.append({
                "name": f"{prefix} bounty-text",
                "q": f'{common} (bounty OR reward OR prize OR payout)',
                "weight": 6 if target["priority"] == "high" else 3,
                "alert_new": "bounty",
                "opportunity_labels": target["labels"],
                "high_requires_opportunity_label": False,
                "assignee_matters": False,
                "target_minimum_bounty_usd": target["minimum_bounty_usd"],
                "target_key": prefix,
            })

        if target["search_help_wanted"]:
            queries.append({
                "name": f"{prefix} help-wanted",
                "q": f'{common} label:"help wanted"',
                "weight": 3,
                "alert_new": "any",
                "opportunity_labels": target["labels"] + ["help wanted"],
                "high_requires_opportunity_label": False,
                "assignee_matters": False,
                "target_minimum_bounty_usd": target["minimum_bounty_usd"],
                "target_key": prefix,
            })

    if not queries:
        raise TargetConfigError("targets produced no searchable queries")
    return queries
