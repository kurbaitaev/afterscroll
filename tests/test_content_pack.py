"""Pack integration tests: local fixtures only, every model/transport captured."""
import datetime as dt
import os
import pathlib
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import content  # noqa: E402


@pytest.fixture
def pack(monkeypatch, tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    (root / "PACK.txt").write_text("fixture manifest")
    (root / "pack.py").write_text('''import sys
if sys.argv[1] == "show":
    print("===== CONTENT PACK: " + sys.argv[2] + " =====")
    print("===== VOICE =====\\nfixture voice\\n===== RULES =====\\nfixture rules")
    print("===== LESSONS (from his own results) =====\\nfixture lessons")
''')
    monkeypatch.setattr(content, "PACK_DIR", root)
    monkeypatch.setattr(content, "_PACK_FLAG", tmp_path / "pack.flag")
    monkeypatch.setattr(content, "STATE", tmp_path / "state.json")
    for name in ("ask_openai", "ask_claude", "api", "save_state", "tell", "query"):
        monkeypatch.setattr(content, name, Mock(side_effect=AssertionError(f"unexpected {name}")))
    content._pack_blocks.cache_clear()
    yield root
    content._pack_blocks.cache_clear()


def test_real_pack_loader_caches_check_and_both_jobs(pack, monkeypatch):
    run = Mock(wraps=subprocess.run)
    monkeypatch.setattr(content.subprocess, "run", run)
    first = content.content_pack("script")
    assert "fixture voice" in first and "fixture rules" in first and "fixture lessons" in first
    assert "CONTENT PACK: ideas" in content.content_pack("ideas")
    assert content.content_pack("script") == first
    assert [c.args[0][-1] for c in run.call_args_list] == ["check", "script", "ideas"]


def test_pack_precedes_transcript_in_actual_writer_call(pack, monkeypatch, tmp_path):
    note = tmp_path / "source.md"
    note.write_text("# Original title\n> — Original author\n## Transcript\nExact original transcript.")
    monkeypatch.setattr(content, "vault_note", lambda src: note)
    writer = Mock(return_value={"beats": [["captured fixture only"]]})
    monkeypatch.setattr(content, "ask_writer", writer)
    content.make_draft({"id": "bookmark", "properties": {"Source": {"url": "https://example.test"}}}, True)
    prompt = writer.call_args.args[0]
    assert prompt.startswith("===== CONTENT PACK: script =====")
    assert prompt.index("===== VOICE =====") < prompt.index("ORIGINAL (verbatim):")
    assert "Exact original transcript." in prompt
    content.api.assert_not_called()


@pytest.mark.parametrize("command", ["drafts", "pick"])
@pytest.mark.parametrize("failure", ["missing", "stale", "check", "timeout", "incomplete", "future"])
def test_pack_failure_stops_before_reads_models_or_writes(pack, monkeypatch, command, failure):
    if failure == "missing":
        (pack / "PACK.txt").unlink()
    elif failure in ("stale", "future"):
        stamp = time.time() - content.PACK_MAX_AGE - 1 if failure == "stale" else time.time() + 600
        os.utime(pack / "PACK.txt", (stamp, stamp))
    elif failure == "check":
        (pack / "pack.py").write_text('raise SystemExit("PRIVATE ERROR")')
    elif failure == "incomplete":
        (pack / "pack.py").write_text('print("incomplete")')
    else:
        monkeypatch.setattr(content.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("pack", 15)))
    sent = []
    monkeypatch.setattr(content, "tell", lambda text: sent.append(text) or True)
    for _ in range(2):
        rc = content.cmd_drafts(True, False) if command == "drafts" else content.cmd_pick(False, False, True, False)
        assert rc == 1
    assert sent == [content.PACK_ALERT]
    assert content._PACK_FLAG.read_text() == content.PACK_ALERT
    for name in ("ask_openai", "ask_claude", "api", "save_state", "query"):
        getattr(content, name).assert_not_called()


def test_failed_telegram_does_not_set_pack_alert_flag(pack, monkeypatch):
    (pack / "PACK.txt").unlink()
    send = Mock(return_value=False)
    monkeypatch.setattr(content, "tell", send)
    assert content.cmd_drafts(True, False) == 1
    assert not content._PACK_FLAG.exists()
    assert send.call_args.args == (content.PACK_ALERT,)


