#!/usr/bin/env python3
"""The remake pipeline, between a ticked bookmark and the views it earned.

    python3 content.py drafts              # new "Worth remaking" ticks → Russian draft rows
    python3 content.py drafts --backfill   # also draft every ticked bookmark still without a row
    python3 content.py stats               # posted reels → views on their Content rows
    python3 content.py stats --force       # run even if the last pass was under 3 days ago
    python3 content.py pick                # this week's reel to film: one waiting draft, tightened
    python3 content.py pick --show         # the latest pick again, nothing new done
    python3 content.py drafts --show-prompt --source-note vault/example.md
    python3 content.py pick --show-prompt  # read-only prompt preview, no model or writes

drafts (timer, every 15 min): a bookmark ticked "Worth remaking" that has no row
in 💡 Content Ideas gets one — status Inbox, a Russian script close to the
original, laid out like the scripts already there (big lines, read while filming).

stats (existing timer): only an Instagram shortcode in a row's Posted link
selects that row. Unmatched reels never create rows. Views never decrease, and
24h/72h snapshots are written once within twelve hours of their target age.
No caption matching or model calls are used by stats.

pick (run weekly from his laptop, Sundays): of the bot drafts still in Inbox, the one
most like the remakes that beat his usual reel. It gets a close edit for speaking
aloud, appended under the original on its row, and Priority High. One pick a week;
a draft is never picked twice. Nothing is written when a model call fails.
"""

import argparse
import datetime as dt
import fcntl
import functools
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROJ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))

import acquire  # noqa: E402
import notion  # noqa: E402

log = logging.getLogger("saved-to-notes.content")

NOTION = "https://api.notion.com/v1"
CONTENT_DB = os.environ.get("CONTENT_DATABASE_ID", "821c50bb18f14c7ca1ae9f350376a77b")
STATE = PROJ / "logs" / "content_state.json"
REEL_ACTOR = "apify~instagram-reel-scraper"
STATS_EVERY = dt.timedelta(days=3)
STATS_POLICY = "posted-link-v1"

# Columns the stats pass owns. Created on first run; never touched otherwise.
CONTENT_COLUMNS = {
    "Posted": {"url": {}},
    "Posted on": {"date": {}},
    "Views": {"number": {"format": "number_with_commas"}},
    "Views 24h": {"number": {"format": "number_with_commas"}},
    "Views 72h": {"number": {"format": "number_with_commas"}},
    "Likes": {"number": {"format": "number_with_commas"}},
    "Comments": {"number": {"format": "number_with_commas"}},
    "Variants": {"number": {}},
    "Stats updated": {"date": {}},
}


# --- plumbing -------------------------------------------------------------------

def load_env() -> None:
    f = PROJ / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _tok() -> str:
    return os.environ["NOTION_TOKEN"]


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    return notion._api(f"{NOTION}/{path}", _tok(), method, body)


def query(db: str, flt: dict | None = None) -> list[dict]:
    out, cursor = [], None
    while True:
        body = {"page_size": 100, **({"filter": flt} if flt else {}),
                **({"start_cursor": cursor} if cursor else {})}
        d = api(f"databases/{db}/query", "POST", body)
        out += d["results"]
        if not d.get("has_more"):
            return out
        cursor = d["next_cursor"]


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    tmp.replace(STATE)


def ensure_columns(db: str, wanted: dict) -> None:
    have = api(f"databases/{db}")["properties"]
    missing = {k: v for k, v in wanted.items() if k not in have}
    if missing:
        api(f"databases/{db}", "PATCH", {"properties": missing})
        log.info("added columns to %s: %s", db, ", ".join(missing))


def text_of(prop: dict) -> str:
    t = prop.get("type")
    return "".join(x.get("plain_text", "") for x in prop.get(t) or []) if t in (
        "title", "rich_text") else ""


def status_of(row: dict) -> str | None:
    return ((row["properties"].get("Status") or {}).get("status") or {}).get("name")


def rt(s: str) -> list:
    # Notion caps one text run at 2000 chars.
    return [{"type": "text", "text": {"content": s[i:i + 2000]}} for i in range(0, len(s), 2000)] or []


def page_text(page_id: str, limit: int = 1500) -> str:
    out = []
    for b in api(f"blocks/{page_id}/children?page_size=100").get("results", []):
        body = b.get(b["type"]) or {}
        out.append("".join(x.get("plain_text", "") for x in body.get("rich_text") or []))
        if sum(map(len, out)) > limit:
            break
    return "\n".join(x for x in out if x).strip()[:limit]


