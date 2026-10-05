#!/usr/bin/env python3
"""Durable, auditable bounty-job control plane.

Phase-complete control plane for GitHub Bounty Radar.
No AI, repository mutation, comments, or PR creation happens here.
Those operations are deliberately separated into later worker stages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILE = Path(os.environ.get("BOUNTY_JOBS_FILE", "jobs.json"))
SCHEMA_VERSION = 2

STATES = (
    "DISCOVERED",
    "PENDING_APPROVAL",
    "ACCEPTED",
    "INGESTING",
    "INVESTIGATING",
    "CROSS_REVIEW",
    "DIAGNOSIS_READY",
    "PROPOSAL_READY",
    "HUMAN_REVIEW",
    "APPROVED",
    "IMPLEMENTING",
    "TESTING",
    "FINAL_REVIEW",
    "PR_READY",
    "PR_OPENED",
    "COMMENT_POSTED",
    "AWAITING_MAINTAINER",
    "MAINTAINER_AGREED",
    "DECLINED",
    "CANCELLED",
    "FAILED",
)

# Complete state machine transitions for control-plane and worker stages.
CONTROL_TRANSITIONS = {
    "DISCOVERED": {"PENDING_APPROVAL", "CANCELLED"},
    "PENDING_APPROVAL": {"ACCEPTED", "DECLINED", "CANCELLED"},
    "ACCEPTED": {"INGESTING", "INVESTIGATING", "CANCELLED", "FAILED"},
    "INGESTING": {"INVESTIGATING", "FAILED", "CANCELLED"},
    "INVESTIGATING": {"CROSS_REVIEW", "DIAGNOSIS_READY", "WAITING_FOR_AI_QUOTA", "FAILED", "CANCELLED"},
    "CROSS_REVIEW": {"DIAGNOSIS_READY", "PROPOSAL_READY", "FAILED", "CANCELLED"},
    "WAITING_FOR_AI_QUOTA": {"INVESTIGATING", "FAILED", "CANCELLED"},
    "WAITING_FOR_AI_QUOTA": {"INVESTIGATING", "FAILED", "CANCELLED"},
    "DIAGNOSIS_READY": {"PROPOSAL_READY", "HUMAN_REVIEW", "FAILED", "CANCELLED"},
    "PROPOSAL_READY": {"HUMAN_REVIEW", "FAILED", "CANCELLED"},
    "HUMAN_REVIEW": {"COMMENT_POSTED", "APPROVED", "CANCELLED", "IMPLEMENTING", "FAILED"},
    "COMMENT_POSTED": {"AWAITING_MAINTAINER", "CANCELLED"},
    "AWAITING_MAINTAINER": {"MAINTAINER_AGREED", "CANCELLED"},
    "MAINTAINER_AGREED": {"APPROVED", "CANCELLED"},
    "APPROVED": {"IMPLEMENTING", "CANCELLED", "FAILED"},
    "IMPLEMENTING": {"TESTING", "FAILED", "CANCELLED"},
    "TESTING": {"FINAL_REVIEW", "PR_READY", "FAILED", "CANCELLED"},
    "FINAL_REVIEW": {"PR_READY", "IMPLEMENTING", "FAILED", "CANCELLED"},
    "PR_READY": {"PR_OPENED", "CANCELLED"},
    "DECLINED": {"PENDING_APPROVAL"},
    "CANCELLED": {"PENDING_APPROVAL"},
    "FAILED": {"PENDING_APPROVAL", "ACCEPTED", "CANCELLED"},
}

TERMINAL = {"DECLINED", "CANCELLED", "PR_OPENED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_repo(repo: str) -> str:
    value = repo.strip().strip("/")
    if value.count("/") != 1:
        raise ValueError("repo must be in OWNER/REPOSITORY form")
    owner, name = value.split("/", 1)
    if not owner or not name or any(ch.isspace() for ch in value):
        raise ValueError("invalid repository")
    return f"{owner}/{name}"


def make_job_id(repo: str, number: int) -> str:
    repo = normalize_repo(repo)
    if not isinstance(number, int) or number <= 0:
        raise ValueError("issue number must be a positive integer")
    raw = f"{repo}#{number}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def empty_store() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "jobs": {},
    }


def load_store(path: Path = STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        raise RuntimeError("invalid jobs.json structure")
    version = int(data.get("version", 1))
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"jobs.json version {version} is newer than supported {SCHEMA_VERSION}")
    data["version"] = SCHEMA_VERSION
    data.setdefault("updated_at", utc_now())
    return data


def save_store(data: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = SCHEMA_VERSION
    data["updated_at"] = utc_now()
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _history(job: dict[str, Any], event: str, **extra: Any) -> None:
    entry = {"at": utc_now(), "event": event}
    entry.update(extra)
    job.setdefault("history", []).append(entry)
    job["history"] = job["history"][-100:]


def create_discovered(data: dict[str, Any], *, repo: str, number: int, title: str,
                      url: str, score: int | None = None, bounty: dict | None = None,
                      labels: list[str] | None = None, trigger: list[str] | None = None) -> tuple[dict, bool]:
    repo = normalize_repo(repo)
    job_id = make_job_id(repo, number)
    now = utc_now()
    jobs = data["jobs"]
    existing = jobs.get(job_id)
    if existing:
        # Never move an accepted/declined/advanced job backwards automatically.
        existing["last_seen_at"] = now
        existing["latest"] = {
            "title": title[:300],
            "url": url,
            "score": score,
            "bounty": bounty,
            "labels": labels or [],
            "trigger": trigger or [],
        }
        return existing, False

    job = {
        "job_id": job_id,
        "repo": repo,
        "issue_number": number,
        "state": "PENDING_APPROVAL",
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "latest": {
            "title": title[:300],
            "url": url,
            "score": score,
            "bounty": bounty,
            "labels": labels or [],
            "trigger": trigger or [],
        },
        "decision": None,
        "history": [],
        "artifacts": {},
        "error": None,
    }
    _history(job, "DISCOVERED", trigger=trigger or [])
    _history(job, "PENDING_APPROVAL")
    jobs[job_id] = job
    return job, True


def transition(data: dict[str, Any], job_id: str, target: str, *, actor: str = "human",
               reason: str = "") -> dict[str, Any]:
    target = target.upper().strip()
    if target not in STATES:
        raise ValueError(f"unknown target state: {target}")
    job = data["jobs"].get(job_id)
    if not job:
        raise KeyError(f"job not found: {job_id}")
    current = job["state"]
    if target == current:
        return job
    allowed = CONTROL_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(f"transition {current} -> {target} is not allowed by control plane")
    now = utc_now()
    job["state"] = target
    job["updated_at"] = now
    job["decision"] = {"actor": actor, "at": now, "reason": reason[:1000]}
    _history(job, "STATE_CHANGED", from_state=current, to_state=target, actor=actor, reason=reason[:1000])
    return job


def validate_action(action: str) -> str:
    value = action.strip().upper()
    aliases = {
        "ACCEPT": "ACCEPT", "DECLINE": "DECLINE", "CANCEL": "CANCEL", "RESET": "RESET",
        "PREPARE_PROPOSAL": "PREPARE_PROPOSAL", "POST_COMMENT": "POST_COMMENT",
        "MAINTAINER_AGREED": "MAINTAINER_AGREED", "APPROVE_IMPLEMENTATION": "APPROVE_IMPLEMENTATION",
        "REJECT_IMPLEMENTATION": "REJECT_IMPLEMENTATION",
    }
    if value not in aliases:
        raise ValueError(f"unsupported action: {action}. Must be one of: {', '.join(sorted(aliases))}")
    return aliases[value]


def apply_action(data: dict[str, Any], job_id: str, action: str, *, actor: str = "human",
                 reason: str = "") -> dict[str, Any]:
    action = validate_action(action)
    job = data["jobs"].get(job_id)
    if not job:
        raise KeyError(f"job not found: {job_id}")

    current = job.get("state")

    # Idempotency checks to prevent duplicate work or corruption on repeated actions
    if action == "ACCEPT":
        # If already accepted or already progressed down the pipeline, don't re-execute or fail
        accepted_pipeline_states = {
            "ACCEPTED", "INGESTING", "INVESTIGATING", "CROSS_REVIEW", "DIAGNOSIS_READY",
            "PROPOSAL_READY", "HUMAN_REVIEW", "COMMENT_POSTED", "AWAITING_MAINTAINER",
            "MAINTAINER_AGREED", "APPROVED", "IMPLEMENTING", "TESTING", "FINAL_REVIEW",
            "PR_READY", "PR_OPENED"
        }
        if current in accepted_pipeline_states:
            return job

    if action == "DECLINE" and current == "DECLINED":
        return job

    if action == "CANCEL" and current == "CANCELLED":
        return job

    if action == "RESET" and current == "PENDING_APPROVAL":
        return job

    target = {
        "ACCEPT": "ACCEPTED",
        "DECLINE": "DECLINED",
        "CANCEL": "CANCELLED",
        "RESET": "PENDING_APPROVAL",
        "PREPARE_PROPOSAL": "PROPOSAL_READY",
        "POST_COMMENT": "COMMENT_POSTED",
        "MAINTAINER_AGREED": "MAINTAINER_AGREED",
        "APPROVE_IMPLEMENTATION": "APPROVED",
        "REJECT_IMPLEMENTATION": "CANCELLED",
    }[action]
    return transition(data, job_id, target, actor=actor, reason=reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub Bounty Radar job control")
    parser.add_argument("action", choices=["create", "accept", "decline", "cancel", "reset", "status", "list"])
    parser.add_argument("--job-id")
    parser.add_argument("--repo")
    parser.add_argument("--number", type=int)
    parser.add_argument("--title", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--score", type=int)
    parser.add_argument("--reason", default="")
    parser.add_argument("--actor", default=os.environ.get("GITHUB_ACTOR", "workflow"))
    args = parser.parse_args(argv)
    data = load_store()
    try:
        if args.action == "create":
            if not args.repo or not args.number:
                raise ValueError("create requires --repo and --number")
            job, created = create_discovered(data, repo=args.repo, number=args.number,
                                             title=args.title, url=args.url, score=args.score)
            save_store(data)
            print(json.dumps({"created": created, "job": job}, indent=2, ensure_ascii=False))
        elif args.action == "list":
            print(json.dumps(list(data["jobs"].values()), indent=2, ensure_ascii=False))
        elif args.action == "status":
            if not args.job_id:
                raise ValueError("status requires --job-id")
            job = data["jobs"].get(args.job_id)
            if not job:
                raise KeyError(f"job not found: {args.job_id}")
            print(json.dumps(job, indent=2, ensure_ascii=False))
        else:
            if not args.job_id:
                raise ValueError(f"{args.action} requires --job-id")
            job = apply_action(data, args.job_id, args.action, actor=args.actor, reason=args.reason)
            save_store(data)
            print(json.dumps(job, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
