"""Recorded customer-view probes send one real request through the bot; no statistic may count it."""
import inspect

import pytest
from pydantic import ValidationError

from console import app as console_app
from regress_mcp import detector, localize, server, sources
from target.bot.app import FeedbackRequest, ReplyRequest

PROBE = "source <> 'probe'"


def test_bot_accepts_probe_source():
    assert ReplyRequest(question="hi", source="probe").source == "probe"
    assert FeedbackRequest(trace_id="t", kind="thumbs_up", source="probe").source == "probe"
    with pytest.raises(ValidationError):
        ReplyRequest(question="hi", source="robot")


def test_detector_excludes_probes():
    assert "r.source <> 'probe'" in detector.DETECTOR_SQL


def test_window_stats_and_traces_exclude_probes():
    assert PROBE in sources.WINDOW_STATS_SQL
    assert PROBE in inspect.getsource(sources.traces)


def test_localisation_shares_and_segments_exclude_probes():
    assert PROBE in inspect.getsource(localize._segments)
    assert PROBE in inspect.getsource(localize._shares)


def test_console_series_and_server_counts_exclude_probes():
    assert PROBE in console_app.SERIES_SQL
    assert PROBE in inspect.getsource(server._draining)
    assert PROBE in inspect.getsource(server.check_gates)


def test_every_requests_query_is_covered():
    """A new statistic over `requests` must decide about probes explicitly."""
    for module in (detector, sources, localize, server, console_app):
        src = inspect.getsource(module)
        reads = src.count("from requests")
        assert reads <= src.count("probe") + _allowed(module), module.__name__


def _allowed(module) -> int:
    # server.run_replay looks rows up by trace_id with golden_id not null, which never matches a probe.
    return 1 if module is server else 0
