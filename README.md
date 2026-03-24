# Newsletter Digest

Automatically processes Gmail newsletters labelled **"Subscription"**, summarises them with **Claude Haiku**, and publishes an **RSS feed** to GitHub Pages. Processed emails are sent to Trash.

---

## How it works

1. GitHub Actions runs every morning at 07:00 UTC (≈ 3 AM ET).
2. The script fetches all unread emails with the `Subscription` label.
3. Each email body is sent to Claude Haiku, which:
   - Extracts all distinct news/information items.
   - Ignores pure advertisements.
   - Returns structured JSON: summary + keywords per item.
4. Entries are written to `docs/feed.xml` as a valid RSS 2.0 feed.
5. The feed is deployed to GitHub Pages (`gh-pages` branch).
6. Each processed email is moved to Gmail Trash.

---

## One-time Setup

### 1. Fork / clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/newsletter-digest.git
cd newsletter-digest
```

### 2. Create a Google Cloud project & OAuth credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. `newsletter-digest`)
3. Enable the **Gmail API** (APIs & Services → Library → Gmail API → Enable)
4. Go to APIs & Services → **OAuth consent screen**
   - User type: **External**
   - Fill in app name, your email, save
   - Add scope: `https://www.googleapis.com/auth/gmail.modify`
   - Add yourself as a **test user**
5. Go to APIs & Services → **Credentials** → Create Credentials → **OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON — save it as `credentials.json` locally (do NOT commit this)

### 3. Generate the OAuth token (run once on your machine)

```bash
pip install google-auth-oauthlib google-api-python-client
python scripts/generate_token.py --credentials /path/to/credentials.json
```

This opens your browser, asks you to authorise the app, then prints out the two JSON blobs you need.

### 4. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name              | Value                                              |
|--------------------------|----------------------------------------------------|
| `ANTHROPIC_API_KEY`      | Your Anthropic API key                             |
| `GMAIL_CREDENTIALS_JSON` | The full JSON from your `credentials.json` file    |
| `GMAIL_TOKEN_JSON`       | The token JSON printed by `generate_token.py`      |
| `FEED_BASE_URL`          | `https://YOUR_USERNAME.github.io/newsletter-digest`|

### 5. Enable GitHub Pages

Go to repo → **Settings → Pages**
- Source: **Deploy from a branch**
- Branch: `gh-pages` / `/ (root)`
- Save

### 6. Trigger the first run

Go to **Actions → Newsletter Digest → Run workflow** to test immediately.

---

## RSS Feed URL

Once GitHub Pages is active, your feed will be at:

```
https://YOUR_USERNAME.github.io/newsletter-digest/feed.xml
```

Add this URL to any RSS reader (NetNewsWire, Reeder, Feedly, Inoreader, etc.)

---

## Customisation

| What                          | Where                                    |
|-------------------------------|------------------------------------------|
| Run schedule                  | `.github/workflows/newsletter-digest.yml` — `cron` line |
| Gmail label to watch          | `scripts/process_newsletters.py` — `LABEL_NAME` |
| Max items kept in RSS         | `scripts/process_newsletters.py` — `MAX_FEED_ITEMS` |
| Summarisation instructions    | `scripts/process_newsletters.py` — `SYSTEM_PROMPT` |

---

## Token refresh

The OAuth token will auto-refresh via the `refresh_token` in `GMAIL_TOKEN_JSON` — no manual renewal needed as long as the app stays authorised in your Google account.

If you ever revoke access, re-run `generate_token.py` and update the `GMAIL_TOKEN_JSON` secret.