def claude_bin() -> str:
    """Same lookup as the bot: a user unit's PATH omits ~/.local/bin."""
    import shutil
    for cand in (os.environ.get("CLAUDE_BIN", "").strip(), shutil.which("claude"),
                 str(pathlib.Path.home() / ".local/bin/claude")):
        if cand and pathlib.Path(cand).exists():
            return cand
    return "claude"


def ask_claude(prompt: str, timeout: int = 600) -> dict:
    """Run the headless Claude CLI and parse the JSON object it answers with."""
    args = [claude_bin(), "-p", prompt, "--max-turns", "3"]
    model = os.environ.get("CLAUDE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    r = subprocess.run(args, cwd=PROJ, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude failed ({r.returncode}): {(r.stderr or r.stdout)[-500:]}")
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise RuntimeError(f"claude gave no JSON: {r.stdout[-300:]}")
    return json.loads(m.group(0))


# The Russian remake drafts run on his OpenAI credits since 2026-11-08 (he wants all copywriting there).
# A separate key name on purpose: OPENAI_API_KEY would also switch the bot's save agent (agent_openai.enabled()).
# Writer failures stop the draft; no other model is substituted.
OPENAI_DRAFT_MODEL = os.environ.get("CONTENT_OPENAI_MODEL", "gpt-5.6-sol")
OPENAI_USD_DAILY = float(os.environ.get("CONTENT_OPENAI_USD_DAILY", "2"))
# $ per 1M tokens: input, cached input, output (developers.openai.com/api/docs/pricing, checked 2026-11-08; 5.6 Sol promo until Nov 30)
OPENAI_PRICES = {"gpt-5.6-sol": (4, 0.4, 20), "gpt-6.1-sol": (2, 0.1, 10), "gpt-6.1-terra": (1, 0.05, 5)}
OPENAI_USAGE = PROJ / "logs" / "openai_usage.tsv"


def openai_spent_today() -> float:
    since, total = time.time() - 86400, 0.0
    try:
        for line in OPENAI_USAGE.read_text().splitlines():
            ts, _, usd = (line.split("\t") + ["", "", ""])[:3]
            if ts and float(ts) >= since:
                total += float(usd or 0)
    except (OSError, ValueError):
        pass
    return total


def ask_openai(prompt: str, timeout: int = 300) -> dict:
    """One JSON answer from the OpenAI Responses API (the prompt itself says JSON, which JSON mode requires)."""
    key = os.environ.get("CONTENT_OPENAI_API_KEY", "").strip()
    if not key or not OPENAI_DRAFT_MODEL:
        raise RuntimeError("no CONTENT_OPENAI_API_KEY")
    if openai_spent_today() >= OPENAI_USD_DAILY:
        raise RuntimeError(f"OpenAI daily cap ${OPENAI_USD_DAILY} reached")
    body = {"model": OPENAI_DRAFT_MODEL, "input": prompt, "max_output_tokens": 16000,
            "reasoning": {"effort": "medium"}, "text": {"format": {"type": "json_object"}}}
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"OpenAI HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < 1:
                time.sleep(5)
                continue
            raise RuntimeError("OpenAI connection timeout/unavailable") from None
    u = d.get("usage", {})
    pin, pcache, pout = OPENAI_PRICES.get(OPENAI_DRAFT_MODEL, (10, 1, 50))
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
    usd = ((u.get("input_tokens", 0) - cached) * pin + cached * pcache + u.get("output_tokens", 0) * pout) / 1e6
    OPENAI_USAGE.parent.mkdir(exist_ok=True)
    with OPENAI_USAGE.open("a") as f:
        f.write(f"{time.time():.0f}\t{OPENAI_DRAFT_MODEL}\t{usd:.5f}\n")
    text = "".join(c.get("text", "") for o in d.get("output", []) if o.get("type") == "message"
                   for c in o.get("content", []) if c.get("type") == "output_text").strip()
    if not text:
        raise RuntimeError(f"openai empty answer (status {d.get('status')}, {d.get('incomplete_details')})")
    return json.loads(text)


class WriterUnavailable(RuntimeError):
    """No draft or pick may be committed after the required writer fails."""


_WRITER_FLAG = PROJ / "logs" / ".writer_alerted"
PACK_DIR = pathlib.Path("/home/magomed/content-pack")
PACK_MAX_AGE = 72 * 60 * 60
PACK_ALERT = "Draft stopped: content pack missing or stale."
_PACK_FLAG = PROJ / "logs" / ".content_pack_alerted"


class PackUnavailable(WriterUnavailable):
    """A draft/pick cannot proceed without the current shared content pack."""


