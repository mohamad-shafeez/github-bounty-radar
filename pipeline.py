#!/usr/bin/env python3
"""Cloud pipeline helpers for accepted bounty jobs.

The pipeline is deliberately evidence-first:
1. ingest issue/repository evidence
2. optionally ask independent AI providers to interpret the evidence
3. store artifacts for human review

It does not post comments, push code, or open PRs automatically.
Those actions remain separate approval-gated operations.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import requests

from jobs import load_store, save_store, transition, utc_now

GITHUB_API = "https://api.github.com"
MAX_FILE_BYTES = int(os.environ.get("MAX_EVIDENCE_FILE_BYTES", "60000"))
MAX_FILES = int(os.environ.get("MAX_EVIDENCE_FILES", "350"))
MAX_COMMENTS = int(os.environ.get("MAX_ISSUE_COMMENTS", "250"))
MAX_TREE_ITEMS = int(os.environ.get("MAX_TREE_ITEMS", "5000"))

INTERESTING_NAMES = re.compile(
    r"(^|/)(README(?:\.[^/]*)?|CONTRIBUTING(?:\.[^/]*)?|package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|pyproject\.toml|requirements(?:[-_.][^/]*)?\.txt|go\.mod|Cargo\.toml|pom\.xml|build\.gradle|Dockerfile|docker-compose[^/]*|\.github/workflows/[^/]+\.ya?ml)$",
    re.I,
)
TEST_PATH = re.compile(r"(^|/)(test|tests|spec|__tests__)(/|$)|(^|/)[^/]*(test|spec)[^/]*$", re.I)
SOURCE_EXT = re.compile(r"\.(py|js|jsx|ts|tsx|go|rs|java|kt|rb|php|cs|cpp|c|h|hpp|swift|vue|svelte)$", re.I)


class PipelineError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-bounty-radar-pipeline",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, **params: Any) -> Any:
        url = path if path.startswith("http") else GITHUB_API + path
        r = self.session.get(url, params=params or None, timeout=30)
        if r.status_code >= 400:
            raise PipelineError(f"GitHub GET {path} -> HTTP {r.status_code}: {r.text[:500]}")
        return r.json()

    def paged(self, path: str, *, per_page: int = 100, limit: int = 250, **params: Any) -> list[Any]:
        out: list[Any] = []
        page = 1
        while len(out) < limit:
            batch = self.get(path, page=page, per_page=min(per_page, limit-len(out)), **params)
            if not isinstance(batch, list):
                raise PipelineError(f"expected list from {path}")
            before = len(out)
            out.extend(batch)
            if len(batch) < per_page or len(out) >= limit or len(out) == before:
                break
            page += 1
        return out[:limit]

    def issue_bundle(self, repo: str, number: int) -> dict[str, Any]:
        issue = self.get(f"/repos/{repo}/issues/{number}")
        comments = self.paged(f"/repos/{repo}/issues/{number}/comments", limit=MAX_COMMENTS)
        timeline = self.paged(f"/repos/{repo}/issues/{number}/timeline", limit=MAX_COMMENTS)
        return {"issue": issue, "comments": comments, "timeline": timeline}

    def repo_meta(self, repo: str) -> dict[str, Any]:
        return self.get(f"/repos/{repo}")

    def tree(self, repo: str, branch: str) -> list[dict[str, Any]]:
        data = self.get(f"/repos/{repo}/git/trees/{branch}", recursive="1")
        tree = data.get("tree", [])
        if len(tree) > MAX_TREE_ITEMS:
            tree = tree[:MAX_TREE_ITEMS]
        return tree

    def contents(self, repo: str, path: str, ref: str) -> str:
        data = self.get(f"/repos/{repo}/contents/{path}", ref=ref)
        if data.get("encoding") != "base64":
            return ""
        import base64
        raw = base64.b64decode(data.get("content", ""))
        return raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")


def choose_files(tree: list[dict[str, Any]]) -> list[str]:
    paths = [x.get("path", "") for x in tree if x.get("type") == "blob"]
    selected: list[str] = []
    for p in paths:
        if INTERESTING_NAMES.search(p) or TEST_PATH.search(p):
            selected.append(p)
    for p in paths:
        if len(selected) >= MAX_FILES:
            break
        if p not in selected and SOURCE_EXT.search(p):
            selected.append(p)
    return selected[:MAX_FILES]


def relevant_prs(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prs = []
    seen = set()
    for event in timeline:
        if event.get("event") not in {"cross-referenced", "connected"}:
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if "pull_request" in source:
            number = source.get("number")
            if number and number not in seen:
                seen.add(number)
                prs.append({"number": number, "title": source.get("title"), "url": source.get("html_url")})
    return prs


def ingest_job(job: dict[str, Any], out_dir: Path, token: str | None = None) -> Path:
    gh = GitHub(token)
    repo = job["repo"]
    number = job["issue_number"]
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle = gh.issue_bundle(repo, number)
    meta = gh.repo_meta(repo)
    default_branch = meta.get("default_branch") or "main"
    tree = gh.tree(repo, default_branch)
    selected = choose_files(tree)

    files: dict[str, str] = {}
    for path in selected:
        try:
            files[path] = gh.contents(repo, path, default_branch)
        except PipelineError:
            continue

    commits = []
    try:
        commits = gh.paged(f"/repos/{repo}/commits", per_page=10, limit=10)
    except Exception:
        pass

    evidence = {
        "schema": 1,
        "generated_at": utc_now(),
        "job_id": job["job_id"],
        "repository": {
            "full_name": repo,
            "default_branch": default_branch,
            "html_url": meta.get("html_url"),
            "description": meta.get("description"),
            "language": meta.get("language"),
            "license": (meta.get("license") or {}).get("spdx_id"),
            "archived": meta.get("archived"),
        },
        "issue": bundle["issue"],
        "comments": bundle["comments"],
        "timeline": bundle["timeline"],
        "commits": commits,
        "related_pull_requests": relevant_prs(bundle["timeline"]),
        "tree": tree,
        "selected_files": selected,
        "files": files,
        "limits": {
            "max_file_bytes": MAX_FILE_BYTES,
            "max_files": MAX_FILES,
            "max_tree_items": MAX_TREE_ITEMS,
            "max_comments": MAX_COMMENTS,
        },
        "evidence_notes": [
            "Repository evidence is authoritative over AI interpretation.",
            "Selected files are prioritized manifests, docs, workflows, tests, and source files.",
            "A selected-file list is not a claim that every byte of the repository was ingested.",
        ],
    }
    path = out_dir / "ingestion.json"
    path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    return path, evidence


def clone_repo(repo: str, destination: Path, ref: str | None = None, depth: int = 50) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    cmd = ["git", "clone", "--filter=blob:none", "--no-tags", f"--depth={depth}"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(destination)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
    return destination


def evidence_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def accept_and_ingest(job_id: str, root: Path, token: str | None) -> Path:
    data = load_store(root / "jobs.json")
    job = data["jobs"].get(job_id)
    if not job:
        raise PipelineError(f"job not found: {job_id}")
    if job["state"] != "ACCEPTED":
        raise PipelineError(f"job must be ACCEPTED before ingestion; current={job['state']}")
    transition(data, job_id, "INGESTING", actor="worker")
    save_store(data, root / "jobs.json")
    out = root / "job-artifacts" / job_id
    try:
        path, evidence = ingest_job(job, out, token)
        data = load_store(root / "jobs.json")
        job = data["jobs"][job_id]
        job["artifacts"]["ingestion"] = str(path.as_posix())
        job["artifacts"]["ingestion_sha256"] = evidence_hash(path)
        job["base_branch"] = evidence.get("repository", {}).get("default_branch", "main")
        transition(data, job_id, "INVESTIGATING", actor="worker")
        save_store(data, root / "jobs.json")
        return path
    except Exception as exc:
        data = load_store(root / "jobs.json")
        job = data["jobs"][job_id]
        job["error"] = str(exc)[:2000]
        job["state"] = "FAILED"
        job["updated_at"] = utc_now()
        job.setdefault("history", []).append({"at": utc_now(), "event": "WORKER_FAILED", "error": str(exc)[:2000]})
        save_store(data, root / "jobs.json")
        raise
