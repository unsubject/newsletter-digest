#!/usr/bin/env python3
"""
Newsletter Digest Processor
Fetches emails labelled "Subscription" from Gmail, summarises them with Claude Sonnet
into a single consolidated digest, and generates/updates an RSS feed.
Processed emails are sent to trash.
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
FEED_DESCRIPTION  = "Auto-summarised newsletter digest, powered by Claude Sonnet"
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


# ── Claude Sonnet summariser ─────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a sharp morning-briefing analyst writing a personal daily digest.

You will receive the contents of multiple newsletter emails. Synthesise ALL of
them into a single, well-organised digest structured around themes. Write with
enough context that the reader understands *why* something matters, not just
*what* happened. Draw connections between topics when they exist across
different newsletters.

Produce a JSON object with these exact keys:

1. "themes": An array of theme objects. Identify the major themes or subject
   areas that emerge across all the emails (e.g. "US-China Trade Relations",
   "AI Industry Developments", "Consumer Tech"). For each theme:

   - "theme": A concise, descriptive theme title.
   - "summary": A 3-6 sentence briefing on this theme. Provide context — what
     happened, why it matters, and how different newsletters covered it. If
     multiple sources covered the same theme, note where they agree, differ, or
     complement each other.
   - "facts": An array of evidence items under this theme. Each item is an
     object with:
       - "statement": The specific fact, data point, quote, or claim.
       - "source": The newsletter name or sender it came from.
       - "type": Either "fact" (verifiable, objective data) or
         "interpretation" (opinion, editorial stance, conjecture, prediction).
     Include 2-6 items per theme.
   - "connections": (optional) 1-2 sentences noting how this theme links to
     another theme in this digest, if applicable. Omit if no connection exists.

   Aim for 3-7 themes. A single email can contribute to multiple themes.

2. "keywords": An array of objects, each with "keyword" (string) and "count"
   (integer), ranked by how many distinct emails mention that topic.
   Include 8-20 keywords. Be specific (e.g. "Federal Reserve rate cut" not
   just "economy").

Respond ONLY with valid JSON, no markdown fences, no preamble.
Schema:
{
  "themes": [
    {
      "theme": "...",
      "summary": "...",
      "facts": [
        {"statement": "...", "source": "...", "type": "fact|interpretation"}
      ],
      "connections": "..."
    }
  ],
  "keywords": [{"keyword": "...", "count": N}, ...]
}

Rules:
- Ignore purely promotional/advertising content with zero informational value.
- Do not invent facts. Only summarise what is explicitly in the emails.
- Merge and deduplicate overlapping coverage across newsletters.
- Clearly distinguish hard facts from opinions/interpretations via the "type" field.
- Attribute every fact or interpretation to its source newsletter.
"""


_anthropic_client: anthropic.Anthropic | None = None