@functools.lru_cache(maxsize=1)
def _pack_blocks() -> dict[str, str]:
    """Check once and cache both jobs for this process, before any model/write."""
    try:
        stamp = (PACK_DIR / "PACK.txt").stat().st_mtime
        age = time.time() - stamp
        if age > PACK_MAX_AGE or age < -300:
            raise ValueError("pack age")
        command = [sys.executable, str(PACK_DIR / "pack.py")]
        subprocess.run(command + ["check"], check=True, capture_output=True, text=True, timeout=15)
        blocks = {}
        for job in ("script", "ideas"):
            text = subprocess.run(command + ["show", job], check=True, capture_output=True,
                                  text=True, timeout=15).stdout
            if not all(s in text for s in (f"===== CONTENT PACK: {job} =====",
                                           "===== VOICE =====", "===== RULES =====", "===== LESSONS")):
                raise ValueError("incomplete pack")
            blocks[job] = text
        if (PACK_DIR / "PACK.txt").stat().st_mtime != stamp:
            raise ValueError("pack changed while loading")
        return blocks
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never forward command output: it can contain private source material.
        raise PackUnavailable(PACK_ALERT) from None


def content_pack(job: str, *, alert: bool = True) -> str:
    try:
        block = _pack_blocks()[job]
    except PackUnavailable:
        if alert and not _PACK_FLAG.exists() and tell(PACK_ALERT):
            _PACK_FLAG.parent.mkdir(exist_ok=True)
            _PACK_FLAG.write_text(PACK_ALERT)
        raise
    if alert:
        _PACK_FLAG.unlink(missing_ok=True)
    return block


def draft_prompt(note: pathlib.Path, author: str = "", *, alert: bool = True) -> str:
    pack = content_pack("script", alert=alert)
    title, source_author, verbatim = note_source(note)
    return pack + "\n" + DRAFT_PROMPT.format(author=source_author or author or "?", title=title, source=verbatim)


def ask_writer(prompt: str) -> dict:
    """Use only the approved OpenAI writer; alert once per failure episode."""
    try:
        result = ask_openai(prompt)
        if not isinstance(result, dict) or not result.get("beats"):
            raise RuntimeError("OpenAI response missing draft beats")
    except Exception as exc:
        # Provider bodies/prompts can contain private text. Only fixed reasons
        # or an exception class reach the existing Telegram alert chat.
        detail = str(exc)
        safe = (detail if re.fullmatch(r"OpenAI HTTP \d{3}", detail) else
                "OpenAI daily budget reached" if detail.startswith("OpenAI daily cap") else
                "OpenAI key missing" if detail == "no CONTENT_OPENAI_API_KEY" else
                detail if detail in ("OpenAI connection timeout/unavailable", "OpenAI response missing draft beats") else
                "OpenAI " + type(exc).__name__)
        if not _WRITER_FLAG.exists() and tell(f"Draft stopped: {safe}. No draft written."):
            _WRITER_FLAG.parent.mkdir(exist_ok=True)
            _WRITER_FLAG.write_text(safe)
        raise WriterUnavailable(safe) from None
    _WRITER_FLAG.unlink(missing_ok=True)
    return result


def tell(text: str) -> bool:
    import digest
    try:
        digest.send(text)
        return True
    except (Exception, SystemExit) as exc:
        log.warning("telegram failed: %s", type(exc).__name__)
        return False


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def nid(s: str) -> str:
    """Bare 32-hex Notion id out of an id or URL ('' when there is none)."""
    m = re.search(r"([0-9a-f]{32})", (s or "").replace("-", ""))
    return m.group(1) if m else ""


# --- drafts ----------------------------------------------------------------------

DRAFT_PROMPT = """You are adapting someone else's Instagram reel into a Russian script that \
Magomed will film himself (talking head). His audience speaks Russian.

Task: cover the whole transcript, preserving its order, examples, argument and ending.
Return spoken text only, without stage directions. Break it into beats: each beat is
1–4 short lines read in one breath. Follow the shared content pack above for voice and rules.

Answer with ONE JSON object and nothing else:
{{"title": "<2–5 word Russian title>", "hook": "<the opening line, as in the script>", \
"beats": [["line", "line"], ["line"], ...]}}

Original author: {author}
Original title: {title}

ORIGINAL (verbatim):
{source}
"""