def test_recovery_clears_flag_then_new_failure_alerts(pack, monkeypatch):
    sent = []
    monkeypatch.setattr(content, "tell", lambda text: sent.append(text) or True)
    stamp = time.time() - content.PACK_MAX_AGE - 1
    os.utime(pack / "PACK.txt", (stamp, stamp))
    assert content.cmd_drafts(True, False) == 1
    os.utime(pack / "PACK.txt", None)
    content.content_pack("script")
    assert not content._PACK_FLAG.exists()
    content._pack_blocks.cache_clear()
    os.utime(pack / "PACK.txt", (stamp, stamp))
    assert content.cmd_drafts(True, False) == 1
    assert sent == [content.PACK_ALERT, content.PACK_ALERT]


def test_preview_draft_cli_prints_exact_prompt_no_model_or_writes(pack, monkeypatch, tmp_path, capsys):
    note = tmp_path / "source.md"
    note.write_text("# Title\n## Transcript\nThe exact original reel transcript.")
    monkeypatch.setattr(content, "load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["content.py", "drafts", "--show-prompt", "--source-note", str(note)])
    assert content.main() == 0
    preview = capsys.readouterr().out
    assert preview.startswith("===== CONTENT PACK: script =====")
    assert "The exact original reel transcript." in preview
    assert not content._PACK_FLAG.exists()


def test_preview_failure_is_quiet_and_preserves_existing_alert(pack, monkeypatch, tmp_path):
    content._PACK_FLAG.write_text("already alerted")
    (pack / "PACK.txt").unlink()
    monkeypatch.setattr(content, "load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["content.py", "drafts", "--show-prompt", "--source-note", str(tmp_path / "source")])
    assert content.main() == 1
    assert content._PACK_FLAG.read_text() == "already alerted"
    content.tell.assert_not_called()


def pick_fixture(monkeypatch):
    def row(identity, status, views):
        return {"id": identity, "properties": {
            "Idea": {"type": "title", "title": [{"plain_text": identity}]}, "Status": {"status": {"name": status}},
            "Views": {"number": views}, "Source": {"url": "https://example.test/" + identity},
            "Notes": {"type": "rich_text", "rich_text": [{"plain_text": content.BOT_NOTE}]},
        }}
    monkeypatch.setattr(content, "query", lambda *a: [row("winner", "Published", 10000),
                                                    row("loser", "Published", 100), row("waiting", "Inbox", 0)])
    beats = [[" ".join(["source"] * 45)]]
    monkeypatch.setattr(content, "script_beats", lambda _: beats)
    return beats


def test_pick_preview_includes_ideas_and_evidence_bypasses_cadence(pack, monkeypatch, capsys):
    pick_fixture(monkeypatch)
    monkeypatch.setattr(content, "load_state", lambda: {"reel_picks": [{"date": dt.date.today().isoformat(), "id": "prior"}]})
    assert content.cmd_pick(False, False, False, False, show_prompt=True) == 0
    prompt = capsys.readouterr().out
    assert prompt.startswith("===== CONTENT PACK: ideas =====")
    assert "DID BETTER" in prompt and "10,000 | winner" in prompt
    assert "DID NOT" in prompt and "100 | loser" in prompt
    assert "WAITING DRAFTS" in prompt and "waiting | waiting" in prompt
    content.ask_claude.assert_not_called()
    content.save_state.assert_not_called()


def test_pick_models_receive_ideas_then_script_pack(pack, monkeypatch):
    beats = pick_fixture(monkeypatch)
    pick = Mock(return_value={"pick": "waiting", "why": "fixture"})
    writer = Mock(return_value={"beats": beats})
    monkeypatch.setattr(content, "ask_claude", pick)
    monkeypatch.setattr(content, "ask_writer", writer)
    assert content.cmd_pick(True, False, True, False) == 0
    assert pick.call_args.args[0].startswith("===== CONTENT PACK: ideas =====")
    assert writer.call_args.args[0].startswith("===== CONTENT PACK: script =====")
    assert "DRAFT:" in writer.call_args.args[0]
    content.api.assert_not_called()
