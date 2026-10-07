#!/usr/bin/env python3
"""Deterministic final safety gate before PR creation.

Compares patch against safety criteria:
- Correctness and absence of regression/security risk.
- Verification that all recorded tests passed.
- Rejection of blocked paths, credentials, and dangerous modifications.
- Updates Section 18 of the Markdown Engineering Dossier.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from jobs import load_store, save_store, utc_now, transition
from dossier import update_dossier_final_review

BLOCKED = re.compile(
    r"(^|/)(\.github/workflows/|\.env|.*\.pem$|.*\.key$|id_rsa|credentials\.json$|token\.json$|.*\.exe$|.*\.so$)",
    re.I,
)


def main() -> int:
    root = Path(".")
    jid = os.environ.get("JOB_ID", "").strip()
    if not jid:
        print("error: JOB_ID is required", file=sys.stderr)
        return 2

    data = load_store(root / "jobs.json")
    job = data["jobs"].get(jid)
    if not job:
        print(f"error: job not found: {jid}", file=sys.stderr)
        return 2

    if job.get("state") not in {"TESTING", "FINAL_REVIEW"}:
        print(f"error: job must be in TESTING or FINAL_REVIEW; current={job.get('state')}", file=sys.stderr)
        return 1

    patch_rel = job.get("artifacts", {}).get("implementation_patch")
    if not patch_rel or not (root / patch_rel).exists():
        print("error: implementation_patch artifact missing", file=sys.stderr)
        return 1

    patch_text = (root / patch_rel).read_text(encoding="utf-8")
    if not patch_text.strip():
        print("error: implementation_patch is empty", file=sys.stderr)
        return 1

    test_rep_rel = job.get("artifacts", {}).get("test_report")
    if not test_rep_rel or not (root / test_rep_rel).exists():
        print("error: test_report artifact missing", file=sys.stderr)
        return 1

    test_report_data = json.loads((root / test_rep_rel).read_text(encoding="utf-8"))
    tests = test_report_data.get("tests", [])

    paths = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:].strip())
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            raw = line[4:].strip()
            if raw.startswith("b/"):
                raw = raw[2:]
            paths.append(raw)

    paths = sorted(set(paths))
    blocked = [p for p in paths if BLOCKED.search(p) or ".." in Path(p).parts or p.startswith("/") or ":" in p]

    report = {
        "generated_at": utc_now(),
        "job_id": jid,
        "changed_paths": paths,
        "blocked_paths": blocked,
        "tests": tests,
        "ok": len(blocked) == 0 and len(tests) > 0 and all(x.get("ok") for x in tests),
    }

    out = root / "job-artifacts" / jid / "final-review.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    job["artifacts"]["final_review"] = str(out.as_posix())

    if not report["ok"]:
        err_msg = f"Final review failed: blocked_paths={blocked}, all_tests_passed={all(x.get('ok') for x in tests)}"
        job["error"] = err_msg
        transition(data, jid, "FAILED", actor="worker", reason=err_msg[:500])
        save_store(data, root / "jobs.json")
        update_dossier_final_review(root, jid, job["issue_number"], report)
        print(f"error: {err_msg}", file=sys.stderr)
        return 1

    transition(data, jid, "PR_READY", actor="worker")
    job.setdefault("history", []).append({"at": utc_now(), "event": "FINAL_REVIEW_PASSED"})
    save_store(data, root / "jobs.json")

    # Update Section 18 of dossier
    update_dossier_final_review(root, jid, job["issue_number"], report)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