def vault_note(url: str) -> pathlib.Path | None:
    key = acquire.normalize_url(url)
    code = acquire._shortcode(key)
    for p in (PROJ / "vault").rglob("*.md"):
        head = p.read_text(errors="replace")[:600]
        m = re.search(r"^source:\s*(\S+)", head, re.M)
        if m and (acquire.normalize_url(m.group(1)) == key or (code and code in m.group(1))):
            return p
    return None


def note_source(p: pathlib.Path) -> tuple[str, str, str]:
    """(title, author, verbatim text) — the transcript, else slides, else the whole note."""
    t = p.read_text(errors="replace")
    title = (re.search(r"^# (.+)$", t, re.M) or [None, p.stem])[1]
    author = (re.search(r"^> — (.+)$", t, re.M) or [None, ""])[1]
    for h in ("## Transcript", "## Slides", "## Caption", "## Post"):
        i = t.find(h)
        if i >= 0:
            return title, author, t[i + len(h):].strip()[:12000]
    return title, author, t.split("---", 2)[-1].strip()[:12000]


def covered_keys(rows: list[dict]) -> set[str]:
    """Every source a Content row already points at — reel links and bookmark page ids."""
    keys = set()
    for r in rows:
        src = (r["properties"].get("Source") or {}).get("url") or ""
        if src:
            keys.add(acquire.normalize_url(src))
            if "notion" in src and nid(src):
                keys.add(nid(src))
    return keys


def beat_blocks(beats: list[list[str]]) -> list[dict]:
    out = []
    for beat in beats:
        lines = [str(x).strip() for x in beat if str(x).strip()]
        if not lines:
            continue
        out.append({"type": "heading_2", "heading_2": {"rich_text": rt("\n".join(lines))}})
        out.append({"type": "paragraph", "paragraph": {"rich_text": []}})
    return out


def make_draft(bm: dict, dry: bool) -> str | None:
    p = bm["properties"]
    src = (p.get("Source") or {}).get("url") or ""
    note = vault_note(src) if src else None
    if not note:
        log.warning("no vault note for %s — skipped", src or bm["id"])
        return None
    title, author, verbatim = note_source(note)
    author = author or text_of(p.get("Author") or {})
    d = ask_writer(draft_prompt(note, author))
    beats = d.get("beats") or []
    if not beats:
        raise RuntimeError("draft came back without beats")
    name = f"{(d.get('title') or title).strip()} · сценарий для съёмки"
    if dry:
        print(name, "\n", "\n\n".join("\n".join(b) for b in beats))
        return None
    bm_url = f"https://www.notion.so/{bm['id'].replace('-', '')}"
    notes = [
        {"type": "paragraph", "paragraph": {"rich_text": rt(
            "Черновик бота по транскрипту оригинала. Проверь текст перед съёмкой.")}},
        {"type": "paragraph", "paragraph": {"rich_text": rt(f"Оригинал: {author} — {title}")}},
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "Исходный ролик", "link": {"url": src}}}]}},
        {"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "Закладка", "link": {"url": bm_url}}}]}},
    ]
    children = beat_blocks(beats) + [
        {"type": "divider", "divider": {}},
        {"type": "toggle", "toggle": {"rich_text": rt("Источник и заметки"), "children": notes}},
    ]
    page = api("pages", "POST", {
        "parent": {"database_id": CONTENT_DB},
        "icon": {"type": "emoji", "emoji": "🎙️"},
        "properties": {
            "Idea": {"title": rt(name)},
            "Status": {"status": {"name": "Inbox"}},
            "Content type": {"select": {"name": "Video"}},
            "Channel": {"multi_select": [{"name": "Instagram RU"}]},
            "Archive status": {"select": {"name": "Active"}},
            "Source": {"url": src},
            "Author": {"rich_text": rt(author)},
            "Hook / key idea": {"rich_text": rt(d.get("hook") or beats[0][0])},
            "Notes": {"rich_text": rt("Черновик бота — проверь перед съёмкой.")},
        },
        "children": children[:100],
    })
    return page.get("url")


