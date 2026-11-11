"""CONTENT-STATS-BY-LINK: captured Notion/provider/Telegram regression checks."""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import urllib.error
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import content  # noqa: E402
import watchdog  # noqa: E402

NOW = dt.datetime(2026, 11, 11, 12, 0, 31, tzinfo=dt.timezone.utc)


def row(rid="linked", url="https://www.instagram.com/reel/EXACT/", age=25, views=10):
    return {"id": rid, "properties": {
        "Posted": {"url": url}, "Source": {"url": "https://instagram.com/reel/UNMATCHED/"},
        "Idea": {"type": "title", "title": [{"plain_text": "same caption"}]},
        "Posted on": {"date": {"start": (NOW - dt.timedelta(hours=age)).isoformat()}},
        "Views": {"number": views}, "Likes": {"number": 2},
        "Views 24h": {"number": None}, "Views 72h": {"number": None},
        "Status": {"status": {"name": "Needs you"}},
    }}


def reel(code="EXACT", views=20):
    return {"shortCode": code, "videoPlayCount": views, "likesCount": 3,
            "commentsCount": 1, "caption": "same caption", "timestamp": "2026-11-10T11:00:00Z"}


@pytest.fixture
def captured(monkeypatch, tmp_path):
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(content.dt, "datetime", Clock)
    monkeypatch.setattr(content, "STATE", tmp_path / "content_state.json")
    data, writes, sent = {}, [], []
    monkeypatch.setattr(content, "query", lambda *a, **k: copy.deepcopy(list(data.values())))
    monkeypatch.setattr(content, "ensure_columns", lambda *a: None)
    monkeypatch.setattr(content, "scrape", lambda *a: [reel()])
    monkeypatch.setattr(content, "tell", lambda text: sent.append(text) or True)
    monkeypatch.setattr(content, "ask_claude", Mock(side_effect=AssertionError("stats called a model")))

    def api(path, method="GET", body=None):
        assert path.startswith("pages/") and path.split("/")[1] in data
        rid = path.split("/")[1]
        if method == "PATCH":
            writes.append((rid, copy.deepcopy(body["properties"])))
            data[rid]["properties"].update(copy.deepcopy(body["properties"]))
        else:
            assert method == "GET"
        return copy.deepcopy(data[rid])
    monkeypatch.setattr(content, "api", api)
    return data, writes, sent


def test_stats_only_posted_shortcode_no_guess_no_create_or_link_overwrite(captured, monkeypatch, capsys):
    data, writes, _ = captured
    for r in [row(), row("no-link", None), row("substring", "https://instagram.com/reel/EXACTmore/"),
              row("foreign", "https://x.com/a/status/123"), row("spoof", "https://instagram.com.evil/reel/EXACT/")]:
        data[r["id"]] = r
    content.save_state({"assign": {"old-caption": "no-link"}})
    monkeypatch.setattr(content, "scrape", lambda *a: [reel(), reel("UNMATCHED"), reel("EXACT", 15)])
    assert content.cmd_stats(False, False, 60) == 0
    assert [rid for rid, _ in writes] == ["linked"]
    assert len(data) == 5
    assert data["linked"]["properties"]["Views"]["number"] == 20
    assert all(not {"Posted", "Status", "Platform", "Channel"} & props.keys() for _, props in writes)
    assert "1 rows updated; 1 unmatched; 0 rows created" in capsys.readouterr().out


@pytest.mark.parametrize("age,expected", [(23.99, []), (24, ["Views 24h"]), (36, ["Views 24h"]),
    (36.01, []), (71.99, []), (72, ["Views 72h"]), (84, ["Views 72h"]), (84.01, [])])
def test_snapshot_first_window_only(captured, age, expected):
    data, writes, _ = captured
    data["linked"] = row(age=age)
    content.cmd_stats(True, False, 60)
    assert [k for k in writes[0][1] if k in ("Views 24h", "Views 72h")] == expected
    original_date = data["linked"]["properties"]["Posted on"]
    content.cmd_stats(True, False, 60)
    assert not {"Views 24h", "Views 72h", "Posted on"} & writes[-1][1].keys()
    assert data["linked"]["properties"]["Posted on"] == original_date


def test_existing_zero_snapshot_and_post_date_preserved(captured):
    data, writes, _ = captured
    data["linked"] = row()
    data["linked"]["properties"]["Views 24h"] = {"number": 0}
    content.cmd_stats(True, False, 60)
    assert "Views 24h" not in writes[0][1] and "Posted on" not in writes[0][1]


