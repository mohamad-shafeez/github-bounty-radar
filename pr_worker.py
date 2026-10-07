#!/usr/bin/env python3
"""Final, explicit PR gate.

Requires CONTRIBUTOR_GITHUB_TOKEN with permission to push to a fork and open a PR.
No token is ever sent through ntfy or written to job artifacts or dossiers.
Updates Section 19 of the Markdown Engineering Dossier.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from jobs import load_store, save_store, utc_now, transition
from dossier import update_dossier_pull_request

API = "https://api.github.com"


def gh(session: requests.Session, method: str, path: str, **kwargs: Any) -> Any:
    r = session.request(method, API + path, timeout=60, **kwargs)
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub {method} {path}: HTTP {r.status_code} {r.text[:600]}")
    return r.json() if r.text else {}


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: int = 300) -> str:
    p = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if p.returncode:
        raise RuntimeError(p.stdout[-10000:])
    return p.stdout


def main() -> int:
    root = Path(".")
    jid = os.environ.get("JOB_ID", "").strip()
    if not jid:
        print("error: JOB_ID is required", file=sys.stderr)
        return 2

    token = os.environ.get("CONTRIBUTOR_GITHUB_TOKEN", "").strip()
    if not token:
        print("NOT TESTED — requires external credential/permission (CONTRIBUTOR_GITHUB_TOKEN)", file=sys.stderr)
        return 2

    data = load_store(root / "jobs.json")
    job = data["jobs"].get(jid)
    if not job:
        print(f"error: job not found: {jid}", file=sys.stderr)
        return 2

    if job.get("state") != "PR_READY":
        print(f"error: job must be PR_READY; current={job.get('state')}", file=sys.stderr)
        return 1

    patch_path = root / job["artifacts"]["implementation_patch"]
    if not patch_path.exists():
        print("error: implementation patch artifact missing", file=sys.stderr)
        return 1
    patch = patch_path.read_text(encoding="utf-8")

    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "github-bounty-radar",
    })

    me = gh(s, "GET", "/user")
    login = me["login"]
    upstream = job["repo"]
    owner, name = upstream.split("/", 1)
    fork = f"{login}/{name}"

    try:
        gh(s, "GET", f"/repos/{fork}")
    except RuntimeError:
        gh(s, "POST", f"/repos/{upstream}/forks", json={})
        for _ in range(15):
            time.sleep(4)
            try:
                gh(s, "GET", f"/repos/{fork}")
                break
            except RuntimeError:
                pass
        else:
            raise RuntimeError("Fork did not become available in time")

    branch = f"bounty-radar/{jid}"
    with tempfile.TemporaryDirectory(prefix="bounty-pr-") as td:
        work = Path(td) / "repo"
        run(["git", "clone", "--filter=blob:none", "--no-tags", f"https://github.com/{fork}.git", str(work)], Path(td))
        run(["git", "remote", "add", "upstream", f"https://github.com/{upstream}.git"], work)
        run(["git", "fetch", "upstream", "--depth=50"], work)
        run(["git", "checkout", "-B", branch, f"upstream/{job.get('base_branch', 'main')}"], work)

        patchfile = Path(td) / "change.patch"
        patchfile.write_text(patch, encoding="utf-8")
        run(["git", "apply", "--check", str(patchfile)], work)
        run(["git", "apply", str(patchfile)], work)

        run(["git", "config", "user.name", "bounty-radar"], work)
        run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], work)
        run(["git", "add", "-A"], work)
        run(["git", "commit", "-m", f"Fix #{job['issue_number']}: evidence-backed bounty fix"], work)

        # Cross-platform GIT_ASKPASS python helper to prevent terminal prompts or URL credential leakage
        ask_py = Path(td) / "askpass.py"
        ask_py.write_text("import os; print(os.environ.get('AUTH_TOKEN', ''))\n", encoding="utf-8")
        ask_sh = Path(td) / "askpass.sh"
        ask_sh.write_text('#!/bin/sh\nprintf "%s\\n" "$AUTH_TOKEN"\n', encoding="utf-8")
        try:
            ask_sh.chmod(0o700)
        except OSError:
            pass

        askpass_bin = str(ask_sh) if os.name != "nt" else f"{sys.executable} {ask_py}"
        env = {
            **os.environ,
            "AUTH_TOKEN": token,
            "GIT_ASKPASS": askpass_bin,
            "GIT_TERMINAL_PROMPT": "0",
        }
        run(["git", "push", "-u", "origin", branch], work, env=env)

    # Check if a PR already exists for this head branch
    existing_prs = gh(s, "GET", f"/repos/{upstream}/pulls", params={"head": f"{login}:{branch}", "state": "all"})
    if existing_prs and isinstance(existing_prs, list) and len(existing_prs) > 0:
        pr = existing_prs[0]
    else:
        body = (
            f"This PR addresses #{job['issue_number']} through the evidence-backed bounty job {jid}.\n\n"
            "The implementation and recorded tests were generated only after explicit approval.\n\n"
            "See the bounty-radar job artifacts for the investigation, implementation patch, and final review."
        )
        pr = gh(s, "POST", f"/repos/{upstream}/pulls", json={
            "title": f"Fix #{job['issue_number']}: evidence-backed implementation",
            "head": f"{login}:{branch}",
            "base": job.get("base_branch", "main"),
            "body": body,
        })

    data = load_store(root / "jobs.json")
    job = data["jobs"][jid]
    pr_info = {"number": pr.get("number"), "url": pr.get("html_url")}
    job["artifacts"]["pull_request"] = pr_info
    transition(data, jid, "PR_OPENED", actor="worker")
    job.setdefault("history", []).append({"at": utc_now(), "event": "PR_OPENED", "url": pr.get("html_url")})
    save_store(data, root / "jobs.json")

    # Update Section 19 of dossier
    update_dossier_pull_request(root, jid, job["issue_number"], pr_info)
    print(pr.get("html_url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