def cmd_drafts(backfill: bool, dry: bool) -> int:
    try:
        content_pack("script")
    except PackUnavailable as exc:
        log.error("%s", exc)
        return 1
    state = load_state()
    ticked = query(os.environ["NOTION_DATABASE_ID"],
                   {"property": "Worth remaking", "checkbox": {"equals": True}})
    covered = covered_keys(query(CONTENT_DB))
    seen = set(state.get("seen_ticks", []))
    first_run = "seen_ticks" not in state
    todo = []
    for bm in ticked:
        src = (bm["properties"].get("Source") or {}).get("url") or ""
        done = acquire.normalize_url(src) in covered or nid(bm["id"]) in covered
        if done:
            seen.add(bm["id"])
        elif bm["id"] not in seen and (backfill or not first_run):
            todo.append(bm)
        elif first_run:
            seen.add(bm["id"])  # the backlog waits for --backfill; only new ticks draft by themselves
    made = 0
    for bm in todo:
        name = text_of(bm["properties"].get("Name") or {})
        try:
            url = make_draft(bm, dry)
        except WriterUnavailable as exc:
            log.error("draft stopped: %s", exc)
            return 1
        except Exception as e:  # noqa: BLE001
            log.error("draft failed for %s: %s", name, e)
            continue  # not marked seen: the next run tries again
        if dry:
            continue
        seen.add(bm["id"])
        state["seen_ticks"] = sorted(seen)
        save_state(state)
        if url:
            made += 1
            tell(f"🎙️ Черновик готов: <b>{esc(name)}</b>\n{url}")
    if not dry:
        state["seen_ticks"] = sorted(seen)
        save_state(state)
    print(f"{len(ticked)} ticked, {len(todo)} to draft, {made} drafted")
    return 0


# --- stats ------------------------------------------------------------------------

def instagram_code(url: str) -> str | None:
    """Exact public Instagram post path; no substring/Source/caption matching."""
    try:
        parsed = urllib.parse.urlsplit(url or "")
        if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password
                or parsed.hostname not in ("instagram.com", "www.instagram.com", "m.instagram.com")
                or parsed.port not in (None, 80, 443)):
            return None
    except ValueError:
        return None
    match = re.fullmatch(r"/(?:reels?|p)/([A-Za-z0-9_-]+)/?", parsed.path)
    return match[1] if match else None


def scrape(handles: list[str], days: int) -> list[dict]:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("APIFY_TOKEN missing")
    items = acquire._apify_run(REEL_ACTOR, {
        "username": handles, "resultsLimit": 60, "onlyPostsNewerThan": f"{days} days",
        "skipTrialReels": False}, token)
    reels = [i for i in items if i.get("shortCode") and not i.get("error")]
    if not reels:
        raise RuntimeError(f"scrape returned no reels ({len(items)} item(s))")
    return reels


def metric(value) -> int | None:
    # Missing, hidden, negative or malformed metrics are unknown, never zero.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if value >= 0 and value < float("inf") and value == int(value) else None


def reel_views(reel: dict) -> int | None:
    value = metric(reel.get("videoPlayCount"))
    return value if value is not None else metric(reel.get("videoViewCount"))


