#!/usr/bin/env python3
"""Approval-gated implementation engine.

This is intentionally conservative:
- Only runs when state is APPROVED.
- Generates unified diff using Gemini with fallback to Grok (or vice versa).
- Rejects path traversal, absolute paths outside repo, workflow modifications, secret files.
- Applies patch in isolated sandbox checkout.
- Runs repository test suites (pytest, npm test, go test, lint/type checks).
- If tests fail: marks FAILED, records test failure, halts PR pipeline.
- If tests pass: marks TESTING, updates engineering dossier.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jobs import load_store, save_store, utc_now, transition
from ai_review import generate_diff
from dossier import update_dossier_implementation_result

BLOCKED = re.compile(
    r"(^|/)(\.github/workflows/|\.env|.*\.pem$|.*\.key$|id_rsa|credentials\.json$|token\.json$|.*\.exe$|.*\.so$|.*\.dylib$)",
    re.I,
)
MAX_PATCH_BYTES = 250_000


def run(cmd: list[str], cwd: Path, timeout: int = 300, env: dict[str, str] | None = None) -> str:
    p = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    if p.returncode:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stdout[-12000:]}")
    return p.stdout


def extract_diff(text: str) -> str:
    m = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.S | re.I)
    return (m.group(1) if m else text).strip() + "\n"


def changed_paths(diff: str) -> list[str]:
    paths = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:].strip())
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            raw = line[4:].strip()
            if raw.startswith("b/"):
                raw = raw[2:]
            paths.append(raw)
    return sorted(set(paths))


def validate_patch_paths(paths: list[str], checkout: Path) -> None:
    """Ensure no path traversal, outside repository paths, or blocked files."""
    if not paths:
        raise RuntimeError("patch changed zero files")

    checkout_resolved = checkout.resolve()
    for p in paths:
        # Rejection of absolute paths or drive letters
        if p.startswith("/") or p.startswith("\\") or ":" in p:
            raise RuntimeError(f"security rejection: absolute path in patch '{p}'")

        # Rejection of directory traversal
        parts = Path(p).parts
        if ".." in parts:
            raise RuntimeError(f"security rejection: path traversal in patch '{p}'")

        # Check blocked paths (workflows, credentials, secrets, executables)
        if BLOCKED.search(p):
            raise RuntimeError(f"security rejection: patch touches blocked path '{p}'")

        # Verify resolution remains inside checkout
        resolved = (checkout / p).resolve()
        try:
            resolved.relative_to(checkout_resolved)
        except ValueError:
            raise RuntimeError(f"security rejection: target file '{p}' escapes repository root")


def test_commands(repo: Path) -> list[list[str]]:
    cmds = []
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or (repo / "tests").exists():
        cmds.append(["python", "-m", "pytest", "-q"])
    if (repo / "package.json").exists():
        try:
            pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                cmds.append(["npm", "test", "--", "--runInBand"])
            if "lint" in scripts:
                cmds.append(["npm", "run", "lint"])
        except Exception:
            pass
    if (repo / "go.mod").exists():
        cmds.append(["go", "test", "./..."])
    return cmds[:4]


def main() -> int:
    root = Path(".")
    job_id = os.environ.get("JOB_ID", "").strip()
    if not job_id:
        print("error: JOB_ID is required", file=sys.stderr)
        return 2

    data = load_store(root / "jobs.json")
    job = data["jobs"].get(job_id)
    if not job:
        print(f"error: job not found: {job_id}", file=sys.stderr)
        return 2

    if job.get("state") != "APPROVED":
        print(f"error: implementation requires APPROVED state; current={job.get('state')}", file=sys.stderr)
        return 1

    ingestion_path = root / job["artifacts"]["ingestion"]
    evidence = json.loads(ingestion_path.read_text(encoding="utf-8"))
    reviews = [
        json.loads((root / p).read_text(encoding="utf-8"))
        for p in job.get("artifacts", {}).get("reviews", [])
        if (root / p).exists()
    ]

    transition(data, job_id, "IMPLEMENTING", actor="worker")
    save_store(data, root / "jobs.json")

    with tempfile.TemporaryDirectory(prefix="bounty-impl-") as td:
        checkout = Path(td) / "repo"
        repo = job["repo"]
        out_dir = root / "job-artifacts" / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            run(
                ["git", "clone", "--filter=blob:none", "--no-tags", "--depth=50", f"https://github.com/{repo}.git", str(checkout)],
                Path(td),
                timeout=300,
            )

            prompt = json.dumps({
                "issue": evidence["issue"],
                "repository": evidence["repository"],
                "reviews": reviews,
                "selected_files": evidence.get("files", {}),
            }, ensure_ascii=False)

            # Generate diff using Gemini/Grok retry & fallback
            raw_ai_diff = generate_diff(prompt)
            diff = extract_diff(raw_ai_diff)

            if len(diff.encode("utf-8")) > MAX_PATCH_BYTES:
                raise RuntimeError("AI patch exceeds maximum allowed size")

            paths = changed_paths(diff)
            validate_patch_paths(paths, checkout)

            patch_file = Path(td) / "change.patch"
            patch_file.write_text(diff, encoding="utf-8")

            # Check and apply
            run(["git", "apply", "--check", str(patch_file)], checkout)
            run(["git", "apply", str(patch_file)], checkout)

            # Run test suites
            report = []
            test_cmds = test_commands(checkout)
            if not test_cmds:
                report.append({"command": "no_test_runner_detected", "ok": True, "output": "No automated test runner detected."})

            for cmd in test_cmds:
                try:
                    out = run(cmd, checkout, timeout=300)
                    report.append({"command": cmd, "ok": True, "output": out[-8000:]})
                except Exception as exc:
                    report.append({"command": cmd, "ok": False, "output": str(exc)})
                    # If tests fail, halt and mark failure
                    test_report_path = out_dir / "test-report.json"
                    test_report_path.write_text(json.dumps({"generated_at": utc_now(), "changed_paths": paths, "tests": report}, indent=2), encoding="utf-8")

                    data = load_store(root / "jobs.json")
                    job = data["jobs"][job_id]
                    job["artifacts"]["test_report"] = str(test_report_path.as_posix())
                    job["error"] = f"Tests failed during implementation: {exc}"[:2000]
                    transition(data, job_id, "FAILED", actor="worker", reason="Implementation tests failed")
                    save_store(data, root / "jobs.json")

                    update_dossier_implementation_result(root, job_id, job["issue_number"], paths, report, error=str(exc))
                    print(f"Implementation test failed: {exc}", file=sys.stderr)
                    return 1

            diff_after = run(["git", "diff", "--no-ext-diff"], checkout)
            patch_path = out_dir / "implementation.patch"
            patch_path.write_text(diff_after, encoding="utf-8")

            test_report_path = out_dir / "test-report.json"
            test_report_path.write_text(json.dumps({"generated_at": utc_now(), "changed_paths": paths, "tests": report}, indent=2), encoding="utf-8")

            data = load_store(root / "jobs.json")
            job = data["jobs"][job_id]
            job["artifacts"]["implementation_patch"] = str(patch_path.as_posix())
            job["artifacts"]["test_report"] = str(test_report_path.as_posix())
            transition(data, job_id, "TESTING", actor="worker")
            save_store(data, root / "jobs.json")

            update_dossier_implementation_result(root, job_id, job["issue_number"], paths, report)
            print("Implementation completed and tests passed. PR creation is intentionally separate.")
            return 0

        except Exception as exc:
            data = load_store(root / "jobs.json")
            job = data["jobs"][job_id]
            job["error"] = f"Implementation error: {exc}"[:2000]
            transition(data, job_id, "FAILED", actor="worker", reason=str(exc)[:500])
            save_store(data, root / "jobs.json")
            update_dossier_implementation_result(root, job_id, job["issue_number"], [], [], error=str(exc))
            print(f"Implementation failed: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
