"""
Second Brain cron — runs daily on Railway.
- Every run: process new Media Inbox entries (transcript -> checkbox takeaways)
- Tuesdays only: compose + send the weekly digest via WhatsApp (covers Monday-evening sweep)

Design notes:
- Digest prompt is fetched LIVE from the Master Prompts Notion page
  (code block containing "SECOND BRAIN DIGEST"), so editing the prompt
  in Notion requires no redeploy. Master page stays the source of truth.
- Aging rules (Alpha 3mo / Abandoned 12mo) are computed in code and
  handed to Claude as candidates to ANNOUNCE. Flips stay human (sweep).
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from anthropic import Anthropic
from twilio.rest import Client as TwilioClient
from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------- config

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWILIO_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH = os.environ["TWILIO_AUTH_TOKEN"]
WA_FROM = os.environ["TWILIO_WHATSAPP_FROM"]   # e.g. whatsapp:+14155238886
WA_TO = os.environ["MY_WHATSAPP_TO"]           # e.g. whatsapp:+46XXXXXXXXX
TWILIO_CONTENT_SID = os.environ["TWILIO_CONTENT_SID"]  # approved template HX...

SECOND_BRAIN_DB = "80e3108f6eb54ce497d50a5c8ee6b265"
PROJECTS_DB = "f34030c4fda748309cad9994b8ba8cc2"
MEDIA_INBOX_DB = "4e96a7fad6c54565b96f3a4f32163540"
LENS_LIBRARY_PAGE = "3935f362-f277-81b8-bf53-c27656033031"
MASTER_PROMPTS_PAGE = "3925f362-f277-8134-a0d5-d024eb4a3604"
DIGEST_LOG_PAGE = "3935f362-f277-814b-a3e4-ef67918baa54"

MODEL = "claude-sonnet-5"      # current Sonnet tier (released 2026-06-30)
NEAR_EMPTY_THRESHOLD = 4        # fewer new entries than this -> spark mode
DIGEST_WINDOW_DAYS = 8          # Tuesday digest covers Monday-evening sweep
FORCE_DIGEST = os.environ.get("FORCE_DIGEST") == "1"  # manual test runs

NOTION = requests.Session()
NOTION.headers.update({
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
})
claude = Anthropic(api_key=ANTHROPIC_API_KEY)
twilio = TwilioClient(TWILIO_SID, TWILIO_AUTH)


# ---------------------------------------------------------------- notion helpers

def query_db(db_id, filter_=None):
    """Query a Notion database, handling pagination."""
    results, cursor = [], None
    while True:
        payload = {}
        if filter_:
            payload["filter"] = filter_
        if cursor:
            payload["start_cursor"] = cursor
        r = NOTION.post(f"https://api.notion.com/v1/databases/{db_id}/query",
                        json=payload)
        r.raise_for_status()
        data = r.json()
        results += data["results"]
        if not data.get("has_more"):
            return results
        cursor = data["next_cursor"]


def prop_text(page, name):
    """Extract plain text from a title/rich_text/select/multi_select/date/url prop."""
    p = page["properties"].get(name)
    if not p:
        return ""
    t = p["type"]
    if t in ("title", "rich_text"):
        return "".join(x["plain_text"] for x in p[t])
    if t == "select":
        return p[t]["name"] if p[t] else ""
    if t == "multi_select":
        return ", ".join(x["name"] for x in p[t])
    if t == "date":
        return p[t]["start"] if p[t] else ""
    if t == "url":
        return p[t] or ""
    if t == "number":
        return "" if p[t] is None else str(p[t])
    return ""


def page_blocks_text(page_id, include_unchecked=True):
    """Flatten a page's blocks to plain text. to_do blocks keep [x]/[ ] markers."""
    lines, cursor = [], None
    while True:
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = NOTION.get(url)
        r.raise_for_status()
        data = r.json()
        for b in data["results"]:
            t = b["type"]
            rich = b.get(t, {}).get("rich_text", [])
            text = "".join(x["plain_text"] for x in rich)
            if not text:
                continue
            if t == "to_do":
                checked = b[t].get("checked", False)
                if not checked and not include_unchecked:
                    continue
                lines.append(("[x] " if checked else "[ ] ") + text)
            elif t.startswith("heading"):
                lines.append("## " + text)
            elif t == "code":
                lines.append("```\n" + text + "\n```")
            else:
                lines.append(text)
        if not data.get("has_more"):
            return "\n".join(lines)
        cursor = data["next_cursor"]