def get_anthropic_client() -> anthropic.Anthropic:
    """Return a cached Anthropic client (instantiated once per run)."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def summarise_all_emails(emails: list[dict]) -> dict | None:
    """
    Call Claude Sonnet to produce a single consolidated summary of all emails.
    Returns a dict with executive_summary, key_facts, and keywords,
    or None if there is no substantive content.
    """
    client = get_anthropic_client()

    # Build a single prompt with all emails
    email_blocks = []
    for i, email in enumerate(emails, 1):
        email_blocks.append(
            f"=== EMAIL {i} of {len(emails)} ===\n"
            f"From: {email['sender']}\n"
            f"Subject: {email['subject']}\n"
            f"Date: {email['date'].strftime('%Y-%m-%d %H:%M %Z')}\n\n"
            f"{email['body']}\n"
        )

    user_content = "\n".join(email_blocks)

    message = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 4096,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": user_content}],
    )

    raw = message.content[0].text.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            return None

    if not data.get("themes"):
        return None

    return data


def _compose_summary(themes: list[dict], keywords: list[dict]) -> str:
    """Compose a plain-text summary from the theme-based structure (for RSS)."""
    parts = []
    for t in themes:
        section = f"## {t.get('theme', 'Untitled')}\n{t.get('summary', '')}"
        facts = t.get("facts", [])
        if facts:
            lines = []
            for f in facts:
                tag = "[fact]" if f.get("type") == "fact" else "[interpretation]"
                lines.append(f"  • {tag} {f.get('statement', '')} — {f.get('source', '')}")
            section += "\n" + "\n".join(lines)
        conn = t.get("connections")
        if conn:
            section += f"\n  ↳ {conn}"
        parts.append(section)
    if keywords:
        kw_str = ", ".join(f"{k['keyword']} ({k['count']})" for k in keywords)
        parts.append(f"Keywords: {kw_str}")
    return "\n\n".join(parts) if parts else ""


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


# ── Digest email sender ───────────────────────────────────────────────────────
def build_digest_html(digest: dict, sources: list[str], run_date: datetime) -> str:
    """Render the consolidated digest as a styled HTML email body."""
    date_str = run_date.strftime("%A, %d %B %Y")

    themes = digest.get("themes", [])
    keywords = digest.get("keywords", [])

    # Build theme sections
    themes_html = ""
    for t in themes:
        theme_title = html_module.escape(t.get("theme", ""))
        summary = html_module.escape(t.get("summary", ""))

        # Facts grouped by type
        facts = t.get("facts", [])
        hard_facts = [f for f in facts if f.get("type") == "fact"]
        interpretations = [f for f in facts if f.get("type") == "interpretation"]

        facts_block = ""
        if hard_facts:
            items = "".join(
                f'<li style="margin-bottom:4px;">{html_module.escape(f["statement"])} '
                f'<span style="color:#888;font-size:12px;">— {html_module.escape(f.get("source", ""))}</span></li>'
                for f in hard_facts
            )
            facts_block += (
                f'<div style="margin-top:10px;">'
                f'<div style="font-size:12px;font-weight:600;color:#2b7a4b;margin-bottom:4px;">Facts</div>'
                f'<ul style="margin:0;padding-left:18px;color:#444;font-size:13px;line-height:1.6;">{items}</ul></div>'
            )
        if interpretations:
            items = "".join(
                f'<li style="margin-bottom:4px;font-style:italic;">{html_module.escape(f["statement"])} '
                f'<span style="color:#888;font-size:12px;font-style:normal;">— {html_module.escape(f.get("source", ""))}</span></li>'
                for f in interpretations
            )
            facts_block += (
                f'<div style="margin-top:10px;">'
                f'<div style="font-size:12px;font-weight:600;color:#b8860b;margin-bottom:4px;">Interpretations &amp; Conjecture</div>'
                f'<ul style="margin:0;padding-left:18px;color:#555;font-size:13px;line-height:1.6;">{items}</ul></div>'
            )

        # Connections callout
        connections_html = ""
        conn = t.get("connections")
        if conn:
            connections_html = (
                f'<div style="margin-top:10px;padding:8px 12px;background:#f8f9ff;border-left:3px solid #7c8bbf;'
                f'font-size:12px;color:#555;line-height:1.5;">'
                f'↳ {html_module.escape(conn)}</div>'
            )

        themes_html += f"""
        <div style="margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid #eee;">
          <div style="font-size:16px;font-weight:700;color:#1a1a2e;margin-bottom:8px;">{theme_title}</div>
          <div style="color:#444;line-height:1.7;font-size:14px;">{summary}</div>
          {facts_block}
          {connections_html}
        </div>"""

    # Keywords section (ranked by occurrence)
    keywords_html = ""
    if keywords:
        kw_badges = "".join(
            f'<span style="display:inline-block;background:#f0f4ff;color:#3b5bdb;'
            f'border-radius:3px;padding:2px 8px;margin:3px 4px 3px 0;font-size:12px;">'
            f'{html_module.escape(k["keyword"])} '
            f'<span style="color:#8b9fd4;font-size:10px;">({k["count"]})</span></span>'
            for k in keywords
        )
        keywords_html = (
            f'<div style="margin-bottom:20px;">'
            f'<div style="font-size:13px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;'
            f'color:#888;margin-bottom:10px;">Topics by Frequency</div>'
            f'<div>{kw_badges}</div></div>'
        )

    # Sources list
    sources_html = ""
    if sources:
        src_items = "".join(f"<li>{html_module.escape(s)}</li>" for s in sources)
        sources_html = (
            f'<div style="margin-top:20px;padding-top:16px;border-top:1px solid #eee;">'
            f'<div style="font-size:13px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;'
            f'color:#888;margin-bottom:10px;">Sources</div>'
            f'<ul style="margin:0;padding-left:18px;color:#666;font-size:13px;line-height:1.6;">{src_items}</ul></div>'
        )

    source_count = len(sources)
    theme_count = len(themes)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:32px auto;background:#fff;border-radius:8px;
              box-shadow:0 1px 4px rgba(0,0,0,0.08);overflow:hidden;">

    <!-- Header -->
    <div style="background:#1a1a2e;padding:28px 32px;">
      <div style="color:#fff;font-size:20px;font-weight:700;">📰 Newsletter Digest</div>
      <div style="color:#aab4d4;font-size:13px;margin-top:4px;">{date_str}</div>
    </div>

    <!-- Stats bar -->
    <div style="background:#f8f9ff;padding:12px 32px;border-bottom:1px solid #eee;
                font-size:13px;color:#555;">
      <strong>{theme_count}</strong> theme{'s' if theme_count != 1 else ''} from
      <strong>{source_count}</strong> newsletter{'s' if source_count != 1 else ''}
    </div>

    <!-- Content -->
    <div style="padding:28px 32px;">
      {themes_html}
      {keywords_html}
      {sources_html}
    </div>

    <!-- Footer -->
    <div style="padding:16px 32px;background:#fafafa;border-top:1px solid #eee;
                font-size:11px;color:#aaa;text-align:center;">
      Digest generated by Claude Sonnet · Original emails have been trashed
    </div>
  </div>
</body>
</html>"""