def test_missing_post_date_filled_from_exact_reel_timestamp(captured):
    data, writes, _ = captured
    data["linked"] = row()
    data["linked"]["properties"]["Posted on"] = {"date": None}
    content.cmd_stats(True, False, 60)
    assert writes[0][1]["Posted on"]["date"]["start"] == "2026-11-10T11:00+00:00"
    assert writes[0][1]["Views 24h"] == {"number": 20}


def test_notion_minute_precision_is_written_before_exact_readback(captured):
    data, writes, _ = captured
    data["linked"] = row()
    content.cmd_stats(True, False, 60)
    assert writes[0][1]["Stats updated"]["date"]["start"] == "2026-11-11T12:00+00:00"


@pytest.mark.parametrize("value", [0, 5, None, -1, "20", float("nan")])
def test_partial_or_invalid_reel_never_lowers_views_or_fills_snapshots(captured, monkeypatch, value):
    data, writes, _ = captured
    data["linked"] = row()
    monkeypatch.setattr(content, "scrape", lambda *a: [reel(views=value), {"error": "partial", "shortCode": "OTHER"}])
    content.cmd_stats(True, False, 60)
    assert writes == []
    assert data["linked"]["properties"]["Views"] == {"number": 10}


def test_failed_scrape_writes_nothing(captured, monkeypatch):
    data, writes, _ = captured
    data["linked"] = row()
    monkeypatch.setattr(content, "scrape", Mock(side_effect=RuntimeError("provider unavailable")))
    with pytest.raises(RuntimeError):
        content.cmd_stats(True, False, 60)
    assert writes == [] and not content.STATE.exists()


def test_snapshot_due_bypasses_old_cadence_and_link_rechecked(captured, monkeypatch):
    data, writes, _ = captured
    data["linked"] = row()
    content.save_state({"stats_at": NOW.isoformat(), "stats_policy": content.STATS_POLICY})
    content.cmd_stats(False, False, 60)
    assert len(writes) == 1
    content.cmd_stats(False, False, 60)
    assert len(writes) == 1
    stale = row()
    monkeypatch.setattr(content, "query", lambda *a: [stale])
    data["linked"]["properties"]["Posted"] = {"url": None}
    content.cmd_stats(True, False, 60)
    assert len(writes) == 1


def test_writer_failure_alerts_once_never_falls_back_or_writes_draft(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "content_pack", lambda *a, **kw: "validated pack")
    flag = tmp_path / "writer.flag"
    monkeypatch.setattr(content, "_WRITER_FLAG", flag)
    monkeypatch.setattr(content, "ask_openai", Mock(side_effect=RuntimeError("private response")))
    monkeypatch.setattr(content, "ask_claude", Mock(side_effect=AssertionError("fallback")))
    sent = []
    monkeypatch.setattr(content, "tell", lambda text: sent.append(text) or True)
    monkeypatch.setattr(content, "vault_note", lambda src: tmp_path / "source")
    monkeypatch.setattr(content, "note_source", lambda p: ("title", "author", "source"))
    monkeypatch.setattr(content, "api", Mock(side_effect=AssertionError("draft write")))
    for _ in range(2):
        with pytest.raises(content.WriterUnavailable):
            content.make_draft({"id": "bookmark", "properties": {"Source": {"url": "https://example.org"}}}, False)
    assert len(sent) == 1 and "private response" not in sent[0]
    monkeypatch.setattr(content, "ask_openai", lambda p: {"beats": [["hello"]]})
    content.ask_writer("retry after recovery")
    assert not flag.exists()


def test_writer_failed_alert_is_not_marked_sent(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "_WRITER_FLAG", tmp_path / "writer.flag")
    monkeypatch.setattr(content, "ask_openai", Mock(side_effect=TimeoutError()))
    monkeypatch.setattr(content, "tell", lambda text: False)
    with pytest.raises(content.WriterUnavailable):
        content.ask_writer("draft")
    assert not content._WRITER_FLAG.exists()


def test_writer_transient_retry_and_permanent_error_bound(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_OPENAI_API_KEY", "fake-test-only")
    monkeypatch.setattr(content, "OPENAI_USAGE", tmp_path / "usage")
    monkeypatch.setattr(content, "OPENAI_USD_DAILY", 1)
    monkeypatch.setattr(content.time, "sleep", lambda _: None)
    body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"beats": [["hello"]]}' }]}]}
    calls = Mock(side_effect=[urllib.error.HTTPError("https://example.test", 429, "rate", {}, io.BytesIO(b"private")),
                             io.BytesIO(json.dumps(body).encode())])
    monkeypatch.setattr(content.urllib.request, "urlopen", calls)
    assert content.ask_openai("draft")["beats"] == [["hello"]]
    assert calls.call_count == 2
    calls = Mock(side_effect=urllib.error.HTTPError("https://example.test", 401, "auth", {}, io.BytesIO(b"private")))
    monkeypatch.setattr(content.urllib.request, "urlopen", calls)
    with pytest.raises(RuntimeError, match="OpenAI HTTP 401"):
        content.ask_openai("draft")
    assert calls.call_count == 1