def set_status(page_id, status):
    NOTION.patch(f"https://api.notion.com/v1/pages/{page_id}",
                 json={"properties": {"Status": {"select": {"name": status}}}}
                 ).raise_for_status()


def append_todos(page_id, items):
    children = [{
        "object": "block", "type": "to_do",
        "to_do": {"rich_text": [{"type": "text", "text": {"content": i[:1900]}}],
                  "checked": False},
    } for i in items]
    NOTION.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                 json={"children": children}).raise_for_status()


import re

def _rich_text(text):
    """Parse inline markdown (**bold**, *italic*, [label](url), bare URLs)
    into Notion rich_text objects. Keeps each object under Notion's limits."""
    # tokenize on links first, then bold, then italic
    tokens = []
    pattern = re.compile(
        r'\[([^\]]+)\]\((https?://[^\s)]+)\)'      # [label](url)
        r'|(\*\*)(.+?)\*\*'                          # **bold**
        r'|(\*)(.+?)\*'                              # *italic*
        r'|(https?://[^\s)]+)')                      # bare url
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            tokens.append(("plain", text[pos:m.start()]))
        if m.group(1):                               # linked label
            tokens.append(("link", m.group(1), m.group(2)))
        elif m.group(3):                             # bold
            tokens.append(("bold", m.group(4)))
        elif m.group(5):                             # italic
            tokens.append(("italic", m.group(6)))
        elif m.group(7):                             # bare url
            tokens.append(("link", m.group(7), m.group(7)))
        pos = m.end()
    if pos < len(text):
        tokens.append(("plain", text[pos:]))

    out = []
    for tok in tokens:
        kind = tok[0]
        if kind == "plain":
            content, ann, link = tok[1], {}, None
        elif kind == "bold":
            content, ann, link = tok[1], {"bold": True}, None
        elif kind == "italic":
            content, ann, link = tok[1], {"italic": True}, None
        elif kind == "link":
            content, ann, link = tok[1], {}, tok[2]
        if not content:
            continue
        obj = {"type": "text", "text": {"content": content[:1900]}}
        if link:
            obj["text"]["link"] = {"url": link}
        if ann:
            obj["annotations"] = ann
        out.append(obj)
    return out or [{"type": "text", "text": {"content": text[:1900]}}]


def _blocks_from_markdown(text):
    """Turn a digest's markdown into Notion blocks: headings, bullets,
    dividers, and paragraphs with inline formatting."""
    blocks = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.strip() == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _rich_text(line[4:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _rich_text(line[3:])}})
        elif line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _rich_text(line[2:])}})
        elif line.lstrip().startswith(("- ", "— ", "• ")):
            content = line.lstrip()[2:]
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich_text(content)}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _rich_text(line)}})
    return blocks


def create_log_entry(title, text):
    """Write a digest as its own child page under the Digest Log, rendering
    markdown into real Notion headings/bold/links. Returns its URL."""
    blocks = _blocks_from_markdown(text)
    r = NOTION.post("https://api.notion.com/v1/pages", json={
        "parent": {"page_id": DIGEST_LOG_PAGE},
        "properties": {"title": {"title": [{"type": "text",
                                            "text": {"content": title}}]}},
        "children": blocks[:100],
    })
    r.raise_for_status()
    page_id = r.json()["id"]
    # Notion caps children at 100 per request; append the rest in batches
    for i in range(100, len(blocks), 100):
        NOTION.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                     json={"children": blocks[i:i+100]}).raise_for_status()
    return r.json()["url"].replace("www.notion.so", "app.notion.com")


