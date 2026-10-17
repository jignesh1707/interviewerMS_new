"""One limited pause per interview, and skipping a question (scored as zero)."""

from datetime import datetime, timedelta, timezone

from app.services import storage as storage_module
from tests.test_plan_limits import ANSWER, HEADERS, _expire, client, create  # noqa: F401 (client is a fixture)


def _post(client, interview_id, action, **kwargs):
    return client.post(f"/api/v1/interviews/{interview_id}/{action}", headers=HEADERS, **kwargs)


def _status(client, interview_id):
    return client.get(f"/api/v1/interviews/{interview_id}/status", headers=HEADERS).json()


def _answer(client, interview_id, index):
    return _post(client, interview_id, "answers", json={"question_index": index, "transcript": ANSWER})


def _paused_since(interview_id, seconds_ago):
    """Pretend the pause started `seconds_ago` seconds ago."""
    store = storage_module.get_store()
    config = store.get_interview(interview_id)["config"]
    config["pause"]["paused_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    store.update_interview(interview_id, config=config)


def _time_passes(interview_id, seconds):
    """Pretend `seconds` went by: every stored moment (deadline, pause start) moves that far into the past."""
    store = storage_module.get_store()
    config = store.get_interview(interview_id)["config"]
    for key in ("deadline_at",):
        config[key] = (datetime.fromisoformat(config[key]) - timedelta(seconds=seconds)).isoformat()
    if (config.get("pause") or {}).get("paused_at"):
        moved = datetime.fromisoformat(config["pause"]["paused_at"]) - timedelta(seconds=seconds)
        config["pause"]["paused_at"] = moved.isoformat()
    store.update_interview(interview_id, config=config)


# ----------------------------------------------------------------------------- pause


def test_a_new_interview_offers_one_pause(client):
    status = create(client)["interview"]
    assert status["paused"] is False
    assert status["pause_available"] is True
    assert status["max_pause_seconds"] == 300


def test_a_paused_interview_refuses_answers_and_freezes_the_countdown(client):
    interview_id = create(client)["interview"]["id"]
    paused = _post(client, interview_id, "pause")
    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert body["paused"] is True and body["pause_available"] is False
    assert 0 < body["pause_seconds_remaining"] <= 300

    _time_passes(interview_id, 60)
    later = _status(client, interview_id)
    assert later["seconds_remaining"] == body["seconds_remaining"]  # the clock does not run while paused
    assert later["pause_seconds_remaining"] <= 240

    refused = _answer(client, interview_id, 0)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "interview_paused"


def test_resuming_moves_the_deadline_by_the_time_spent_paused(client):
    interview_id = create(client)["interview"]["id"]
    before = datetime.fromisoformat(_status(client, interview_id)["deadline_at"])
    _post(client, interview_id, "pause")
    _paused_since(interview_id, 120)

    resumed = _post(client, interview_id, "resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["paused"] is False
    after = datetime.fromisoformat(resumed.json()["deadline_at"])
    assert timedelta(seconds=118) <= after - before <= timedelta(seconds=125)
    assert _answer(client, interview_id, 0).status_code == 200


def test_only_one_pause_is_allowed_per_interview(client):
    interview_id = create(client)["interview"]["id"]
    _post(client, interview_id, "pause")
    _post(client, interview_id, "resume")
    again = _post(client, interview_id, "pause")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "pause_already_used"


def test_a_pause_ends_by_itself_after_the_maximum(client):
    interview_id = create(client)["interview"]["id"]
    before = datetime.fromisoformat(_status(client, interview_id)["deadline_at"])
    _post(client, interview_id, "pause")
    _paused_since(interview_id, 400)

    status = _status(client, interview_id)
    assert status["paused"] is False
    after = datetime.fromisoformat(status["deadline_at"])
    assert timedelta(seconds=299) <= after - before <= timedelta(seconds=301)  # capped at 5 minutes
    assert _answer(client, interview_id, 0).status_code == 200


def test_resuming_an_interview_that_is_not_paused_is_harmless(client):
    interview_id = create(client)["interview"]["id"]
    resumed = _post(client, interview_id, "resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["paused"] is False
    assert resumed.json()["pause_available"] is True


def test_a_pause_is_refused_once_time_is_up(client):
    interview_id = create(client)["interview"]["id"]
    _expire(interview_id)
    refused = _post(client, interview_id, "pause")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "time_limit_reached"


def test_a_paused_interview_can_still_be_finished(client):
    interview_id = create(client)["interview"]["id"]
    assert _answer(client, interview_id, 0).status_code == 200
    _post(client, interview_id, "pause")
    finished = _post(client, interview_id, "finish")
    assert finished.status_code == 200, finished.text


def test_a_finished_interview_cannot_be_paused(client):
    interview_id = create(client)["interview"]["id"]
    _answer(client, interview_id, 0)
    _post(client, interview_id, "finish")
    refused = _post(client, interview_id, "pause")
    assert refused.status_code == 409


# ----------------------------------------------------------------------------- skip


def test_skipping_moves_on_to_the_next_question(client):
    created = create(client)
    interview_id = created["interview"]["id"]
    skipped = _post(client, interview_id, "skip", json={"question_index": 0})
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["next_question_id"] == created["questions"][1]["id"]
    status = _status(client, interview_id)
    assert status["answered_count"] == 1 and status["skipped_count"] == 1


def test_a_skipped_question_cannot_be_answered_afterwards(client):
    interview_id = create(client)["interview"]["id"]
    _post(client, interview_id, "skip", json={"question_index": 0})
    late = _answer(client, interview_id, 0)
    assert late.status_code == 409


def test_a_question_already_answered_cannot_be_skipped(client):
    interview_id = create(client)["interview"]["id"]
    _answer(client, interview_id, 0)
    refused = _post(client, interview_id, "skip", json={"question_index": 0})
    assert refused.status_code == 409


def test_skipping_is_refused_while_paused_and_after_time_is_up(client):
    interview_id = create(client)["interview"]["id"]
    _post(client, interview_id, "pause")
    paused = _post(client, interview_id, "skip", json={"question_index": 0})
    assert paused.json()["error"]["code"] == "interview_paused"
    _post(client, interview_id, "resume")
    _expire(interview_id)
    late = _post(client, interview_id, "skip", json={"question_index": 0})
    assert late.json()["error"]["code"] == "time_limit_reached"


def test_finishing_needs_at_least_one_real_answer(client):
    interview_id = create(client)["interview"]["id"]
    _post(client, interview_id, "skip", json={"question_index": 0})
    refused = _post(client, interview_id, "finish")
    assert refused.status_code == 422
    assert "no recorded answers" in refused.json()["error"]["message"]


def _report(client, interview_id):
    finished = _post(client, interview_id, "finish")
    assert finished.status_code == 200, finished.text
    return finished.json()["report"]


def test_a_skipped_question_is_listed_in_the_report_with_a_zero_score(client):
    interview_id = create(client)["interview"]["id"]
    _answer(client, interview_id, 0)
    _post(client, interview_id, "skip", json={"question_index": 1})
    report = _report(client, interview_id)

    skipped = [item for item in report["per_question"] if item["skipped"]]
    assert len(skipped) == 1 and skipped[0]["overall"] == 0
    assert report["skipped_count"] == 1
    assert [item["skipped"] for item in report["per_question"]] == [False, True]


def test_each_skipped_question_counts_as_zero_in_the_overall_score(client):
    """The AI's score covers the answered questions; skips pull it down as zeros (1 answered + 1 skipped = half)."""
    full_id = create(client, ref="stu-full")["interview"]["id"]
    _answer(client, full_id, 0)
    full = _report(client, full_id)

    half_id = create(client, ref="stu-half")["interview"]["id"]
    _answer(client, half_id, 0)
    _post(client, half_id, "skip", json={"question_index": 1})
    half = _report(client, half_id)

    assert half["overall_score"] == round(full["overall_score"] / 2)
    assert half["dimension_scores"]["structure"] == round(full["dimension_scores"]["structure"] / 2)
    assert half["readiness_level"] in ("needs_practice", "not_ready")  # 76 halved is 38: not ready


def test_the_ai_scorer_is_not_shown_skipped_questions_as_answers(client):
    interview_id = create(client)["interview"]["id"]
    _answer(client, interview_id, 0)
    _post(client, interview_id, "skip", json={"question_index": 1})
    _report(client, interview_id)
    scoring = [call for call in client.fake_router.calls if call["task"] == "final_scoring"][-1]
    sent = " ".join(str(getattr(m, "content", m)) for m in scoring["messages"])
    assert "skipped" not in sent.lower()
