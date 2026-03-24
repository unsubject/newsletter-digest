#!/usr/bin/env python3
"""
Newsletter Digest Processor
Fetches emails labelled "Subscription" from Gmail, summarises them with Claude Haiku,
and generates/updates an RSS feed. Processed emails are sent to trash.
"""

import os
import json
import base64
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

import html as html_module

import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


# ── Constants ────────────────────────────────────────────────────────────────
LABEL_NAME        = "Subscription"
RSS_OUTPUT_DIR    = Path("docs")          # GitHub Pages serves from /docs on main branch
RSS_FILE          = RSS_OUTPUT_DIR / "feed.xml"
STATE_FILE        = Path("scripts/processed_ids.json")
FEED_TITLE        = "Simon's Newsletter Digest"
FEED_DESCRIPTION  = "Auto-summarised newsletter entries, powered by Claude Haiku"
FEED_LINK         = os.environ.get("FEED_BASE_URL", "https://example.github.io/newsletter-digest")
MAX_FEED_ITEMS    = 200   # Keep RSS manageable; oldest entries drop off


# ── Gmail helpers ─────────────────────────────────────────────────────────────
def build_gmail_service():
    """Build Gmail API client from credentials stored in env vars."""
    creds_json = os.environ.get("GMAIL_CREDENTIALS_JSON")
    token_json  = os.environ.get("GMAIL_TOKEN_JSON")

    if not creds_json or not token_json:
        raise EnvironmentError(
            "GMAIL_CREDENTIALS_JSON and GMAIL_TOKEN_JSON must be set as environment variables."
        )

    creds_data  = json.loads(creds_json)
    token_data  = json.loads(token_json)

    creds = Credentials(
        token         = token_data.get("token"),
        refresh_token = token_data.get("refresh_token"),
        token_uri     = creds_data["installed"]["token_uri"],
        client_id     = creds_data["installed"]["client_id"],
        client_secret = creds_data["installed"]["client_secret"],
        scopes        = ["https://www.googleapis.com/auth/gmail.modify"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("gmail", "v1", credentials=creds)


def get_label_id(service, label_name: str) -> str | None:
    """Return the Gmail label ID for a given display name."""
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    return None


def fetch_unprocessed_messages(service, label_id: str, processed_ids: set) -> list:
    """Return list of message metadata not yet processed."""
    messages = []
    page_token = None

    while True:
        kwargs = dict(userId="me", labelIds=[label_id], maxResults=50)
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        batch    = response.get("messages", [])
        messages.extend([m for m in batch if m["id"] not in processed_ids])

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return messages


def get_message_detail(service, msg_id: str) -> dict:
    """Fetch full message and parse into a clean dict."""
    raw = service.users().messages().get(
        userId="me", id=msg_id, format="raw"
    ).execute()

    msg_bytes = base64.urlsafe_b64decode(raw["raw"])
    msg       = message_from_bytes(msg_bytes)

    # ── Headers ───────────────────────────────────────────────────────────────
    subject = str(make_header(decode_header(msg.get("Subject", "(no subject)"))))
    sender  = str(make_header(decode_header(msg.get("From", "Unknown"))))
    date_str = msg.get("Date", "")

    try:
        date_dt = parsedate_to_datetime(date_str)
    except Exception:
        date_dt = datetime.now(timezone.utc)

    # ── Body extraction ───────────────────────────────────────────────────────
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset   = part.get_content_charset() or "utf-8"
                body_text = part.get_payload(decode=True).decode(charset, errors="replace")
                break
        # Fallback: grab HTML if no plain text
        if not body_text:
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    charset   = part.get_content_charset() or "utf-8"
                    html      = part.get_payload(decode=True).decode(charset, errors="replace")
                    body_text = re.sub(r"<[^>]+>", " ", html)
                    body_text = html_module.unescape(body_text)
                    body_text = re.sub(r"\s{2,}", " ", body_text).strip()
                    break
    else:
        charset   = msg.get_content_charset() or "utf-8"
        body_text = msg.get_payload(decode=True).decode(charset, errors="replace")

    return {
        "id":       msg_id,
        "subject":  subject,
        "sender":   sender,
        "date":     date_dt,
        "body":     body_text[:12000],  # cap to avoid huge token bills
    }


def trash_message(service, msg_id: str):
    """Move a message to Gmail Trash."""
    service.users().messages().trash(userId="me", id=msg_id).execute()


# ── Claude Haiku summariser ───────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a ruthlessly efficient newsletter analyst.

Your job:
1. Read the email body provided.
2. Decide whether there is substantive content (news, analysis, insights, facts, announcements).
3. If the email is purely promotional/advertising with zero informational value, respond with exactly:
   {"items": []}
4. Otherwise, identify ALL distinct subjects/news items — even if there are many.
   For each item produce a JSON object with these exact keys:
   - "summary":  2-5 sentences capturing key points and key facts. Be specific — include numbers, names, dates where present.
   - "keywords": array of 3-8 concise keyword strings relevant to this item.
   
   Respond ONLY with valid JSON, no markdown fences, no preamble.
   Schema: {"items": [ {"summary": "...", "keywords": ["...", ...]}, ... ]}

Rules:
- If an email mixes ads and real content, extract the real content and ignore the ads.
- Do not invent facts. Only summarise what is explicitly in the email.
- Keywords should be specific (e.g. "Federal Reserve rate cut" not just "economy").
"""


_anthropic_client: anthropic.Anthropic | None = None


def get_anthropic_client() -> anthropic.Anthropic:
    """Return a cached Anthropic client (instantiated once per run)."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def summarise_with_haiku(email: dict) -> list[dict]:
    """
    Call Claude Haiku to summarise the email.
    Returns a list of item dicts: [{summary, keywords}, ...]
    Each item will later be enriched with sender/subject/date.
    """
    client = get_anthropic_client()

    user_content = (
        f"From: {email['sender']}\n"
        f"Subject: {email['subject']}\n"
        f"Date: {email['date'].strftime('%Y-%m-%d %H:%M %Z')}\n\n"
        f"--- EMAIL BODY ---\n{email['body']}"
    )

    message = client.messages.create(
        model      = "claude-haiku-4-5-20251001",
        max_tokens = 2048,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": user_content}],
    )

    raw = message.content[0].text.strip()

    try:
        data  = json.loads(raw)
        items = data.get("items", [])
    except json.JSONDecodeError:
        # Defensive: try to find JSON block inside response
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            items = json.loads(match.group()).get("items", [])
        else:
            items = []

    return items


# ── RSS builder ───────────────────────────────────────────────────────────────
def _xml_text(item: ET.Element, tag: str) -> str:
    """Safely get text from a child element."""
    el = item.find(tag)
    return el.text if el is not None and el.text else ""


def load_existing_feed() -> list[dict]:
    """Parse existing RSS feed into a list of entry dicts."""
    if not RSS_FILE.exists():
        return []

    try:
        tree    = ET.parse(RSS_FILE)
        root    = tree.getroot()
        channel = root.find("channel")
        entries = []

        for item in channel.findall("item"):
            keywords_raw = _xml_text(item, "keywords")
            keywords     = [k.strip() for k in keywords_raw.split(",")] if keywords_raw else []

            entries.append({
                "title":    _xml_text(item, "title"),
                "sender":   _xml_text(item, "author"),
                "subject":  _xml_text(item, "title"),
                "date":     _xml_text(item, "pubDate"),
                "summary":  _xml_text(item, "description"),
                "keywords": keywords,
                "link":     _xml_text(item, "link"),
                "guid":     _xml_text(item, "guid"),
            })

        return entries
    except Exception:
        return []


def build_rss(entries: list[dict]) -> str:
    """Generate RSS 2.0 XML string from a list of entry dicts."""
    rss   = ET.Element("rss", version="2.0")
    chan  = ET.SubElement(rss, "channel")

    ET.SubElement(chan, "title").text         = FEED_TITLE
    ET.SubElement(chan, "link").text          = FEED_LINK
    ET.SubElement(chan, "description").text   = FEED_DESCRIPTION
    ET.SubElement(chan, "language").text      = "en"
    ET.SubElement(chan, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for e in entries[:MAX_FEED_ITEMS]:
        item = ET.SubElement(chan, "item")

        ET.SubElement(item, "title").text       = e.get("subject", "Newsletter Entry")
        ET.SubElement(item, "author").text      = e.get("sender", "")
        ET.SubElement(item, "description").text = e.get("summary", "")
        ET.SubElement(item, "pubDate").text     = e.get("date", "")
        guid_el = ET.SubElement(item, "guid")
        guid_el.set("isPermaLink", "false")
        guid_el.text = e.get("guid", e.get("summary", "")[:80])
        ET.SubElement(item, "link").text        = FEED_LINK

        keywords = e.get("keywords", [])
        if keywords:
            ET.SubElement(item, "keywords").text = ", ".join(keywords)

        # Also add keywords as individual <category> tags for RSS reader compatibility
        for kw in keywords:
            ET.SubElement(item, "category").text = kw

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


# ── State persistence ─────────────────────────────────────────────────────────
def load_processed_ids() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_processed_ids(ids: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("▶ Starting newsletter digest run…")

    # Setup
    gmail         = build_gmail_service()
    processed_ids = load_processed_ids()
    label_id      = get_label_id(gmail, LABEL_NAME)

    if not label_id:
        print(f"✗ Gmail label '{LABEL_NAME}' not found. Exiting.")
        return

    # Fetch unprocessed messages
    messages = fetch_unprocessed_messages(gmail, label_id, processed_ids)
    print(f"  Found {len(messages)} unprocessed message(s).")

    if not messages:
        print("✓ Nothing to process.")
        # Still save state in case IDs changed (e.g. after manual trash)
        save_processed_ids(processed_ids)
        return

    # Load existing RSS entries
    feed_entries = load_existing_feed()
    new_count    = 0

    for msg_meta in messages:
        msg_id = msg_meta["id"]
        print(f"\n  Processing {msg_id}…")

        try:
            email = get_message_detail(gmail, msg_id)
            print(f"    Subject : {email['subject']}")
            print(f"    From    : {email['sender']}")

            items = summarise_with_haiku(email)

            if not items:
                print("    ↳ Haiku determined: no substantive content. Skipping.")
            else:
                print(f"    ↳ Haiku extracted {len(items)} item(s).")
                pub_date = email["date"].strftime("%a, %d %b %Y %H:%M:%S +0000")

                for i, item in enumerate(items):
                    entry = {
                        "subject":  email["subject"] if len(items) == 1 else f"{email['subject']} [{i+1}/{len(items)}]",
                        "sender":   email["sender"],
                        "date":     pub_date,
                        "summary":  item.get("summary", ""),
                        "keywords": item.get("keywords", []),
                        "guid":     f"{msg_id}-{i}",
                        "link":     FEED_LINK,
                    }
                    feed_entries.insert(0, entry)  # newest first
                    new_count += 1

            # Trash the email
            trash_message(gmail, msg_id)
            print(f"    ✓ Trashed.")

            processed_ids.add(msg_id)

        except Exception as exc:
            print(f"    ✗ Error processing {msg_id}: {exc}")
            # Don't trash on error — leave for retry

    # Write updated RSS
    RSS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rss_xml = build_rss(feed_entries)
    RSS_FILE.write_text(rss_xml, encoding="utf-8")
    print(f"\n  RSS feed written → {RSS_FILE}  ({new_count} new item(s) added)")

    # Persist processed IDs
    save_processed_ids(processed_ids)

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