def read_log_tail(n=3):
    """Full text of the last n digest child pages (the digest's memory)."""
    pages, cursor = [], None
    while True:
        url = f"https://api.notion.com/v1/blocks/{DIGEST_LOG_PAGE}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = NOTION.get(url)
        r.raise_for_status()
        data = r.json()
        pages += [(b["id"], b["child_page"]["title"])
                  for b in data["results"] if b["type"] == "child_page"]
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return "\n\n".join(f"## {title}\n{page_blocks_text(pid)}"
                       for pid, title in pages[-n:])


# ---------------------------------------------------------------- whatsapp

def notify(teaser):
    """Send the approved WhatsApp utility template: {{1}} teaser only.
    No link — Manasa checks Notion directly. Template variables must be
    single-line."""
    teaser = " ".join(teaser.split())[:550]  # no newlines/tabs, sane length
    twilio.messages.create(from_=WA_FROM, to=WA_TO,
                           content_sid=TWILIO_CONTENT_SID,
                           content_variables=json.dumps({"1": teaser}))


def _chunk(text, limit=1450):
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > limit:
            chunks.append(current.strip())
            current = para
        else:
            current += ("\n\n" if current else "") + para
    if current.strip():
        chunks.append(current.strip())
    return chunks


def try_freeform(text):
    """Attempt a free-form WhatsApp send (works only inside the 24h window,
    which MedTracker's daily traffic usually keeps open — shared sender).
    Twilio accepts out-of-window sends and fails them ASYNC with error 63016,
    so: send the first chunk, poll its status, only then commit to the rest.
    Returns True if delivery is happening, False if the window was closed."""
    chunks = _chunk(text)
    total = len(chunks)
    first = twilio.messages.create(
        from_=WA_FROM, to=WA_TO,
        body=(f"(1/{total})\n" if total > 1 else "") + chunks[0])
    for _ in range(12):                      # up to ~24s
        time.sleep(2)
        status = twilio.messages(first.sid).fetch().status
        if status in ("delivered", "read"):
            break
        if status in ("failed", "undelivered"):
            return False
    # delivered, or still in transit after 24s (assume window open)
    for i, chunk in enumerate(chunks[1:], 2):
        twilio.messages.create(from_=WA_FROM, to=WA_TO,
                               body=f"({i}/{total})\n" + chunk)
    return True


# ---------------------------------------------------------------- job 1: media inbox

def extract_youtube_id(url):
    import re
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def process_media_inbox():
    new_rows = [p for p in query_db(MEDIA_INBOX_DB)
                if prop_text(p, "Status") in ("", "New")]
    processed = []
    for page in new_rows:
        title = prop_text(page, "Title") or "(untitled)"
        url = prop_text(page, "URL")
        vid = extract_youtube_id(url)
        if not vid:
            append_todos(page["id"], ["No YouTube transcript available — "
                                      "add a manual note if this mattered."])
            set_status(page["id"], "Processed")
            continue
        try:
            transcript = " ".join(
                seg["text"] for seg in
                YouTubeTranscriptApi().fetch(vid).to_raw_data())[:60000]
        except Exception as e:
            append_todos(page["id"], [f"Transcript fetch failed ({type(e).__name__}) — "
                                      "add a manual note if this mattered."])
            set_status(page["id"], "Processed")
            continue
        msg = claude.messages.create(
            model=MODEL, max_tokens=1200,
            messages=[{"role": "user", "content":
                f"Video: {title}\n\nTranscript:\n{transcript}\n\n"
                "Extract 4-8 point-wise takeaways: concrete, applicable principles "
                "or strategies, one line each, no fluff, no chapter summaries. "
                "Return ONLY the takeaway lines, one per line, no numbering."}])
        takeaways = [l.strip("-• ").strip()
                     for l in msg.content[0].text.strip().split("\n") if l.strip()]
        append_todos(page["id"], takeaways[:8])
        set_status(page["id"], "Processed")
        processed.append(title)
    return processed


# ---------------------------------------------------------------- job 2: weekly digest

