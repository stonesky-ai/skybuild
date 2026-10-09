import pytest

from skybuild.client import ClientError
from skybuild.scheduler import schedule_due


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def reconcile_due_deferrals(self, project_id, **kwargs):
        self.calls.append((project_id, kwargs))
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page


def page(cursor=None, changed=None, count=0):
    return {"scanned": count, "reassessed": changed or [], "next_after_task_id": cursor}


def test_timer_catches_up_immediately_continues_large_sweep_and_restarts_completed_sweep():
    client = Pages([page("task-a", ["task-a"], 1), page(None, ["task-b"], 1), page()])
    events, delays = [], []
    result = schedule_due(client, "project", max_ticks=3, max_pages=1, page_size=1,
                          report=events.append, sleep=delays.append)
    assert [call[1]["after_task_id"] for call in client.calls] == [None, "task-a", None]
    assert delays == [60, 60]
    assert [event["complete"] for event in events] == [False, True, True]
    assert result == events[-1]


def test_failure_retains_last_confirmed_cursor_and_stops_without_sleep():
    client = Pages([page("task-a", ["task-a"], 1), ClientError("unavailable", "secret")])
    events, delays = [], []
    result = schedule_due(client, "project", max_ticks=3, page_size=1,
                          report=events.append, sleep=delays.append)
    assert result == {"tick": 1, "scanned": 1, "reassessed": ["task-a"],
                      "complete": False, "next_after_task_id": "task-a", "uncertain_page": True}
    assert not delays
    assert events == [result]


@pytest.mark.parametrize("invalid", [page("task-a", count=0), page(count=True),
                                       page(changed=["bad/id"], count=1), {}, None])
def test_malformed_page_does_not_advance_cursor(invalid):
    client = Pages([invalid])
    result = schedule_due(client, "project", sleep=lambda _: pytest.fail("must stop"))
    assert result["uncertain_page"]
    assert result["next_after_task_id"] is None


def test_repeated_cursor_stops_instead_of_looping():
    client = Pages([page("task-a", count=1), page("task-a", count=1)])
    result = schedule_due(client, "project", page_size=1)
    assert result["scanned"] == 1
    assert result["uncertain_page"]


@pytest.mark.parametrize("bounds", [{"max_ticks": 0}, {"max_ticks": True}, {"interval_seconds": 0},
                                    {"interval_seconds": 86401}, {"max_pages": 101}, {"page_size": 101}])
def test_invalid_bounds_do_not_call_api(bounds):
    client = Pages([])
    with pytest.raises(ValueError):
        schedule_due(client, "project", **bounds)
    assert not client.calls


def test_cli_schedule_is_finite_and_reports_ticks(monkeypatch, capsys):
    from skybuild.__main__ import main
    from skybuild.client import Client

    monkeypatch.setenv("SKYBUILD_API_URL", "http://test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "token")
    monkeypatch.setattr(Client, "reconcile_due_deferrals", lambda *args, **kwargs: page())
    assert main(["schedule-due", "project", "--max-ticks", "1"]) == 0
    assert "tick" in capsys.readouterr().out
