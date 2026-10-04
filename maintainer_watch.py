#!/usr/bin/env python3
"""Record new maintainer-visible issue activity without making conclusions.

Watches issues in AWAITING_MAINTAINER, COMMENT_POSTED, HUMAN_REVIEW, etc.
Updates Section 16 of the Markdown Engineering Dossier.
Does NOT automatically approve; human approval remains strictly required.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from jobs import load_store, save_store, utc_now
from notify import send
from dossier import update_dossier_maintainer_response


def main() -> int:
    root = Path(".")
    data = load_store(root / "jobs.json")
    token = os.environ.get("GITHUB_TOKEN", "")
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bounty-radar",
    })
    if token:
        session.headers["Authorization"] = f"Bearer {token}"

    watched_states = {
        "COMMENT_POSTED",
        "AWAITING_MAINTAINER",
        "MAINTAINER_AGREED",
        "HUMAN_REVIEW",
        "APPROVED",
        "PR_READY",
        "PR_OPENED",
    }
    changed = False

    for jid, job in data["jobs"].items():
        if job.get("state") not in watched_states:
            continue

        url = f"https://api.github.com/repos/{job['repo']}/issues/{job['issue_number']}/comments"
        try:
            r = session.get(url, params={"per_page": 100}, timeout=30)
            if r.status_code >= 400:
                continue
            comments = r.json()
        except requests.RequestException:
            continue

        seen = set(job.get("observed_comment_ids", []))
        new = []
        for c in comments:
            cid = c.get("id")
            if cid in seen:
                continue
            new.append({
                "id": cid,
                "author": (c.get("user") or {}).get("login"),
                "association": c.get("author_association"),
                "body": c.get("body", "")[:4000],
                "created_at": c.get("created_at"),
                "url": c.get("html_url"),
            })
            seen.add(cid)

        if new:
            job.setdefault("maintainer_activity", []).extend(new)
            job["maintainer_activity"] = job["maintainer_activity"][-50:]
            job["observed_comment_ids"] = list(seen)[-200:]
            job.setdefault("history", []).append({
                "at": utc_now(),
                "event": "NEW_ISSUE_ACTIVITY",
                "count": len(new),
            })
            job["updated_at"] = utc_now()
            changed = True

            # Update engineering dossier section 16
            update_dossier_maintainer_response(root, jid, job["issue_number"], job["maintainer_activity"])

            send(
                f"New activity — {job['repo']}#{job['issue_number']}",
                f"{len(new)} new issue comment/event item(s) observed for bounty job {jid}. Review the maintainer response before approving implementation.",
                click=f"https://github.com/{job['repo']}/issues/{job['issue_number']}",
            )

    if changed:
        save_store(data, root / "jobs.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
