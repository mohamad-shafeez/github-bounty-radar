#!/usr/bin/env python3
"""Free GitHub bounty radar.

Designed for GitHub Actions + ntfy.sh.
No paid AI/API is required.

Environment variables:
  GITHUB_TOKEN      GitHub Actions provides this automatically.
  NTFY_TOPIC       Private ntfy topic; required for real notifications.
  NTFY_SERVER      Optional, defaults to https://ntfy.sh
  DRY_RUN=1         Print notifications instead of sending them.
  SEND_TEST_ALERT=1 Send one ntfy test and exit.
  RADAR_STATE_FILE  Optional state path, defaults to state.json.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from jobs import create_discovered, load_store as load_job_store, save_store as save_job_store
from targets import load_targets, build_queries, TargetConfigError

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

try:
    TARGETS = load_targets(Path(os.environ.get("RADAR_TARGETS_FILE", "targets.json")))
    QUERIES = build_queries(TARGETS)
except TargetConfigError as exc:
    # Keep imports usable for local unit tests; the runtime main() reports the
    # configuration error rather than silently scanning an unintended scope.
    TARGETS = []
    QUERIES = []
    TARGET_CONFIG_ERROR = str(exc)
else:
    TARGET_CONFIG_ERROR = ""

MIN_BOUNTY_AMOUNT = 50
BOUNTY_LABEL_REGEX = r"(?i)bounty|💎"
IGNORE_LABEL_REGEX = r"(?i)rewarded|wontfix|duplicate|invalid"
IGNORE_REPOS = {
    "SecureBananaLabs/bug-bounty",
    "relayhop/sn-monetization-runtime",
}
IGNORE_TITLE_REGEX = ""

PLATFORM_PATTERNS = {
    "Upwork": r"https?://(?:www\.)?upwork\.com/jobs/",
}

ALERT_RULES = {
    "new_issue": True,
    "bounty_added": True,
    "bounty_increased": True,
    "opportunity_label_added": True,
    "bounty_label_added": True,
    "unassigned": True,
    "bounty_possible": False,
    "comments_surge": False,
    "any_new_comment": False,
}
COMMENTS_SURGE_THRESHOLD = 10

HIGH_SCORE = 40
MIN_ALERT_SCORE = 10
AMOUNT_POINTS = [(1000, 30), (500, 25), (250, 20), (100, 12), (50, 8), (1, 4)]
EVIDENCE_POINTS = {"confirmed": 10, "uncertain": 3, "none": 0}
RECENCY_POINTS = [(15, 20), (60, 14), (360, 8), (1440, 3)]
COMMENT_POINTS = [(0, 8), (2, 5), (5, 2)]
MANY_COMMENTS_LIMIT = 10
MANY_COMMENTS_PENALTY = -5
NO_ASSIGNEE_POINTS = 8
OPPORTUNITY_LABEL_POINTS = 15
TECH_POINTS_EACH = 4
TECH_POINTS_MAX = 12

TECH_KEYWORDS = {
    "Python": [r"\bpython\b"],
    "JavaScript": [r"\bjavascript\b", r"(?<![\w.])js\b"],
    "Node.js": [r"\bnode\.js\b", r"\bnodejs\b"],
    "Express": [r"\bexpress\.?js\b", r"\bexpress\s+(?:server|middleware|router|app)\b"],
    "Flask": [r"\bflask\b"],
    "REST APIs": [r"\brest(?:ful)?\s+apis?\b", r"\brest\s+endpoints?\b"],
    "MongoDB": [r"\bmongodb\b", r"\bmongoose\b"],
    "Firebase": [r"\bfirebase\b", r"\bfirestore\b"],
    "React": [r"\bReact\b", r"\bReact(?:JS|\.js)\b", r"\breact-native\b"],
    "LLM/API integration": [
        r"\bLLMs?\b", r"\bopenai\b", r"\bchatgpt\b", r"\banthropic\b",
        r"\blarge language models?\b", r"\bapi integrations?\b", r"\bwebhooks?\b",
    ],
    "Backend": [r"\bback-?end\b"],
}

PER_PAGE = 100
MAX_PAGES = 2
BASELINE_MAX_PAGES = 2
WINDOW_OVERLAP_MINUTES = 90
NEW_ISSUE_MAX_AGE_HOURS = 48
FIRST_RUN_SEND_ALERTS = False
REALERT_COOLDOWN_HOURS = 12
MAX_ALERTS_PER_RUN = 15
MAX_TRACKED_ISSUES = 1000
STATE_HEARTBEAT_HOURS = 12
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
MAX_RATE_LIMIT_WAIT = 90
SEARCH_PAUSE_SECONDS = 2.5

QUERY_DEFAULTS = {
    "enabled": True,
    "weight": 0,
    "alert_new": "any",
    "opportunity_labels": [],
    "high_requires_opportunity_label": False,
    "assignee_matters": True,
}

# -----------------------------------------------------------------------------
# CONSTANTS / RUNTIME
# -----------------------------------------------------------------------------

STATE_VERSION = 1
STATE_PATH = Path(os.environ.get("RADAR_STATE_FILE", "state.json"))
JOBS_PATH = Path(os.environ.get("BOUNTY_JOBS_FILE", "jobs.json"))
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "mohamad-shafeez/github-bounty-radar")
APPROVAL_WORKFLOW_URL = f"https://github.com/{GITHUB_REPOSITORY}/actions/workflows/job-control.yml"
SEARCH_URL = "https://api.github.com/search/issues"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
DRY_RUN = os.environ.get("DRY_RUN") == "1"

EVENT_ORDER = [
    "bounty_increased",
    "bounty_added",
    "opportunity_label_added",
    "bounty_label_added",
    "unassigned",
    "new_issue",
    "bounty_possible",
    "comments_surge",
    "any_new_comment",
]

EVENT_STYLE = {
    "new_issue": ("🟠", "NEW ISSUE", "orange_circle"),
    "bounty_added": ("💰", "BOUNTY ADDED", "moneybag"),
    "bounty_increased": ("📈", "BOUNTY INCREASED", "chart_with_upwards_trend"),
    "opportunity_label_added": ("🏷️", "LABEL ADDED", "label"),
    "bounty_label_added": ("🏷️", "BOUNTY LABEL ADDED", "label"),
    "unassigned": ("👤", "UNASSIGNED", "bust_in_silhouette"),
    "bounty_possible": ("❓", "POSSIBLE BOUNTY", "question"),
    "comments_surge": ("💬", "COMMENT SURGE", "speech_balloon"),
    "any_new_comment": ("💬", "NEW COMMENT", "speech_balloon"),
}

SECRETS = [
    value for value in (
        os.environ.get("GITHUB_TOKEN", ""),
        os.environ.get("NTFY_TOPIC", ""),
    ) if value
]


class SearchError(Exception):
    pass


class StateError(Exception):
    pass


class ConfigError(Exception):
    pass


# -----------------------------------------------------------------------------
# LOGGING / HELPERS
# -----------------------------------------------------------------------------

def redact(value):
    text = str(value)
    for secret in SECRETS:
        text = text.replace(secret, "***")
    return text


def log(message):
    print(redact(message), flush=True)


def warn(message):
    message = redact(message)
    print(f"::warning::{message}" if IN_ACTIONS else f"[warn] {message}", flush=True)


def fail(message):
    message = redact(message)
    print(f"::error::{message}" if IN_ACTIONS else f"[error] {message}", flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes_since(dt, now):
    if not dt:
        return 10**9
    return max(0, int((now - dt).total_seconds() // 60))


def ago(dt, now):
    if dt is None:
        return "unknown time ago"
    minutes = minutes_since(dt, now)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if minutes < 1440:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = minutes // 1440
    return f"{days} day{'s' if days != 1 else ''} ago"


def money(amount, currency):
    if amount is None:
        return "?"
    value = float(amount)
    text = f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
    return f"{currency or '$'}{text}"


def lower_set(values):
    return {str(value).lower() for value in values}


# -----------------------------------------------------------------------------
# BOUNTY DETECTION
# -----------------------------------------------------------------------------

NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
AMOUNT_RE = re.compile(
    rf"(?<![\w$])(?:"
    rf"(?P<sym>US\$|\$|€|£)\s?(?P<n1>{NUM})(?P<k1>\s?[kK])?(?!\w)"
    rf"|(?P<code>USD|EUR|GBP)\s?(?P<n2>{NUM})(?P<k2>\s?[kK])?(?!\w)"
    rf"|(?P<n3>{NUM})(?P<k3>\s?[kK])?\s?(?P<unit>USD|EUR|GBP|dollars?|euros?)\b"
    rf")"
)
CURRENCY_SYMBOL = {
    "$": "$", "US$": "$", "USD": "$", "DOLLAR": "$", "DOLLARS": "$",
    "€": "€", "EUR": "€", "EURO": "€", "EUROS": "€",
    "£": "£", "GBP": "£",
}
BOUNTY_WORD_RE = re.compile(r"(?i)\b(?:bount(?:y|ies)|reward|prize|payout)\b")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
RANK_CONFIRMED = 70


def find_amounts(text):
    found = []
    for match in AMOUNT_RE.finditer(text or ""):
        number = match.group("n1") or match.group("n2") or match.group("n3")
        kilo = match.group("k1") or match.group("k2") or match.group("k3")
        unit = match.group("sym") or match.group("code") or match.group("unit")
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        if kilo:
            value *= 1000
        if not 0 < value <= 1_000_000:
            continue
        if unit == "$" and re.fullmatch(r"\d", number) and not kilo:
            continue
        found.append({
            "amount": value,
            "currency": CURRENCY_SYMBOL.get(unit.upper(), "$"),
            "start": match.start(),
            "end": match.end(),
            "raw": match.group(0).strip(),
        })
    return found


def strip_code(text):
    text = HTML_COMMENT_RE.sub(" ", text or "")
    text = CODE_BLOCK_RE.sub(" ", text)
    return INLINE_CODE_RE.sub(" ", text)


def enclosing_bracket(text, start, end):
    left = text.rfind("[", 0, start)
    right = text.find("]", end)
    if left == -1 or right == -1:
        return None
    if "]" in text[left:start] or "[" in text[end:right]:
        return None
    return text[left + 1:right]


def keyword_near(text, start, end, distance=40):
    window = text[max(0, start - distance):end + distance]
    return bool(BOUNTY_WORD_RE.search(window))


def detect_bounty(title, body, labels):
    candidates = []

    for amount in find_amounts(title):
        inside = enclosing_bracket(title, amount["start"], amount["end"])
        if inside is not None and (
            inside.strip() == amount["raw"] or BOUNTY_WORD_RE.search(inside)
        ):
            candidates.append((100, amount["amount"], amount["currency"], "title",
                               f'"{amount["raw"]}" inside square brackets in title'))
        elif keyword_near(title, amount["start"], amount["end"]):
            candidates.append((80, amount["amount"], amount["currency"], "title",
                               f'"{amount["raw"]}" near a bounty word in title'))

    for label in labels:
        amounts = find_amounts(label)
        if len(amounts) == 1:
            amount = amounts[0]
            rest = re.sub(r"[\W_]+", " ", label[:amount["start"]] + " " + label[amount["end"]:]).strip().lower()
            if rest in ("", "bounty", "reward", "prize"):
                candidates.append((90, amount["amount"], amount["currency"], "label",
                                   f'amount label "{label}"'))
                continue
        if re.search(BOUNTY_LABEL_REGEX, label) and not re.search(IGNORE_LABEL_REGEX, label):
            candidates.append((20, None, None, "label", f'bounty-style label "{label}"'))

    clean = strip_code(body)[:20000]
    for amount in find_amounts(clean):
        line_start = clean.rfind("\n", 0, amount["start"]) + 1
        line_end = clean.find("\n", amount["end"])
        if line_end == -1:
            line_end = len(clean)
        before = clean[line_start:amount["start"]]
        if re.fullmatch(r"[\s>*_#\-•|]*(?:bount(?:y|ies)|reward|prize|payout)\W{0,6}", before, re.I):
            candidates.append((70, amount["amount"], amount["currency"], "body",
                               f'line like "Bounty: {amount["raw"]}" in body'))
        elif keyword_near(clean[line_start:line_end], amount["start"] - line_start,
                          amount["end"] - line_start):
            candidates.append((40, amount["amount"], amount["currency"], "body",
                               f'"{amount["raw"]}" near bounty wording in body'))

    platforms = [
        name for name, pattern in PLATFORM_PATTERNS.items()
        if re.search(pattern, body or "", re.I)
    ]
    if platforms and not candidates:
        candidates.append((10, None, None, "platform", f"link to {platforms[0]} in body"))

    if not candidates:
        return {
            "status": "none", "amount": None, "currency": None,
            "source": None, "pattern": None,
        }, platforms

    rank, amount, currency, source, pattern = sorted(
        candidates, key=lambda item: (-item[0], -(item[1] or 0))
    )[0]
    status = "confirmed" if rank >= RANK_CONFIRMED and amount is not None else "uncertain"
    return {
        "status": status,
        "amount": amount,
        "currency": currency,
        "source": source,
        "pattern": pattern,
    }, platforms


def qualifies(bounty):
    return (
        bounty["status"] == "confirmed"
        and (bounty["amount"] or 0) >= MIN_BOUNTY_AMOUNT
    )


def describe_bounty(bounty):
    if bounty["status"] == "none":
        return "No bounty detected"
    if bounty["status"] == "confirmed":
        return f'{money(bounty["amount"], bounty["currency"])} (confirmed: {bounty["pattern"]})'
    amount = f' {money(bounty["amount"], bounty["currency"])}' if bounty["amount"] else ""
    return f"Possible bounty{amount} (uncertain: {bounty['pattern']})"


# -----------------------------------------------------------------------------
# TECHNOLOGY MATCHING / SNAPSHOTS
# -----------------------------------------------------------------------------

TECH_PATTERNS = {
    name: [re.compile(pattern, re.I if name != "React" else 0) for pattern in patterns]
    for name, patterns in TECH_KEYWORDS.items()
}


def match_tech(title, body, labels):
    text = f"{title}\n{(body or '')[:8000]}\n{' '.join(labels)}"
    return [
        name for name, patterns in TECH_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    ]


def make_snapshot(item):
    labels = [label.get("name", "") for label in item.get("labels", []) if isinstance(label, dict)]
    assignees = [assignee.get("login", "") for assignee in item.get("assignees", []) if isinstance(assignee, dict)]
    if not assignees and isinstance(item.get("assignee"), dict):
        assignees = [item["assignee"].get("login", "")]

    repository_url = item.get("repository_url", "")
    repo = repository_url.split("repos/")[-1] if "repos/" in repository_url else "unknown/unknown"
    title = item.get("title") or ""
    body = item.get("body") or ""
    bounty, platforms = detect_bounty(title, body, labels)

    return {
        "number": item.get("number"),
        "repo": repo,
        "title": title[:200],
        "url": item.get("html_url", ""),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "comments": int(item.get("comments") or 0),
        "labels": labels,
        "assignees": assignees,
        "author": (item.get("user") or {}).get("login", ""),
        "bounty": bounty,
        "platforms": platforms,
        "tech": match_tech(title, body, labels),
    }


# -----------------------------------------------------------------------------
# SCORING / CONFIG
# -----------------------------------------------------------------------------

def has_opportunity_label(snapshot, config):
    return bool(lower_set(snapshot["labels"]) & lower_set(config["opportunity_labels"]))


def score_issue(snapshot, config, reference_time, now):
    parts = []
    bounty = snapshot["bounty"]

    if bounty["status"] == "confirmed" and bounty["amount"]:
        for minimum, points in AMOUNT_POINTS:
            if bounty["amount"] >= minimum:
                parts.append((f"bounty {money(bounty['amount'], bounty['currency'])}", points))
                break

    evidence = EVIDENCE_POINTS[bounty["status"]]
    if evidence:
        parts.append((f"{bounty['status']} bounty evidence", evidence))

    age = minutes_since(reference_time, now)
    for max_age, points in RECENCY_POINTS:
        if age <= max_age:
            parts.append((f"fresh ({age} min)", points))
            break

    if config["assignee_matters"] and not snapshot["assignees"]:
        parts.append(("no assignee", NO_ASSIGNEE_POINTS))

    for max_comments, points in COMMENT_POINTS:
        if snapshot["comments"] <= max_comments:
            parts.append((f"{snapshot['comments']} comments", points))
            break
    else:
        if snapshot["comments"] > MANY_COMMENTS_LIMIT:
            parts.append((f"{snapshot['comments']} comments (busy)", MANY_COMMENTS_PENALTY))

    if has_opportunity_label(snapshot, config):
        parts.append(("opportunity label", OPPORTUNITY_LABEL_POINTS))
    if config["weight"]:
        parts.append(("search weight", config["weight"]))
    if snapshot["tech"]:
        parts.append((
            f"tech match ({', '.join(snapshot['tech'])})",
            min(TECH_POINTS_EACH * len(snapshot["tech"]), TECH_POINTS_MAX),
        ))

    return sum(points for _, points in parts), parts


def query_config(entry):
    return {**QUERY_DEFAULTS, **entry}


def validate_config():
    if TARGET_CONFIG_ERROR:
        raise ConfigError(TARGET_CONFIG_ERROR)
    names = set()
    for entry in QUERIES:
        if not entry.get("name") or not entry.get("q"):
            raise ConfigError("Every query needs a name and q")
        if entry["name"] in names:
            raise ConfigError(f"Duplicate query name: {entry['name']}")
        names.add(entry["name"])
        if query_config(entry)["alert_new"] not in ("any", "bounty", "never"):
            raise ConfigError(f"{entry['name']}: alert_new must be any, bounty, or never")

    unknown = set(ALERT_RULES) - set(EVENT_ORDER)
    if unknown:
        raise ConfigError(f"Unknown ALERT_RULES keys: {sorted(unknown)}")


def merge_configs(configs):
    strength = {"any": 2, "bounty": 1, "never": 0}
    return {
        "weight": max(config["weight"] for config in configs),
        "alert_new": max((config["alert_new"] for config in configs), key=lambda value: strength[value]),
        "opportunity_labels": sorted({label for config in configs for label in config["opportunity_labels"]}),
        "high_requires_opportunity_label": any(config["high_requires_opportunity_label"] for config in configs),
        "assignee_matters": any(config["assignee_matters"] for config in configs),
    }


def query_hash(entry):
    return hashlib.sha1(entry["q"].encode("utf-8")).hexdigest()[:10]


# -----------------------------------------------------------------------------
# GITHUB
# -----------------------------------------------------------------------------

def rate_limit_wait(response):
    text = (response.text or "").lower()
    limited = (
        response.status_code == 429
        or (
            response.status_code == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in text
                or "retry-after" in response.headers
            )
        )
    )
    if not limited:
        return None

    retry_after = response.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return int(retry_after)

    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(1, int(reset) - int(time.time()))
    return 60


def github_search(session, query, page):
    params = {
        "q": query,
        "sort": "updated",
        "order": "desc",
        "per_page": PER_PAGE,
        "page": page,
    }
    last_problem = "unknown problem"

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = session.get(SEARCH_URL, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_problem = f"network error: {type(exc).__name__}"
        else:
            wait = rate_limit_wait(response)
            if wait is not None:
                if wait > MAX_RATE_LIMIT_WAIT:
                    raise SearchError(f"rate limited; reset is {wait}s away")
                warn(f"rate limited; waiting {wait + 1}s (attempt {attempt}/{HTTP_RETRIES})")
                time.sleep(wait + 1)
                last_problem = "rate limited"
                continue

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise SearchError("GitHub returned invalid JSON") from exc
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise SearchError("GitHub response has no items list")
                return data

            if response.status_code in (401, 403):
                raise SearchError(f"HTTP {response.status_code}: token rejected or access denied")
            if response.status_code in (400, 404, 422):
                raise SearchError(f"HTTP {response.status_code}: GitHub rejected the query")
            last_problem = f"HTTP {response.status_code}"

        if attempt < HTTP_RETRIES:
            delay = 2 ** attempt
            warn(f"search problem ({last_problem}); retrying in {delay}s")
            time.sleep(delay)

    raise SearchError(f"failed after {HTTP_RETRIES} attempts ({last_problem})")


def github_get_issue(session, repo, number):
    url = f"https://api.github.com/repos/{repo}/issues/{number}"
    last_problem = "unknown problem"
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = session.get(
                url,
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_problem = f"network error: {type(exc).__name__}"
        else:
            wait = rate_limit_wait(response)
            if wait is not None:
                if wait > MAX_RATE_LIMIT_WAIT:
                    raise SearchError(f"rate limited; reset is {wait}s away")
                time.sleep(wait + 1)
                last_problem = "rate limited"
                continue
            if response.status_code == 200:
                item = response.json()
                if "pull_request" in item:
                    return [], True
                return [item], True
            last_problem = f"HTTP {response.status_code}: {response.text[:200]}"
        if attempt < HTTP_RETRIES:
            time.sleep(attempt)
    raise SearchError(f"failed exact issue lookup after {HTTP_RETRIES} attempts ({last_problem})")


def run_query(session, entry, watermark):
    exact_issue = entry.get("exact_issue")
    if exact_issue:
        item = github_get_issue(session, entry["repo"], int(exact_issue))
        return item, True

    cutoff = None
    if watermark:
        parsed = parse_ts(watermark)
        cutoff = parsed - timedelta(minutes=WINDOW_OVERLAP_MINUTES) if parsed else None

    max_pages = MAX_PAGES if cutoff else BASELINE_MAX_PAGES
    items = []
    complete = True

    for page in range(1, max_pages + 1):
        data = github_search(session, entry["q"], page)
        batch = data["items"]
        total = data.get("total_count", 0)
        items.extend(batch)
        log(f"  [search] {entry['name']}: page {page} -> {len(batch)} results (GitHub total {total})")

        if data.get("incomplete_results"):
            warn(f"{entry['name']}: GitHub marked these search results incomplete")
            complete = False

        if not batch or page * PER_PAGE >= total:
            break

        last_updated = parse_ts(batch[-1].get("updated_at"))
        if cutoff and last_updated and last_updated < cutoff:
            break

        if page == max_pages:
            if cutoff:
                warn(f"{entry['name']}: more changed issues exist than MAX_PAGES covers; some may be missed")
            break

        time.sleep(SEARCH_PAUSE_SECONDS)

    return items, complete


# -----------------------------------------------------------------------------
# STATE
# -----------------------------------------------------------------------------

def new_state():
    return {"version": STATE_VERSION, "saved_at": None, "queries": {}, "issues": {}}


def load_state(path):
    if not path.exists():
        return new_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"{path} cannot be read; refusing to overwrite it") from exc

    if (
        not isinstance(state, dict)
        or state.get("version") != STATE_VERSION
        or not isinstance(state.get("issues"), dict)
        or not isinstance(state.get("queries"), dict)
    ):
        raise StateError(f"{path} has an unexpected format; refusing to overwrite it")
    return state


def comparable(state):
    copy = json.loads(json.dumps(state))
    copy["saved_at"] = None
    for record in copy["issues"].values():
        record.pop("last_seen", None)
    return json.dumps(copy, sort_keys=True)


def serialize_state(state):
    compact = {"separators": (",", ":"), "sort_keys": True, "ensure_ascii": False}
    issues = state["issues"]
    lines = ",\n".join(
        f"{json.dumps(key)}:{json.dumps(issues[key], **compact)}"
        for key in sorted(issues)
    )
    return (
        '{"issues":{\n'
        + lines
        + '\n},"queries":'
        + json.dumps(state["queries"], **compact)
        + ',"saved_at":'
        + json.dumps(state["saved_at"])
        + ',"version":'
        + json.dumps(STATE_VERSION)
        + "}\n"
    )


def save_state_atomically(state, path):
    text = serialize_state(state)
    json.loads(text)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def prune_state(state):
    issues = state["issues"]
    excess = len(issues) - MAX_TRACKED_ISSUES
    if excess <= 0:
        return 0
    keys = sorted(issues, key=lambda key: issues[key].get("updated_at") or "")[:excess]
    for key in keys:
        del issues[key]
    return excess


# -----------------------------------------------------------------------------
# CHANGE DETECTION
# -----------------------------------------------------------------------------

def event_key(event_type, detail):
    return f"{event_type}:{detail}" if detail else event_type


def find_changes(snapshot, old, config, baseline_silent, now):
    events = []
    skips = []

    if old is None:
        if baseline_silent:
            return [], ["first run of this search: recorded as baseline"]

        created = parse_ts(snapshot["created_at"])
        age_hours = minutes_since(created, now) / 60
        opportunity = has_opportunity_label(snapshot, config)

        if age_hours <= NEW_ISSUE_MAX_AGE_HOURS:
            if config["alert_new"] == "never":
                skips.append("new issue, but alert_new is never")
            elif config["alert_new"] == "bounty" and not qualifies(snapshot["bounty"]):
                skips.append(f"new issue without confirmed bounty >= {MIN_BOUNTY_AMOUNT}")
            else:
                events.append({"type": "new_issue", "detail": "", "previous": None})
        elif opportunity:
            label = next(
                label for label in snapshot["labels"]
                if label.lower() in lower_set(config["opportunity_labels"])
            )
            events.append({
                "type": "opportunity_label_added",
                "detail": label.lower(),
                "previous": "not tracked before",
                "labels": [label],
            })
        else:
            skips.append(f"old issue ({int(age_hours)}h) first seen: recorded silently")
        return events, skips

    old_bounty = old["bounty"]
    new_bounty = snapshot["bounty"]

    if qualifies(new_bounty):
        if not qualifies(old_bounty):
            if old_bounty["status"] == "none":
                before = "no bounty"
            elif old_bounty["status"] == "uncertain":
                before = "possible bounty only (uncertain)"
            else:
                before = f"below minimum ({money(old_bounty['amount'], old_bounty['currency'])})"
            events.append({
                "type": "bounty_added",
                "detail": f"{new_bounty['amount']:.0f}",
                "previous": before,
            })
        elif (
            new_bounty["amount"] > old_bounty["amount"]
            and new_bounty["currency"] == old_bounty["currency"]
        ):
            events.append({
                "type": "bounty_increased",
                "detail": f"{new_bounty['amount']:.0f}",
                "previous": money(old_bounty["amount"], old_bounty["currency"]),
            })
    elif new_bounty["status"] == "uncertain" and old_bounty["status"] == "none":
        events.append({"type": "bounty_possible", "detail": "", "previous": "no bounty"})

    old_labels = lower_set(old["labels"])
    opportunity_labels = lower_set(config["opportunity_labels"])
    for label in snapshot["labels"]:
        if label.lower() in old_labels:
            continue
        if label.lower() in opportunity_labels:
            events.append({
                "type": "opportunity_label_added",
                "detail": label.lower(),
                "previous": "label was not present",
                "labels": [label],
            })
        elif re.search(BOUNTY_LABEL_REGEX, label) and not re.search(IGNORE_LABEL_REGEX, label):
            events.append({
                "type": "bounty_label_added",
                "detail": label.lower(),
                "previous": "label was not present",
                "labels": [label],
            })

    if config["assignee_matters"] and old["assignees"] and not snapshot["assignees"]:
        events.append({
            "type": "unassigned",
            "detail": "",
            "previous": f"was assigned to {', '.join(old['assignees'])}",
        })

    grown = snapshot["comments"] - old["comments"]
    if grown >= COMMENTS_SURGE_THRESHOLD:
        events.append({
            "type": "comments_surge",
            "detail": "",
            "previous": f"{old['comments']} comments",
        })
    if grown > 0:
        events.append({
            "type": "any_new_comment",
            "detail": str(snapshot["comments"]),
            "previous": f"{old['comments']} comments",
        })

    return events, skips


def decide(snapshot, old, config, baseline_silent, now):
    events, skips = find_changes(snapshot, old, config, baseline_silent, now)
    decision = {"events": [], "skips": skips, "score": 0, "parts": [], "level": "NORMAL"}
    if not events:
        return decision

    if snapshot["repo"] in IGNORE_REPOS:
        decision["skips"].append(f"repo {snapshot['repo']} is ignored")
        return decision
    if IGNORE_TITLE_REGEX and re.search(IGNORE_TITLE_REGEX, snapshot["title"]):
        decision["skips"].append("title matches IGNORE_TITLE_REGEX")
        return decision
    if any(re.search(IGNORE_LABEL_REGEX, label) for label in snapshot["labels"]):
        decision["skips"].append("issue has an ignored label")
        return decision

    history = (old or {}).get("alerts", [])
    cooldown_start = now - timedelta(hours=REALERT_COOLDOWN_HOURS)
    recent_keys = {
        alert["key"]
        for alert in history
        if (parse_ts(alert.get("at")) or now) >= cooldown_start
    }

    kept = []
    for event in events:
        key = event_key(event["type"], event["detail"])
        if not ALERT_RULES.get(event["type"], False):
            decision["skips"].append(f"{event['type']}: rule disabled")
        elif key in recent_keys:
            decision["skips"].append(f"{key}: already alerted within cooldown")
        else:
            event["key"] = key
            kept.append(event)

    if not kept:
        return decision

    kept.sort(key=lambda event: EVENT_ORDER.index(event["type"]))
    headline = kept[0]["type"]
    reference_time = (
        parse_ts(snapshot["created_at"])
        if headline == "new_issue"
        else parse_ts(snapshot["updated_at"])
    )
    score, parts = score_issue(snapshot, config, reference_time, now)
    decision.update(score=score, parts=parts)

    if score < MIN_ALERT_SCORE:
        decision["skips"].append(f"score {score} is below {MIN_ALERT_SCORE}")
        return decision

    level = "HIGH" if score >= HIGH_SCORE else "NORMAL"
    if level == "HIGH" and config["high_requires_opportunity_label"] and not has_opportunity_label(snapshot, config):
        level = "NORMAL"
        decision["skips"].append("not HIGH: opportunity label is missing")

    decision.update(events=kept, level=level)
    return decision


# -----------------------------------------------------------------------------
# NTFY
# -----------------------------------------------------------------------------

def build_notification(snapshot, decision, now, job_id=None):
    events = decision["events"]
    level = decision["level"]
    head = events[0]
    emoji, headline, tag = EVENT_STYLE[head["type"]]
    bounty = snapshot["bounty"]
    amount = money(bounty["amount"], bounty["currency"]) if bounty["status"] == "confirmed" else None

    if head["type"] == "new_issue" and amount:
        core = f"{amount} BOUNTY" if level == "HIGH" else f"NEW ISSUE — {amount} BOUNTY"
    elif head["type"] in ("bounty_added", "bounty_increased"):
        core = f"{headline} {amount}"
    elif "labels" in head:
        core = f"{headline}: {', '.join(head['labels'])}"
    else:
        core = headline

    title = f"🔥 HIGH PRIORITY — {core}" if level == "HIGH" else f"{emoji} {core}"
    assignee = ", ".join(snapshot["assignees"]) if snapshot["assignees"] else "No assignee"
    plural = "" if snapshot["comments"] == 1 else "s"

    lines = [
        f"{snapshot['repo']} #{snapshot['number']}",
        snapshot["title"],
        f"Labels: {', '.join(snapshot['labels']) or 'none'}",
        f"💬 {snapshot['comments']} comment{plural} · 👤 {assignee} · 🕒 Created {ago(parse_ts(snapshot['created_at']), now)}",
        f"💰 {describe_bounty(bounty)}",
    ]

    if head.get("previous") and head["type"] != "new_issue":
        lines.append(f"Previously: {head['previous']}")
    if len(events) > 1:
        lines.append("Also: " + ", ".join(EVENT_STYLE[event["type"]][1].lower() for event in events[1:]))
    if snapshot["tech"]:
        lines.append(f"🧰 Tech: {', '.join(snapshot['tech'])}")
    lines.append(f"Score {decision['score']}")

    actions = [{"action": "view", "label": "Open issue", "url": snapshot["url"]}]
    if job_id:
        lines.append(f"🆔 Job: {job_id}")
        lines.append("📱 Open approval workflow to ACCEPT or DECLINE")
        actions.append({"action": "view", "label": "Approve / Decline", "url": APPROVAL_WORKFLOW_URL})

    return {
        "title": title,
        "message": "\n".join(lines),
        "priority": 5 if level == "HIGH" else 3,
        "tags": (["fire"] if level == "HIGH" else []) + [tag],
        "click": snapshot["url"],
        "actions": actions,
    }


def send_ntfy(topic, payload):
    if DRY_RUN:
        log("  [dry-run] notification:")
        log(f"    {payload['title']}")
        for line in payload["message"].splitlines():
            log(f"    {line}")
        return True

    if not topic:
        warn("NTFY_TOPIC is empty")
        return False

    body = dict(payload)
    body["topic"] = topic

    for attempt in (1, 2):
        try:
            response = requests.post(NTFY_SERVER, json=body, timeout=15)
            if 200 <= response.status_code < 300:
                return True
            warn(f"ntfy HTTP {response.status_code} (attempt {attempt}/2)")
        except requests.RequestException as exc:
            warn(f"ntfy network error {type(exc).__name__} (attempt {attempt}/2)")
        if attempt == 1:
            time.sleep(2)
    return False


# -----------------------------------------------------------------------------
# RUN
# -----------------------------------------------------------------------------

def run():
    validate_config()

    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic and not DRY_RUN and os.environ.get("SEND_TEST_ALERT") != "1":
        fail("NTFY_TOPIC is not set")
        return 1

    if os.environ.get("SEND_TEST_ALERT") == "1":
        ok = send_ntfy(topic, {
            "title": "✅ Bounty Radar test",
            "message": "If you can read this, phone notifications work.",
            "priority": 3,
            "tags": ["white_check_mark"],
        })
        log("test notification sent" if ok else "test notification FAILED")
        return 0 if ok else 1

    token = os.environ.get("GITHUB_TOKEN", "")
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bounty-radar",
    })
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        warn("GITHUB_TOKEN not set; using unauthenticated GitHub requests")

    state = load_state(STATE_PATH)
    jobs = load_job_store(JOBS_PATH)
    jobs_before = json.dumps(jobs, sort_keys=True, ensure_ascii=False)
    before = comparable(state)
    now = now_utc()
    log(f"Bounty Radar run at {iso(now)} | tracked issues: {len(state['issues'])}")

    active = [entry for entry in QUERIES if query_config(entry)["enabled"]]
    if not active:
        raise ConfigError("No enabled searches")

    found = {}
    query_ok = {}
    baseline_queries = set()

    for index, entry in enumerate(active):
        name = entry["name"]
        saved = state["queries"].get(name, {})
        is_baseline = saved.get("q_hash") != query_hash(entry) or not saved.get("watermark")
        if is_baseline:
            baseline_queries.add(name)

        mode = "BASELINE" if is_baseline else "INCREMENTAL"
        log(f"Search '{name}': {mode} | {entry['q']}")

        try:
            items, complete = run_query(
                session,
                entry,
                None if is_baseline else saved["watermark"],
            )
        except SearchError as exc:
            fail(f"search '{name}' failed: {exc}; watermark unchanged")
            query_ok[name] = None
            continue

        query_ok[name] = {"items": items, "complete": complete}
        for item in items:
            if "pull_request" in item or not isinstance(item.get("id"), int):
                continue
            issue_id = str(item["id"])
            slot = found.setdefault(issue_id, {"item": item, "queries": []})
            slot["queries"].append(name)

        if index < len(active) - 1:
            time.sleep(SEARCH_PAUSE_SECONDS)

    successful_searches = sum(1 for value in query_ok.values() if value is not None)
    if successful_searches == 0:
        fail("Every search failed; state was not changed")
        return 1

    total_hits = sum(len(value["items"]) for value in query_ok.values() if value)
    log(f"Collected {total_hits} results -> {len(found)} unique issues")

    configs = {entry["name"]: query_config(entry) for entry in active}
    plans = []

    for issue_id, slot in found.items():
        snapshot = make_snapshot(slot["item"])
        old = state["issues"].get(issue_id)

        old_seen = set((old or {}).get("seen_by", []))
        current_seen = set(slot["queries"])
        seen_by = sorted((old_seen | current_seen) & set(configs))
        if not seen_by:
            continue

        config = merge_configs([configs[name] for name in seen_by])
        silent = (
            old is None
            and all(name in baseline_queries for name in slot["queries"])
            and not FIRST_RUN_SEND_ALERTS
        )
        decision = decide(snapshot, old, config, silent, now)

        plans.append({
            "id": issue_id,
            "snapshot": snapshot,
            "old": old,
            "seen_by": seen_by,
            "decision": decision,
            "queries": slot["queries"],
        })

        if old is None and not silent:
            bounty = snapshot["bounty"]
            amount = f" {money(bounty['amount'], bounty['currency'])}" if bounty["amount"] else ""
            log(
                f"  new issue {snapshot['repo']}#{snapshot['number']}: "
                f"bounty={bounty['status']}{amount} tech={snapshot['tech'] or '-'}"
            )

        for reason in decision["skips"]:
            if not reason.startswith("first run"):
                log(f"  skip {snapshot['repo']}#{snapshot['number']}: {reason}")

    # Alert highest-scoring opportunities first.
    to_alert = sorted(
        (plan for plan in plans if plan["decision"]["events"]),
        key=lambda plan: -plan["decision"]["score"],
    )

    sent = failed = deferred = 0
    failed_ids = set()

    for plan in to_alert:
        snapshot = plan["snapshot"]
        decision = plan["decision"]
        label = f"{snapshot['repo']}#{snapshot['number']}"

        if sent >= MAX_ALERTS_PER_RUN:
            deferred += 1
            failed_ids.add(plan["id"])
            log(f"  deferred {label}: MAX_ALERTS_PER_RUN reached")
            continue

        parts = ", ".join(f"{name} {points:+d}" for name, points in decision["parts"])
        log(
            f"  ALERT [{decision['level']}] {label} "
            f"events={[event['type'] for event in decision['events']]} "
            f"score={decision['score']} ({parts})"
        )

        # Create the durable job before notifying so the phone notification can
        # point to a deterministic job id. The job contains no credentials.
        job, created = create_discovered(
            jobs,
            repo=snapshot["repo"],
            number=int(snapshot["number"]),
            title=snapshot["title"],
            url=snapshot["url"],
            score=int(decision["score"]),
            bounty=snapshot["bounty"],
            labels=snapshot["labels"],
            trigger=[event["type"] for event in decision["events"]],
        )
        job_id = job["job_id"]
        payload = build_notification(snapshot, decision, now, job_id=job_id)
        if send_ntfy(topic, payload):
            sent += 1
            plan["sent_events"] = decision["events"]
        else:
            failed += 1
            failed_ids.add(plan["id"])
            warn(f"alert for {label} failed; it will be retried next run")

    # Only commit a snapshot when its pending alerts were handled successfully.
    for plan in plans:
        if plan["id"] in failed_ids:
            continue

        record = dict(plan["snapshot"])
        record.pop("url", None)
        record["seen_by"] = plan["seen_by"]
        record["last_seen"] = iso(now)

        history = list((plan["old"] or {}).get("alerts", []))
        for event in plan.get("sent_events", []):
            history.append({"key": event["key"], "at": iso(now)})
        record["alerts"] = history[-10:]
        state["issues"][plan["id"]] = record

    # Advance a query watermark only if that search completed and no issue from
    # that search has an unsent/deferred alert.
    for entry in active:
        name = entry["name"]
        result = query_ok[name]
        if result is None or not result["complete"]:
            continue

        pending_for_query = any(
            plan["id"] in failed_ids and name in plan["queries"]
            for plan in plans
        )
        if pending_for_query:
            continue

        stamps = [parse_ts(item.get("updated_at")) for item in result["items"]]
        stamps = [stamp for stamp in stamps if stamp]
        old_mark = state["queries"].get(name, {}).get("watermark")
        newest = iso(max(stamps)) if stamps else (old_mark or iso(now))
        if old_mark and old_mark > newest:
            newest = old_mark
        state["queries"][name] = {
            "q_hash": query_hash(entry),
            "watermark": newest,
        }

    removed = prune_state(state)

    jobs_changed = json.dumps(jobs, sort_keys=True, ensure_ascii=False) != jobs_before
    if jobs_changed:
        save_job_store(jobs, JOBS_PATH)
        log(f"Jobs saved: {len(jobs['jobs'])} jobs")

    if sent == 0 and failed > 0:
        fail("Alerts were attempted but none could be sent; state was not saved")
        return 1

    changed = comparable(state) != before
    last_saved = parse_ts(state.get("saved_at"))
    heartbeat_due = (
        last_saved is None
        or (now - last_saved) >= timedelta(hours=STATE_HEARTBEAT_HOURS)
    )

    if changed or heartbeat_due:
        state["saved_at"] = iso(now)
        save_state_atomically(state, STATE_PATH)
        size_kb = STATE_PATH.stat().st_size // 1024
        reason = "changes found" if changed else "periodic refresh"
        log(
            f"State saved ({reason}): {len(state['issues'])} issues, "
            f"{size_kb} KB, pruned {removed}"
        )
    else:
        log(f"State unchanged, not written: {len(state['issues'])} issues")

    log(
        f"Summary: alerts sent={sent} failed={failed} deferred={deferred} | "
        f"searches ok={successful_searches}/{len(active)}"
    )
    return 0


def main():
    try:
        return run()
    except (ConfigError, StateError) as exc:
        fail(str(exc))
        return 2
    except KeyboardInterrupt:
        fail("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
