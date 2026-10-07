#!/usr/bin/env python3
"""Safe orchestration for human-gated bounty job actions."""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from jobs import load_store, save_store, transition, apply_action, validate_action
from proposal import build as build_proposal
from dossier import update_section, save_dossier, get_dossier_path


def post_comment(root: Path, job_id: str) -> None:
    data = load_store(root / "jobs.json")
    job = data["jobs"].get(job_id)
    if not job:
        raise RuntimeError(f"job not found: {job_id}")

    # Idempotent check: if already posted or awaiting maintainer, do not duplicate comment
    if job.get("state") in {"COMMENT_POSTED", "AWAITING_MAINTAINER"} and job.get("artifacts", {}).get("posted_comment"):
        print(f"Comment was already posted for {job_id}: {job['artifacts']['posted_comment'].get('url')}")
        return

    if job.get("state") != "HUMAN_REVIEW":
        raise RuntimeError(f"job must be HUMAN_REVIEW to post comment; current={job.get('state')}")

    token = os.environ.get("UPSTREAM_GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UPSTREAM_GITHUB_TOKEN is required to post to the target repository")

    proposal_path = root / job["artifacts"]["proposal"]
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))

    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "github-bounty-radar",
    })
    url = f"https://api.github.com/repos/{job['repo']}/issues/{job['issue_number']}/comments"
    r = s.post(url, json={"body": proposal["body"]}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"comment post failed: HTTP {r.status_code}: {r.text[:500]}")

    data = load_store(root / "jobs.json")
    job = data["jobs"][job_id]
    posted = r.json()
    job["artifacts"]["posted_comment"] = {"id": posted.get("id"), "url": posted.get("html_url")}
    if posted.get("id"):
        job["observed_comment_ids"] = list(set(job.get("observed_comment_ids", [])) | {posted.get("id")})[-200:]

    actor = os.environ.get("ACTOR") or os.environ.get("GITHUB_ACTOR", "human")
    transition(data, job_id, "COMMENT_POSTED", actor=actor)
    transition(data, job_id, "AWAITING_MAINTAINER", actor="worker")
    save_store(data, root / "jobs.json")

    # Update dossier section 15 with posted comment URL
    dossier_path = get_dossier_path(root, job_id, job["issue_number"])
    if dossier_path.exists():
        content = dossier_path.read_text(encoding="utf-8")
        updated_body = f"{proposal['body']}\n\n- **Posted Comment URL**: {posted.get('html_url')}"
        new_content = update_section(content, "15. Maintainer Proposal", updated_body)
        save_dossier(root, job_id, job["issue_number"], new_content)

    print(f"Posted comment successfully: {posted.get('html_url')}")


def apply(action: str, job_id: str, root: Path = Path(".")) -> None:
    action = validate_action(action)
    actor = os.environ.get("ACTOR") or os.environ.get("GITHUB_ACTOR", "human")
    reason = os.environ.get("REASON", "")

    if action == "PREPARE_PROPOSAL":
        data = load_store(root / "jobs.json")
        job = data["jobs"].get(job_id)
        if not job or job.get("state") not in {"DIAGNOSIS_READY", "CROSS_REVIEW"}:
            raise RuntimeError(f"job is not ready for proposal; current={job.get('state') if job else 'None'}")
        build_proposal(job_id, root)
        return

    if action == "POST_COMMENT":
        post_comment(root, job_id)
        return

    data = load_store(root / "jobs.json")
    if action == "MAINTAINER_AGREED":
        transition(data, job_id, "MAINTAINER_AGREED", actor=actor, reason=reason)
    elif action == "APPROVE_IMPLEMENTATION":
        transition(data, job_id, "APPROVED", actor=actor, reason=reason)
    elif action == "REJECT_IMPLEMENTATION":
        transition(data, job_id, "CANCELLED", actor=actor, reason=reason)
    else:
        apply_action(data, job_id, action, actor=actor, reason=reason)

    save_store(data, root / "jobs.json")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("action")
    p.add_argument("job_id")
    a = p.parse_args()
    apply(a.action, a.job_id)