def get_digest_prompt():
    """Live-fetch the digest prompt from the Master Prompts page."""
    full = page_blocks_text(MASTER_PROMPTS_PAGE)
    for block in full.split("```"):
        if "SECOND BRAIN DIGEST" in block:
            return block.strip()
    raise RuntimeError("Digest prompt not found on Master Prompts page")


def notion_url(page):
    return page.get("url", "").replace("www.notion.so", "app.notion.com")


def build_context():
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=DIGEST_WINDOW_DAYS)).date().isoformat()

    # Second Brain: all punchlines + full bodies for window entries
    entries = query_db(SECOND_BRAIN_DB)
    all_lines, window_entries = [], []
    for p in entries:
        line = (f"- [{prop_text(p,'Date')}] {prop_text(p,'Title')} "
                f"({prop_text(p,'Category')} | {prop_text(p,'Status')}): "
                f"{prop_text(p,'Punchline')} | {notion_url(p)}")
        all_lines.append(line)
        if prop_text(p, "Date") >= window_start:
            window_entries.append(p)

    window_bodies = []
    for p in window_entries:
        window_bodies.append(f"### {prop_text(p,'Title')} ({prop_text(p,'Date')}) "
                             f"| {notion_url(p)}\n{page_blocks_text(p['id'])}")

    # Projects + aging candidates
    projects, aging = [], []
    for p in query_db(PROJECTS_DB):
        status = prop_text(p, "Status")
        last = prop_text(p, "Last active")
        projects.append(f"- {prop_text(p,'Name')} [{status}"
                        f"{' | queue '+prop_text(p,'Queue') if prop_text(p,'Queue') else ''}]"
                        f" — {prop_text(p,'What it is')}"
                        f"{' | Parked reason: '+prop_text(p,'Parked reason') if prop_text(p,'Parked reason') else ''}"
                        f"{' | last active '+last if last else ''} | {notion_url(p)}")
        if last:
            age = (now.date() - datetime.fromisoformat(last[:10]).date()).days
            if status == "Alpha" and age >= 90:
                aging.append(f"- {prop_text(p,'Name')}: Alpha, {age}d since last "
                             "active -> ask: still in use? Done if yes, Paused if no.")
            if status in ("Paused", "In Progress", "Alpha") and age >= 365:
                aging.append(f"- {prop_text(p,'Name')}: {age}d inactive -> announce "
                             "flip to Abandoned, object if wrong.")

    lenses = page_blocks_text(LENS_LIBRARY_PAGE)

    # Media: checked takeaways (Reviewed) + this week's shares as attention signals
    media, shared_this_week = [], []
    for p in query_db(MEDIA_INBOX_DB):
        if p.get("created_time", "")[:10] >= window_start:
            shared_this_week.append(f"- {prop_text(p,'Title')}")
        if prop_text(p, "Status") == "Reviewed":
            checked = [l for l in
                       page_blocks_text(p["id"]).split("\n") if l.startswith("[x]")]
            if checked:
                media.append(f"From '{prop_text(p,'Title')}':\n" + "\n".join(checked))

    # Digest memory: the last 3 digest pages
    log_tail = read_log_tail(3)

    return (f"TODAY: {now.date().isoformat()}\n"
            f"NEW ENTRIES IN WINDOW (last {DIGEST_WINDOW_DAYS}d): {len(window_entries)}\n\n"
            f"=== PROJECTS ===\n" + "\n".join(projects) + "\n\n"
            f"=== AGING CANDIDATES (announce, never flip silently) ===\n"
            + ("\n".join(aging) or "none") + "\n\n"
            f"=== LENS LIBRARY ===\n{lenses}\n\n"
            f"=== WINDOW ENTRIES, FULL BODIES ===\n"
            + ("\n\n".join(window_bodies) or "none") + "\n\n"
            f"=== ALL PUNCHLINES, FULL DATABASE ===\n" + "\n".join(all_lines) + "\n\n"
            f"=== MEDIA SHARED THIS WEEK (attention signals) ===\n"
            + ("\n".join(shared_this_week) or "none") + "\n\n"
            f"=== CHECKED MEDIA TAKEAWAYS ===\n" + ("\n\n".join(media) or "none") + "\n\n"
            f"=== DIGEST LOG (last digests — never repeat their chew-on threads, "
            f"stress-tests, tensions, or external finds) ===\n"
            + (log_tail or "empty — this is the first digest"))


