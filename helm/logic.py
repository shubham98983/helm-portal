"""Pure domain rules for pull requests, checks, reviews, and audit events."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

AI_TRAILER = re.compile(r"^AI-Assisted:\s*\S+", re.IGNORECASE | re.MULTILINE)
FAILING_CHECKS = {"failure", "cancelled", "timed_out", "action_required"}
PENDING_CHECKS = {"queued", "in_progress"}
PASSING_CHECKS = {"success", "neutral", "skipped"}


def detect_ai_pr(pr: dict[str, Any], commits: list[dict[str, Any]] | None = None) -> list[str]:
    """Return the ordered AI contribution reasons for a pull request."""
    reasons: list[str] = []
    if any(str(label.get("name", "")).casefold() == "ai-generated" for label in pr.get("labels", [])):
        reasons.append("label")
    branch = str(pr.get("head", {}).get("ref", "")).casefold()
    if branch.startswith(("ai/", "ai-fix/")):
        reasons.append("branch")
    for commit in commits or []:
        message = commit.get("commit", {}).get("message", "")
        if AI_TRAILER.search(str(message)):
            reasons.append("commit trailer")
            break
    return reasons


def is_bot_author(pr: dict[str, Any]) -> bool:
    login = pr.get("user", {}).get("login", "")
    return str(login).casefold().endswith("[bot]")


def _timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def review_states(reviews: list[dict[str, Any]], head_sha: str) -> dict[str, dict[str, Any]]:
    """Resolve the latest meaningful review for every reviewer."""
    latest: dict[str, dict[str, Any]] = {}
    for review in sorted(
        reviews, key=lambda row: (_timestamp(row.get("submitted_at")), str(row.get("id", "")))
    ):
        reviewer = str(review.get("user", {}).get("login", "")).casefold()
        state = str(review.get("state", "")).upper()
        if not reviewer or state == "COMMENTED":
            continue
        latest[reviewer] = review
    result = {}
    for reviewer, review in latest.items():
        state = str(review.get("state", "")).upper()
        commit_id = review.get("commit_id")
        result[reviewer] = {
            "state": state,
            "commit_id": commit_id,
            "submitted_at": review.get("submitted_at"),
            "current": state == "APPROVED" and commit_id == head_sha,
            "stale": state == "APPROVED" and commit_id != head_sha,
            "blocking": state == "CHANGES_REQUESTED",
        }
    return result


def required_approvers(
    approvers_file: str | None, members: list[dict[str, Any]], author: str
) -> list[str] | None:
    """Parse repository approvers or fall back to configured leads/reviewers."""
    author = author.casefold()
    if approvers_file is None:
        configured = [
            m.get("github", "").casefold() for m in members if m.get("role") in {"lead", "reviewer"}
        ]
    else:
        configured = []
        for line in approvers_file.splitlines():
            username = line.partition("#")[0].strip().lstrip("@").strip().casefold()
            if username:
                configured.append(username)
    unique = list(dict.fromkeys(login for login in configured if login and login != author))
    if approvers_file is None and not unique:
        return None
    return unique


def check_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"failing": 0, "pending": 0, "passing": 0, "unknown": 0}
    for check in checks:
        status = str(check.get("conclusion") or check.get("status") or "unknown").casefold()
        bucket = (
            "failing"
            if status in FAILING_CHECKS
            else "pending"
            if status in PENDING_CHECKS
            else "passing"
            if status in PASSING_CHECKS
            else "unknown"
        )
        counts[bucket] += 1
    overall = (
        "failing"
        if counts["failing"]
        else "pending"
        if counts["pending"]
        else "passing"
        if checks and not counts["unknown"]
        else "unknown"
    )
    return {"overall": overall, "counts": counts}


def approval_summary(
    required: list[str] | None, reviews: list[dict[str, Any]], head_sha: str
) -> dict[str, Any]:
    states = review_states(reviews, head_sha)
    approved = sorted(
        user for user, state in states.items() if state["current"] and (required is None or user in required)
    )
    missing = sorted(user for user in (required or []) if user not in approved)
    stale = sorted(user for user, state in states.items() if state["stale"])
    changes_requested = sorted(user for user, state in states.items() if state["blocking"])
    return {
        "required": required,
        "approved_by": approved,
        "missing": missing,
        "stale_by": stale,
        "changes_requested_by": changes_requested,
    }


def board_column(
    pr: dict[str, Any], checks: dict[str, Any], approvals: dict[str, Any], now: datetime | None = None
) -> str | None:
    """Choose the kanban column; old completed PRs do not appear on the board."""
    now = now or datetime.now(timezone.utc)
    state = str(pr.get("state", "open")).casefold()
    merged_at = pr.get("merged_at")
    if state == "closed" or merged_at:
        timestamp = merged_at or pr.get("closed_at")
        closed = _timestamp(timestamp) if timestamp else now
        return "done" if now - closed <= timedelta(days=14) else None
    if pr.get("draft"):
        return "draft"
    if checks.get("overall") in {"failing", "pending"}:
        return "ci"
    if approvals.get("missing") or approvals.get("changes_requested_by"):
        return "awaiting"
    return "ready"


def audit_verdict(
    pr: dict[str, Any],
    required: list[str] | None,
    reviews: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    merge_sha: str | None = None,
) -> dict[str, Any]:
    """Evaluate a merged AI PR against required reviews and checks at merge."""
    if required is None:
        return {"verdict": "unknown", "reasons": ["required approvers are unknown"]}
    merged_at = pr.get("merged_at")
    sha = merge_sha or pr.get("merge_commit_sha") or pr.get("head", {}).get("sha")
    eligible_reviews = []
    for review in reviews:
        submitted = review.get("submitted_at")
        if not submitted or (merged_at and _timestamp(submitted) >= _timestamp(merged_at)):
            continue
        eligible_reviews.append(review)
    states = review_states(eligible_reviews, sha)
    reasons = []
    for reviewer in required:
        review = states.get(reviewer.casefold())
        if not review or not review["current"]:
            reasons.append(f"missing current approval from {reviewer}")
    blocking = [user for user, state in states.items() if state["blocking"]]
    if blocking:
        reasons.append(f"changes requested by {', '.join(sorted(blocking))}")
    matching_checks = [check for check in checks if not check.get("head_sha") or check.get("head_sha") == sha]
    check_result = check_summary(matching_checks)
    if check_result["overall"] != "passing":
        bad = [
            str(check.get("name", check.get("id", "check")))
            for check in matching_checks
            if str(check.get("conclusion") or check.get("status") or "unknown").casefold()
            not in PASSING_CHECKS
        ]
        reasons.append(f"checks were not passing: {', '.join(bad) if bad else check_result['overall']}")
    return {"verdict": "violation" if reasons else "compliant", "reasons": reasons}


def event_key(repo: str, kind: str, subject: str, occurred_at: str, actor: str = "") -> str:
    """Stable opaque key for idempotently storing a source event."""
    raw = "\0".join((repo.casefold(), kind.casefold(), str(subject), occurred_at, actor.casefold()))
    return hashlib.sha256(raw.encode()).hexdigest()
