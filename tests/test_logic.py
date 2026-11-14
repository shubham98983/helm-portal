from datetime import datetime, timedelta, timezone

from helm.logic import (
    approval_summary,
    audit_verdict,
    board_column,
    check_summary,
    detect_ai_pr,
    event_key,
    is_bot_author,
    required_approvers,
    review_states,
)


def pull():
    return {
        "state": "open",
        "draft": False,
        "labels": [],
        "head": {"ref": "feature/x", "sha": "head1"},
        "user": {"login": "author"},
        "merged_at": None,
    }


def review(login, state, sha="head1", submitted="2025-01-01T10:00:00Z", review_id=1):
    return {
        "id": review_id,
        "user": {"login": login},
        "state": state,
        "commit_id": sha,
        "submitted_at": submitted,
    }


def test_ai_detection_reasons_and_bot_are_separate():
    pr = pull()
    pr["labels"] = [{"name": "AI-GENERATED"}]
    pr["head"]["ref"] = "AI-fix/issue-1"
    commits = [{"commit": {"message": "fixes issue\nAI-Assisted: codex"}}]
    assert detect_ai_pr(pr, commits) == ["label", "branch", "commit trailer"]
    assert not is_bot_author(pr)
    pr["user"]["login"] = "example[bot]"
    assert is_bot_author(pr)
    assert detect_ai_pr(pr, []) == ["label", "branch"]
    pr["labels"] = []
    pr["head"]["ref"] = "ai/patch"
    assert detect_ai_pr(pr) == ["branch"]
    pr["head"]["ref"] = "ai-fix/patch"
    assert detect_ai_pr(pr) == ["branch"]


def test_review_state_comment_does_not_override_and_dismissed_clears():
    reviews = [
        review("alice", "APPROVED", "head1", review_id=1),
        review("alice", "COMMENTED", "newsha", "2025-01-01T11:00:00Z", 2),
    ]
    assert review_states(reviews, "head1")["alice"]["current"]
    assert review_states([review("alice", "APPROVED", "old")], "head1")["alice"]["stale"]
    states = review_states([review("alice", "APPROVED"), review("alice", "DISMISSED", review_id=2)], "head1")
    assert states["alice"]["state"] == "DISMISSED"
    assert not states["alice"]["current"]
    summary = approval_summary(["alice"], [review("alice", "APPROVED", "old")], "head1")
    assert summary["missing"] == ["alice"] and summary["stale_by"] == ["alice"]


def test_required_approvers_parsing_fallback_author_and_unknown():
    members = [
        {"github": "lead", "role": "lead"},
        {"github": "Bob", "role": "reviewer"},
        {"github": "member", "role": "member"},
    ]
    assert required_approvers("@BOB # owner\nlead # comment\n@bob\n", members, "lead") == ["bob"]
    assert required_approvers(None, members, "LEAD") == ["bob"]
    assert required_approvers("# no approvers\n", members, "author") == []
    assert required_approvers(None, [{"github": "m", "role": "member"}], "author") is None


def test_check_summary_classifies_every_status():
    states = [
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "queued",
        "in_progress",
        "success",
        "neutral",
        "skipped",
    ]
    result = check_summary([{"conclusion": state} for state in states])
    assert result == {
        "overall": "failing",
        "counts": {"failing": 4, "pending": 2, "passing": 3, "unknown": 0},
    }
    assert check_summary([{"status": "in_progress"}])["overall"] == "pending"
    assert check_summary([{"conclusion": "success"}])["overall"] == "passing"
    assert check_summary([])["overall"] == "unknown"


def test_board_column_order_and_completion_window():
    now = datetime.now(timezone.utc)
    approvals = {"missing": ["bob"], "changes_requested_by": []}
    assert board_column({**pull(), "draft": True}, {"overall": "failing"}, approvals) == "draft"
    assert board_column(pull(), {"overall": "pending"}, approvals) == "ci"
    assert board_column(pull(), {"overall": "passing"}, approvals) == "awaiting"
    assert (
        board_column(pull(), {"overall": "passing"}, {"missing": [], "changes_requested_by": []}) == "ready"
    )
    closed = {"state": "closed", "closed_at": (now - timedelta(days=13)).isoformat()}
    assert board_column(closed, {}, {}) == "done"
    closed["closed_at"] = (now - timedelta(days=15)).isoformat()
    assert board_column(closed, {}, {}) is None


def test_audit_compliant_violation_failing_checks_and_unknown():
    pr = {"merged_at": "2025-01-02T10:00:00Z", "merge_commit_sha": "merge-sha", "head": {"sha": "merge-sha"}}
    approved = review("alice", "APPROVED", "merge-sha", "2025-01-02T09:00:00Z")
    passing = [{"name": "tests", "head_sha": "merge-sha", "conclusion": "success"}]
    assert audit_verdict(pr, ["alice"], [approved], passing)["verdict"] == "compliant"
    after_merge = review("alice", "APPROVED", "merge-sha", "2025-01-02T11:00:00Z")
    result = audit_verdict(pr, ["alice"], [after_merge], passing)
    assert result["verdict"] == "violation" and "missing current approval" in result["reasons"][0]
    result = audit_verdict(
        pr, ["alice"], [approved], [{"name": "tests", "head_sha": "merge-sha", "conclusion": "failure"}]
    )
    assert result["verdict"] == "violation" and "checks were not passing" in result["reasons"][-1]
    assert audit_verdict(pr, None, [], [])["verdict"] == "unknown"


def test_event_key_is_stable_and_scoped():
    key = event_key("Org/Repo", "review", "123", "2025-01-01T00:00:00Z", "Alice")
    assert key == event_key("org/repo", "REVIEW", "123", "2025-01-01T00:00:00Z", "alice")
    assert key != event_key("org/other", "review", "123", "2025-01-01T00:00:00Z", "alice")