def test_draft_uses_existing_channel_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "content_pack", lambda *a, **kw: "validated pack")
    monkeypatch.setattr(content, "vault_note", lambda src: tmp_path / "source")
    monkeypatch.setattr(content, "note_source", lambda p: ("title", "author", "source"))
    monkeypatch.setattr(content, "ask_writer", lambda p: {"title": "title", "beats": [["line"]]})
    calls = []
    monkeypatch.setattr(content, "api", lambda *args: calls.append(args) or {"url": "https://example.test"})
    content.make_draft({"id": "bookmark", "properties": {"Source": {"url": "https://example.org"}}}, False)
    props = calls[0][2]["properties"]
    assert props["Channel"] == {"multi_select": [{"name": "Instagram RU"}]}
    assert "Platform" not in props


@pytest.fixture
def heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "MINI_HEARTBEAT", tmp_path / "mini-heartbeat")
    monkeypatch.setattr(watchdog.time, "time", lambda: 1800000000)
    sent = []
    monkeypatch.setattr(watchdog, "alert", lambda t, c, msg: sent.append(msg) or True)
    return tmp_path, sent


def test_mini_missing_quiet_stale_once_then_back_once(heartbeat):
    folder, sent = heartbeat
    watchdog.check_mini_heartbeat("token", "chat")
    assert not sent
    watchdog.MINI_HEARTBEAT.touch()
    os.utime(watchdog.MINI_HEARTBEAT, (1799992800, 1799992800))
    watchdog.check_mini_heartbeat("token", "chat")
    watchdog.check_mini_heartbeat("token", "chat")
    assert len(sent) == 1 and sent[0].startswith("Mac mini silent since ") and sent[0].endswith(" PT")
    os.utime(watchdog.MINI_HEARTBEAT, (1800000000, 1800000000))
    watchdog.check_mini_heartbeat("token", "chat")
    watchdog.check_mini_heartbeat("token", "chat")
    assert sent[-1] == "Mac mini is back." and len(sent) == 2


def test_mini_failed_send_or_recovery_preserves_retry_and_dry_run_is_separate(heartbeat, monkeypatch, capsys):
    folder, sent = heartbeat
    watchdog.MINI_HEARTBEAT.touch()
    os.utime(watchdog.MINI_HEARTBEAT, (1799992800, 1799992800))
    monkeypatch.setattr(watchdog, "alert", lambda *args: False)
    watchdog.check_mini_heartbeat("token", "chat")
    assert not (folder / ".mini-silent").exists()
    watchdog.check_mini_heartbeat("token", "chat", dry_run=True)
    watchdog.check_mini_heartbeat("token", "chat", dry_run=True)
    assert len(capsys.readouterr().out.splitlines()) == 1
    assert not (folder / ".mini-silent").exists()
    (folder / ".mini-silent").write_text("delivered stale")
    os.utime(watchdog.MINI_HEARTBEAT, (1800000000, 1800000000))
    watchdog.check_mini_heartbeat("token", "chat")
    assert (folder / ".mini-silent").exists()


def test_watchdog_auth_and_save_recovery_flags_require_send_ack(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "_AUTH_FLAG", tmp_path / "auth")
    monkeypatch.setattr(watchdog, "_AUTH_FAILED", tmp_path / "failed")
    monkeypatch.setattr(watchdog, "_SAVES_FLAG", tmp_path / "saves")
    monkeypatch.setattr(watchdog, "WLOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(watchdog, "claude_token_expired", lambda: True)
    monkeypatch.setattr(watchdog, "alert", lambda *args: False)
    watchdog.check_claude_auth("t", "c")
    assert not watchdog._AUTH_FLAG.exists()
    watchdog._AUTH_FLAG.write_text("sent")
    watchdog._SAVES_FLAG.write_text("sent")
    monkeypatch.setattr(watchdog, "claude_token_expired", lambda: False)
    monkeypatch.setattr(watchdog, "saves_failing", lambda: False)
    watchdog.check_claude_auth("t", "c")
    watchdog.check_saves("t", "c")
    assert watchdog._AUTH_FLAG.exists() and watchdog._SAVES_FLAG.exists()