def run_digest():
    prompt = get_digest_prompt()
    context = build_context()
    tools = [{"type": "web_search_20250305", "name": "web_search",
              "max_uses": 4}]
    user_content = (
        f"{prompt}\n\nAll Notion data is provided below. Use web search ONLY "
        f"for the OUTSIDE THE DATABASE section.\n\n"
        f"FORMATTING (the digest renders as a Notion page):\n"
        f"- Use '## ' for the five section headers and '### ' for sub-labels.\n"
        f"- Use '- ' for bullet lists (footer, finds).\n"
        f"- Use **bold** for entry titles and key terms; *italic* for quoted\n"
        f"  seed text.\n"
        f"- Links MUST be markdown: [short label](url) — never a bare pasted\n"
        f"  URL mid-sentence, never a raw entry title without its link.\n"
        f"- Keep paragraphs short (2-4 sentences). Use '---' between sections.\n"
        f"Begin your response with one line starting 'TEASER: ' — this becomes "
        f"the WhatsApp message and must earn an open on its own. Name the single "
        f"most interesting SPECIFIC finding of the week — the actual idea, "
        f"tension, or connection, never 'your digest is ready' or 'this week's "
        f"themes' (labels, not headlines). Authentic, not clickbait: no "
        f"manufactured suspense or withheld hooks; the pull comes from it being "
        f"TRUE and about her actual thinking. One concrete sentence, ≤25 words, "
        f"like a smart friend texting 'hey, noticed something about your week'. "
        f"Good: 'Two of your entries this week quietly contradict each other "
        f"about who AI should serve.' Weak: 'Interesting patterns this week.' "
        f"Put the teaser line BEFORE any heading.\n\n{context}")

    messages = [{"role": "user", "content": user_content}]
    # Continuation loop: web_search is a server-side tool. The model may pause
    # (stop_reason 'pause_turn') mid-search, or end on a tool_use, before it
    # has written the final digest text. Keep feeding its own output back until
    # it finishes with a normal 'end_turn'. Cap iterations so a misbehaving
    # run can't loop forever.
    msg = None
    for _ in range(8):
        msg = claude.messages.create(
            model=MODEL, max_tokens=16000, tools=tools, messages=messages)
        if msg.stop_reason in ("pause_turn", "tool_use"):
            messages.append({"role": "assistant", "content": msg.content})
            # server-side tool results are already attached to msg.content on
            # the next call; we just continue the turn
            continue
        break

    digest = "\n".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text").strip()
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            "Digest hit max_tokens — truncated. Raise the limit or shorten the "
            "prompt; not sending a half-written digest.")
    if not digest:
        raise RuntimeError(
            f"Digest came back empty (stop_reason={getattr(msg,'stop_reason',None)})")
    teaser = "Your Hatchery digest is ready"
    if digest.startswith("TEASER:"):
        first, _, rest = digest.partition("\n")
        teaser = first.replace("TEASER:", "").strip()
        digest = rest.strip()
    url = create_log_entry(
        f"Digest {datetime.now(timezone.utc).date().isoformat()}", digest)
    delivered = try_freeform(f"{digest}\n\nFull version with links: {url}")
    if not delivered:
        notify(teaser)
        print("Window closed — sent template doorbell instead.")


# ---------------------------------------------------------------- entrypoint

if __name__ == "__main__":
    try:
        processed = process_media_inbox()
        if processed:
            print(f"Media processed: {processed}")
        is_tuesday = datetime.now(timezone.utc).weekday() == 1
        if is_tuesday or FORCE_DIGEST:
            run_digest()
            print("Digest sent.")
        else:
            print("Not Tuesday — media only.")
    except Exception as e:
        # failure must be loud, not silent
        try:
            notify(f"⚠️ cron failed: {type(e).__name__}: {e} — check Railway logs")
        finally:
            raise