def send_digest_email(service, digest: dict, sources: list[str], run_date: datetime):
    """Send the HTML digest to the authenticated Gmail account (self-send)."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Resolve the account's own address
    profile   = service.users().getProfile(userId="me").execute()
    own_email = profile["emailAddress"]

    date_str  = run_date.strftime("%a, %d %b %Y")
    source_count = len(sources)
    subject   = f"Newsletter Digest — {date_str} ({source_count} source{'s' if source_count != 1 else ''})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = own_email
    msg["To"]      = own_email

    html_body = build_digest_html(digest, sources, run_date)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me",
        body={"raw": raw_bytes}
    ).execute()

    print(f"  ✉ Digest email sent to {own_email}  ({source_count} source(s))")


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
        save_processed_ids(processed_ids)
        return

    # ── Phase 1: Fetch all emails ────────────────────────────────────────────
    emails: list[dict] = []
    fetched_ids: list[str] = []

    for msg_meta in messages:
        msg_id = msg_meta["id"]
        print(f"\n  Fetching {msg_id}…")

        try:
            email = get_message_detail(gmail, msg_id)
            print(f"    Subject : {email['subject']}")
            print(f"    From    : {email['sender']}")
            emails.append(email)
            fetched_ids.append(msg_id)
        except Exception as exc:
            print(f"    ✗ Error fetching {msg_id}: {exc}")

    if not emails:
        print("✗ No emails could be fetched. Exiting.")
        save_processed_ids(processed_ids)
        return

    # ── Phase 2: Summarise all emails in one Sonnet call ─────────────────────
    print(f"\n  Summarising {len(emails)} email(s) with Sonnet…")
    digest = summarise_all_emails(emails)

    if not digest:
        print("  ↳ Sonnet determined: no substantive content across all emails.")
    else:
        print("  ↳ Consolidated digest generated.")

        # Build a single RSS entry for this run
        run_date = datetime.now(timezone.utc)
        pub_date = run_date.strftime("%a, %d %b %Y %H:%M:%S +0000")
        date_label = run_date.strftime("%Y-%m-%d")

        keywords_flat = [k["keyword"] for k in digest.get("keywords", [])]
        sources = [f"{e['sender']} — {e['subject']}" for e in emails]

        entry = {
            "subject":  f"Newsletter Digest — {date_label}",
            "sender":   "Newsletter Digest Bot",
            "date":     pub_date,
            "summary":  _compose_summary(
                digest.get("themes", []),
                digest.get("keywords", []),
            ),
            "keywords": keywords_flat,
            "guid":     f"digest-{date_label}",
            "link":     FEED_LINK,
        }

        feed_entries = load_existing_feed()
        feed_entries.insert(0, entry)

        # Write updated RSS
        RSS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        rss_xml = build_rss(feed_entries)
        RSS_FILE.write_text(rss_xml, encoding="utf-8")
        print(f"\n  RSS feed written → {RSS_FILE}  (1 new digest entry added)")

        # Send digest email
        send_digest_email(gmail, digest, sources, run_date)

    # ── Phase 3: Trash processed emails and persist state ────────────────────
    for i, msg_id in enumerate(fetched_ids):
        try:
            trash_message(gmail, msg_id)
            print(f"    ✓ Trashed {emails[i]['subject']}")
        except Exception as exc:
            print(f"    ✗ Error trashing {msg_id}: {exc}")
        processed_ids.add(msg_id)

    save_processed_ids(processed_ids)
    print("\n✓ Done.")


if __name__ == "__main__":
    main()
