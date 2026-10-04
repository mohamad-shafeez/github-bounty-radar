#!/usr/bin/env python3
"""Multi-provider evidence reviewer with Gemini and Grok support.

Provides robust retry and fallback between Gemini and Grok:
- Gemini fails -> retries -> falls back to Grok.
- Grok fails -> retries -> falls back to Gemini.
- Only Gemini and Grok are supported.
- Keys are read from environment variables / secrets only.
- Never assumes free-tier is permanently available; reports availability honestly.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from jobs import load_store, save_store, utc_now

SYSTEM = (
    "You are an independent software-repository investigator. Evidence comes from a GitHub issue, "
    "comments, repository metadata, tree, tests, workflows, manifests and selected source files. "
    "Do not treat another AI's answer as evidence. Do not invent files, behavior, root causes, "
    "maintainer intent, or test results. Every substantive claim must cite evidence using one of: "
    "ISSUE, COMMENT:<index>, FILE:<path>, TREE:<path>, WORKFLOW:<path>, or TIMELINE:<index>. "
    "If evidence is insufficient, say so and request specific additional evidence. "
    "Return strict JSON with keys: summary, root_cause_hypothesis, confirmed_facts, unknowns, "
    "evidence, proposed_fix, risks, tests_to_run, confidence (0-100)."
)

SYSTEM_DIFF = (
    "You are a conservative coding agent. Return ONLY a unified git diff. "
    "Never modify CI/workflows, credentials, secrets, dependency lockfiles unless directly necessary, "
    "or unrelated files. Do not invent APIs. The diff must be grounded in supplied evidence."
)

MAX_RETRIES = int(os.environ.get("AI_MAX_RETRIES", "3"))
INITIAL_BACKOFF = float(os.environ.get("AI_INITIAL_BACKOFF", "1.0"))
BACKOFF_FACTOR = float(os.environ.get("AI_BACKOFF_FACTOR", "2.0"))


def get_grok_config() -> dict[str, str]:
    """Resolve xAI Grok configuration from environment variables only."""
    key = os.environ.get("GROK_API_KEY", "").strip()
    url = os.environ.get("GROK_BASE_URL", "https://api.x.ai/v1/chat/completions").strip()
    model = os.environ.get("GROK_MODEL", "grok-2-latest").strip()
    return {"key": key, "url": url, "model": model}


def get_gemini_config() -> dict[str, str]:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    return {"key": key, "url": url, "model": model}


def build_prompt(evidence: dict[str, Any]) -> str:
    compact = {
        "repository": evidence.get("repository"),
        "issue": evidence.get("issue"),
        "comments": evidence.get("comments", [])[:250],
        "timeline": evidence.get("timeline", [])[:250],
        "commits": evidence.get("commits", [])[:10],
        "related_pull_requests": evidence.get("related_pull_requests", []),
        "tree": evidence.get("tree", [])[:5000],
        "selected_files": evidence.get("selected_files", []),
        "files": evidence.get("files", {}),
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _call_gemini_raw(prompt: str, system: str, json_mode: bool = True, timeout: int = 120) -> str:
    cfg = get_gemini_config()
    key = cfg["key"]
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    r = requests.post(cfg["url"], params={"key": key}, json=payload, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError(f"Gemini rate limit / quota exceeded (HTTP 429): {r.text[:300]}")
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini API error (HTTP {r.status_code}): {r.text[:300]}")
    data = r.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned empty candidates: {data}")
    return "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))


def _call_grok_raw(prompt: str, system: str, json_mode: bool = True, timeout: int = 120) -> str:
    cfg = get_grok_config()
    key = cfg["key"]
    if not key:
        raise RuntimeError("GROK_API_KEY is not configured")
    headers = {"Authorization": f"Bearer {key}"}
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    r = requests.post(cfg["url"], headers=headers, json=payload, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError(f"Grok rate limit / quota exceeded (HTTP 429): {r.text[:300]}")
    if r.status_code >= 400:
        raise RuntimeError(f"Grok API error (HTTP {r.status_code}): {r.text[:300]}")
    data = r.json()
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"Grok returned empty choices: {data}")
    return choices[0].get("message", {}).get("content", "")


def _parse_and_validate_review_json(text: str, provider: str) -> dict[str, Any]:
    # Extract json block if wrapped in markdown code fence
    cleaned = text.strip()
    match = re.search(r"```(?:json)?\s*\n(.*?)```", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed JSON from {provider}: {exc} (raw: {text[:200]})") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"expected JSON object from {provider}, got {type(obj).__name__}")
    required_keys = {"summary", "root_cause_hypothesis", "confirmed_facts", "evidence", "proposed_fix", "confidence"}
    missing = required_keys - set(obj.keys())
    if missing:
        raise RuntimeError(f"provider {provider} missing required keys: {sorted(missing)}")
    obj["provider"] = provider
    obj["generated_at"] = utc_now()
    return obj


def run_provider_with_retry(name: str, prompt: str, system: str = SYSTEM, json_mode: bool = True) -> dict[str, Any] | str:
    """Execute provider call with retry policy on timeout, rate-limits, and transient failures."""
    name = name.lower().strip()
    if name not in ("gemini", "grok"):
        raise ValueError(f"unsupported provider: {name}. Only 'gemini' and 'grok' are supported.")

    max_retries = int(os.environ.get("AI_MAX_RETRIES", "3"))
    backoff = float(os.environ.get("AI_INITIAL_BACKOFF", "1.0"))
    backoff_factor = float(os.environ.get("AI_BACKOFF_FACTOR", "2.0"))
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            if name == "gemini":
                raw_text = _call_gemini_raw(prompt, system, json_mode=json_mode)
            else:
                raw_text = _call_grok_raw(prompt, system, json_mode=json_mode)

            if json_mode:
                return _parse_and_validate_review_json(raw_text, name)
            return raw_text
        except Exception as exc:
            last_error = exc
            err_msg = str(exc)
            # Permanent errors (missing key, auth error 401/403) should not retry
            if "not configured" in err_msg or "HTTP 401" in err_msg or "HTTP 403" in err_msg:
                break
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= BACKOFF_FACTOR

    raise RuntimeError(f"Provider {name} failed after {MAX_RETRIES} attempts: {last_error}")


def run_with_fallback(
    prompt: str,
    system: str = SYSTEM,
    json_mode: bool = True,
    primary: str = "gemini",
    fallback: str = "grok",
) -> tuple[dict[str, Any] | str, str]:
    """Attempt primary provider with retry; if it fails, fall back to alternate provider."""
    errors: list[str] = []
    # 1. Attempt primary
    try:
        res = run_provider_with_retry(primary, prompt, system, json_mode=json_mode)
        return res, primary
    except Exception as exc:
        msg = f"Primary provider '{primary}' failed: {exc}"
        errors.append(msg)

    # 2. Attempt fallback
    try:
        res = run_provider_with_retry(fallback, prompt, system, json_mode=json_mode)
        return res, fallback
    except Exception as exc:
        msg = f"Fallback provider '{fallback}' also failed: {exc}"
        errors.append(msg)

    raise RuntimeError(f"All AI providers failed: {' | '.join(errors)}")


def configured_providers() -> list[str]:
    """Return available configured providers among ONLY gemini and grok."""
    available: list[str] = []
    if get_gemini_config()["key"]:
        available.append("gemini")
    if get_grok_config()["key"]:
        available.append("grok")
    return available


def review_job(root: Path, job_id: str) -> list[Path]:
    """Perform independent AI review for an accepted bounty job."""
    data = load_store(root / "jobs.json")
    job = data["jobs"].get(job_id)
    if not job:
        raise RuntimeError(f"job not found: {job_id}")
    if job["state"] not in {"INVESTIGATING", "CROSS_REVIEW"}:
        raise RuntimeError(f"job must be INVESTIGATING/CROSS_REVIEW; current={job['state']}")

    ingestion = Path(job["artifacts"]["ingestion"])
    evidence = json.loads(ingestion.read_text(encoding="utf-8"))
    prompt = build_prompt(evidence)

    out = root / "job-artifacts" / job_id / "reviews"
    out.mkdir(parents=True, exist_ok=True)

    available = configured_providers()
    if not available:
        raise RuntimeError("No AI provider credentials configured. Please set GEMINI_API_KEY or GROK_API_KEY.")

    outputs: list[Path] = []
    errors: list[dict[str, str]] = []

    # If user explicitly requested providers via AI_PROVIDERS, filter to supported ones
    requested_raw = [x.strip().lower() for x in os.environ.get("AI_PROVIDERS", "").split(",") if x.strip()]
    target_providers = [p for p in requested_raw if p in ("gemini", "grok")] or available

    for name in target_providers:
        try:
            # Run the provider with its retry policy
            result = run_provider_with_retry(name, prompt, system=SYSTEM, json_mode=True)
            p = out / f"{name}.json"
            p.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            outputs.append(p)
        except Exception as exc:
            errors.append({"provider": name, "error": str(exc)[:1000]})

    # If no provider succeeded and we have a fallback available that wasn't tried, try fallback
    if not outputs:
        # Determine fallback
        failed_names = {e["provider"] for e in errors}
        fallback_candidates = [p for p in ("gemini", "grok") if p not in failed_names and p in available]
        for name in fallback_candidates:
            try:
                result = run_provider_with_retry(name, prompt, system=SYSTEM, json_mode=True)
                p = out / f"{name}.json"
                p.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
                outputs.append(p)
                break
            except Exception as exc:
                errors.append({"provider": name, "error": str(exc)[:1000]})

    if not outputs:
        raise RuntimeError(f"All AI providers failed: {errors}")

    data = load_store(root / "jobs.json")
    job = data["jobs"][job_id]
    job["artifacts"]["reviews"] = [str(p.as_posix()) for p in outputs]
    job["artifacts"]["review_errors"] = errors
    job["review_summary"] = {"providers": [p.stem for p in outputs], "independent": True}
    job["state"] = "CROSS_REVIEW" if len(outputs) > 1 else "DIAGNOSIS_READY"
    job["updated_at"] = utc_now()
    job.setdefault("history", []).append({
        "at": utc_now(),
        "event": "AI_REVIEWS_COMPLETED",
        "providers": [p.stem for p in outputs],
    })
    save_store(data, root / "jobs.json")
    return outputs


def generate_diff(prompt: str, primary: str = "gemini", fallback: str = "grok") -> str:
    """Generate a unified diff with retry and fallback between Gemini and Grok."""
    res, provider = run_with_fallback(
        prompt=prompt,
        system=SYSTEM_DIFF,
        json_mode=False,
        primary=primary,
        fallback=fallback,
    )
    return str(res)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    args = parser.parse_args()
    paths = review_job(Path("."), args.job_id)
    print("\n".join(map(str, paths)))