def posted_at(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Existing legacy Posted on dates were UTC dates. Preserve their value;
        # date-only rows use the beginning of that recorded UTC date.
        if stamp.tzinfo is None:
            if len(value) != 10:
                return None
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return stamp
    except (TypeError, ValueError):
        return None


def snapshot_fields(properties: dict, stamp: dt.datetime | None, now: dt.datetime) -> list[str]:
    if stamp is None:
        return []
    age = (now - stamp).total_seconds() / 3600
    return [f"Views {hours}h" for hours in (24, 72)
            if hours <= age <= hours + 12
            and (properties.get(f"Views {hours}h") or {}).get("number") is None]


def cmd_stats(force: bool, dry: bool, days: int) -> int:
    state = load_state()
    now = dt.datetime.now(dt.timezone.utc)
    rows = [r for r in query(CONTENT_DB)
            if instagram_code((r["properties"].get("Posted") or {}).get("url"))]
    # Keep the existing cadence, but don't skip an eligible once-only snapshot.
    # The first pass under this policy also disregards old guessed assignments.
    due = any(snapshot_fields(r["properties"], posted_at(
        ((r["properties"].get("Posted on") or {}).get("date") or {}).get("start")), now) for r in rows)
    last = posted_at(state.get("stats_at"))
    if (last and not force and not due and state.get("stats_policy") == STATS_POLICY
            and dt.timedelta(0) <= now - last < STATS_EVERY - dt.timedelta(hours=6)):
        print(f"last pass {last.isoformat()} — under 3 days ago; no snapshot due, skipping")
        return 0
    handles = [h.strip() for h in os.environ.get("IG_HANDLES", "kurbaitaev,kurbaiitaev").split(",") if h.strip()]
    reels = scrape(handles, days)
    # Duplicate scraper records are one post, never additive views.
    by_code = {}
    for reel in reels:
        code = reel.get("shortCode")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", code) or reel.get("error"):
            continue
        old = by_code.get(code)
        if old is None or (reel_views(reel) if reel_views(reel) is not None else -1) > (reel_views(old) if reel_views(old) is not None else -1):
            by_code[code] = reel
    now = dt.datetime.now(dt.timezone.utc)  # observation clock after the scrape
    if not dry:
        ensure_columns(CONTENT_DB, CONTENT_COLUMNS)
    prev = state.get("views", {})
    report, skipped, matched = [], 0, set()
    for row in rows:
        rid = row["id"]
        # Fresh page read protects user edits to Posted and existing snapshots.
        fresh = row if dry else api(f"pages/{rid}")
        p = fresh["properties"]
        code = instagram_code((p.get("Posted") or {}).get("url"))
        reel = by_code.get(code)
        if reel is None:
            continue
        matched.add(code)
        views = reel_views(reel)
        current = metric((p.get("Views") or {}).get("number"))
        if views is None or (current is not None and views < current):
            skipped += 1
            continue  # partial/regressed input cannot refresh metrics or snapshots
        # Notion stores date-times at minute precision (verified on the live
        # database). Write that precision so exact readback stays meaningful.
        observed = now.replace(second=0, microsecond=0).isoformat(timespec="minutes")
        props = {"Views": {"number": views}, "Stats updated": {"date": {"start": observed}}}
        for column, key in (("Likes", "likesCount"), ("Comments", "commentsCount")):
            value = metric(reel.get(key))
            if value is not None:
                props[column] = {"number": value}
        date = (p.get("Posted on") or {}).get("date")
        stamp = posted_at(date.get("start")) if date else posted_at(reel.get("timestamp"))
        if not date and stamp and stamp <= now:
            stamp = stamp.replace(second=0, microsecond=0)
            props["Posted on"] = {"date": {"start": stamp.isoformat(timespec="minutes")}}
        for field in snapshot_fields(p, stamp, now):
            props[field] = {"number": views}
        name = text_of(p.get("Idea") or {}).replace(" · сценарий для съёмки", "")
        if not dry:
            api(f"pages/{rid}", "PATCH", {"properties": props})
            saved = api(f"pages/{rid}")["properties"]
            for field, value in props.items():
                if field == "Stats updated" or field == "Posted on":
                    if posted_at((saved.get(field, {}).get("date") or {}).get("start")) != posted_at(value["date"]["start"]):
                        raise RuntimeError(f"Stats date readback failed: {field}")
                elif saved.get(field, {}).get("number") != value["number"]:
                    raise RuntimeError(f"Stats metric readback failed: {field}")
        report.append((views, views - (current if current is not None else 0), name))
        prev[rid] = views
    unmatched = len(set(by_code) - matched)
    summary = f"{len(report)} rows {'would update' if dry else 'updated'}; {unmatched} unmatched; 0 rows created; {skipped} invalid/regressed rows skipped"
    print(summary, flush=True)
    if os.environ.get("INVOCATION_ID"):
        # The existing unit appends stdout to a file. Also keep its bounded
        # outcome in the unit journal without changing that log configuration.
        import syslog
        syslog.openlog("saved-to-notes-content-stats")
        syslog.syslog(syslog.LOG_INFO, summary)
    if dry:
        return 0
    state.update(views=prev, stats_at=now.isoformat(), stats_policy=STATS_POLICY)
    save_state(state)
    if report:
        top = "\n".join(f"• <b>{v:,}</b> (+{change:,}) — {esc(name)}"
                        for v, change, name in sorted(report, reverse=True)[:10])
        tell(f"📊 Просмотры — {len(report)} ролик(ов)\n{top}")
    return 0


# --- reel of the week ---------------------------------------------------------------
USUAL_VIEWS = int(os.environ.get("CONTENT_USUAL_VIEWS", "1753"))  # median of his 38 reels, Jul 1 to Oct 9 2026
BOT_NOTE = "Черновик бота"
PICK_EVERY = dt.timedelta(days=6)
KEEP_SHARE = 0.6   # a close edit that keeps less of his draft than this is thrown away
MIN_WORDS = 40     # a draft shorter than this is not a script to read to camera (the original had little speech)

PICK_PROMPT = """Magomed films Russian talking-head reels that are remakes of reels he saved. \
His usual reel gets {usual} views. Below are his remakes that did clearly better, the ones that \
did not, and the drafts waiting to be filmed.

Pick the ONE waiting draft most likely to do well for HIM. Judge by what his better remakes \
have in common (topic, tone, format) and what the weaker ones share, not by general taste.

Answer with ONE JSON object and nothing else:
{{"pick": "<draft id>", "why": "<one plain English sentence, max 30 words: which better remake it \
resembles and in what way>", "runner_up": "<draft id or empty>"}}

DID BETTER (views | title | hook):
{winners}

DID NOT (views | title | hook):
{losers}

WAITING DRAFTS (id | title | hook | how it opens):
{drafts}"""

TIGHTEN_PROMPT = """You are doing a CLOSE EDIT of a Russian script Magomed will read to camera. \
It is already an adaptation of another creator's reel; your job is only to make it better to say aloud.

Task: keep the order, examples, argument and ending. Change a line only for a clear
gain when spoken; cut or merge repeated lines. Keep the beats: each beat is 1-4 short
spoken lines without stage directions. Follow the shared content pack above for voice and rules.

Answer with ONE JSON object and nothing else:
{{"beats": [["line", "line"], ["line"]], "changes": ["<what changed and why, max 12 words, in English>"]}}
Give at most 5 changes. If the draft already reads well aloud, change little and say so.

DRAFT:
{draft}"""


def script_beats(page_id: str) -> list[list[str]]:
    """The script on a draft row: its big lines, down to the first divider."""
    beats = []
    for b in api(f"blocks/{page_id}/children?page_size=100").get("results", []):
        if b["type"] == "divider":
            break
        if b["type"] in ("heading_1", "heading_2", "heading_3"):
            lines = "".join(x.get("plain_text", "") for x in b[b["type"]].get("rich_text") or []).split("\n")
            lines = [x.strip() for x in lines if x.strip()]
            if lines:
                beats.append(lines)
    return beats


def kept_share(old: list[list[str]], new: list[list[str]]) -> float:
    """How much of the draft's wording survives the edit (words, counted with repeats)."""
    import collections
    def words(beats):
        return collections.Counter(re.findall(r"\w+", " ".join(" ".join(b) for b in beats).lower()))
    a, b = words(old), words(new)
    return sum(min(n, b[w]) for w, n in a.items()) / max(1, sum(a.values()))


def cmd_pick(dry: bool, show: bool, force: bool, as_json: bool, show_prompt: bool = False) -> int:
    state = load_state()
    picks = state.get("reel_picks", [])

    def out(p: dict | None) -> int:
        if as_json:
            print(json.dumps(p or {}, ensure_ascii=False))
        elif p:
            print(f"{p['date']}  {p['title']}\n{p['why']}\n{p['url']}\n")
            print("\n\n".join("\n".join(b) for b in p["beats"]))
        else:
            print("no pick yet")
        return 0

    last = picks[-1] if picks else None
    if show:
        return out(last)
    if last and not force and not dry and not show_prompt and dt.date.today() - dt.date.fromisoformat(last["date"]) < PICK_EVERY:
        log.info("this week's pick is already made (%s)", last["date"])
        return out(last)

    try:
        pack = content_pack("ideas", alert=not show_prompt)
        script_pack = content_pack("script", alert=not show_prompt)
    except PackUnavailable as exc:
        log.error("%s", exc)
        return 1
    rows = query(CONTENT_DB)
    def prop(r, k):
        return text_of(r["properties"].get(k) or {})

    def views(r):
        return (r["properties"].get("Views") or {}).get("number") or 0

    remakes = [r for r in rows if status_of(r) == "Published" and views(r)
               and (r["properties"].get("Source") or {}).get("url")]
    def line(r):
        return f"{views(r):,} | {prop(r, 'Idea')[:70]} | {prop(r, 'Hook / key idea')[:140]}"

    winners = [line(r) for r in sorted(remakes, key=views, reverse=True) if views(r) >= 2 * USUAL_VIEWS]
    losers = [line(r) for r in sorted(remakes, key=views) if views(r) < 2 * USUAL_VIEWS]
    done = {p["id"] for p in picks}
    waiting = {r["id"]: r for r in rows if status_of(r) == "Inbox" and BOT_NOTE in prop(r, "Notes")
               and r["id"] not in done}
    if not waiting:
        log.info("no bot drafts waiting in Inbox")
        return out(None)
    if not winners:
        log.warning("no remake has beaten twice his usual yet: nothing to judge by, no pick")
        return out(None)
    scripts = {i: script_beats(i) for i in waiting}
    def words(beats):
        return len(re.findall(r"\w+", " ".join(" ".join(x) for x in beats)))

    short = [prop(r, "Idea").split(" · ")[0] for i, r in waiting.items() if words(scripts[i]) < MIN_WORDS]
    if short:
        log.info("left out, under %d words: %s", MIN_WORDS, ", ".join(short))
    waiting = {i: r for i, r in waiting.items() if words(scripts[i]) >= MIN_WORDS}
    if not waiting:
        log.info("no waiting draft is long enough to film")
        return out(None)
    drafts = "\n".join(
        f"{i} | {prop(r, 'Idea')[:70]} | {prop(r, 'Hook / key idea')[:140]} | "
        + " / ".join(" ".join(b) for b in scripts[i][:3])[:400] for i, r in waiting.items())
    prompt = pack + "\n" + PICK_PROMPT.format(usual=f"{USUAL_VIEWS:,}", winners="\n".join(winners),
                                             losers="\n".join(losers) or "(none)", drafts=drafts)
    if show_prompt:
        print(prompt)
        return 0
    ans = ask_claude(prompt)
    pid = ans.get("pick")
    if pid not in waiting:
        raise RuntimeError(f"the pick {pid!r} is not one of the waiting drafts")
    row, old = waiting[pid], scripts[pid]
    beats, changes, kept, tightened = old, [], 1.0, False
    try:
        t = ask_writer(script_pack + "\n" + TIGHTEN_PROMPT.format(draft="\n\n".join("\n".join(b) for b in old)))
        new = [[str(x).strip() for x in b if str(x).strip()] for b in t.get("beats") or []]
        new = [b for b in new if b]
        share = kept_share(old, new) if new else 0
        if new and share >= KEEP_SHARE:
            beats, changes, kept, tightened = new, [str(c) for c in (t.get("changes") or [])][:5], share, new != old
        else:
            log.warning("close edit thrown away: it kept %.0f%% of the draft", share * 100)
    except WriterUnavailable as exc:
        log.error("pick stopped: %s", exc)
        return 1
    except Exception as e:
        log.warning("close edit failed, the draft stays as it is: %s", e)
    today = dt.date.today().isoformat()
    runner = waiting.get(ans.get("runner_up") or "")
    pick = {"id": pid, "date": today, "title": prop(row, "Idea").split(" · ")[0], "url": row.get("url", ""),
            "source": (row["properties"].get("Source") or {}).get("url") or "",
            "why": str(ans.get("why") or "").strip(), "runner_up": prop(runner, "Idea").split(" · ")[0] if runner else "",
            "waiting": len(waiting), "tightened": tightened, "kept": round(kept, 2), "changes": changes, "beats": beats}
    if dry:
        return out(pick)
    api(f"pages/{pid}", "PATCH", {"properties": {"Priority": {"select": {"name": "High"}}}})
    if tightened:
        api(f"blocks/{pid}/children", "PATCH", {"children": [
            {"type": "divider", "divider": {}},
            {"type": "paragraph", "paragraph": {"rich_text": rt(
                f"Вариант недели, {today}: тот же текст, подтянутый для речи. Оригинал выше.")}},
        ] + beat_blocks(beats)[:90]})
    state = load_state()
    state["reel_picks"] = state.get("reel_picks", []) + [pick]
    save_state(state)
    log.info("reel of the week: %s (%s)", pick["title"], "tightened" if tightened else "draft as it is")
    return out(pick)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["drafts", "stats", "pick"])
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--show-prompt", action="store_true", help="print full prompt; no models, writes or alerts")
    ap.add_argument("--source-note", type=pathlib.Path, help="local vault note for drafts --show-prompt")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=60)
    a = ap.parse_args()
    if a.show_prompt and (a.cmd == "stats" or a.show):
        ap.error("--show-prompt is for drafts or pick, without --show")
    if a.source_note and not (a.cmd == "drafts" and a.show_prompt):
        ap.error("--source-note requires drafts --show-prompt")
    if a.show_prompt and a.cmd == "drafts" and not a.source_note:
        ap.error("drafts --show-prompt requires --source-note <local vault note>")
    _pack_blocks.cache_clear()  # one cache per CLI run, including embedded invocations
    load_env()
    if a.show_prompt:
        if a.cmd == "pick":
            return cmd_pick(False, False, False, False, show_prompt=True)
        try:
            print(draft_prompt(a.source_note, alert=False))
        except (PackUnavailable, OSError) as exc:
            log.error("prompt preview unavailable: %s", type(exc).__name__)
            return 1
        return 0
    (PROJ / "state").mkdir(exist_ok=True)
    with (PROJ / "state" / "content.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("another content operation is active")
            return 75
        if a.cmd == "drafts":
            return cmd_drafts(a.backfill, a.dry_run)
        if a.cmd == "pick":
            return cmd_pick(a.dry_run, a.show, a.force, a.json)
        return cmd_stats(a.force, a.dry_run, a.days)


if __name__ == "__main__":
    sys.exit(main())
