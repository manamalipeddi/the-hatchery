# Second Brain Cron

One Railway service, runs **daily**: processes new Media Inbox videos every run,
sends the WhatsApp digest **Mondays only**.

## Setup (~20 min, one time)

### 1. Notion integration (5 min)
1. Go to notion.so/my-integrations → **New integration** → name it `Second Brain Cron`,
   internal, read + update + insert content capabilities.
2. Copy the token → `NOTION_TOKEN`.
3. Share these five things with the integration (open each → `•••` → Connections →
   `Second Brain Cron`):
   - Second Brain database
   - Projects database
   - Media Inbox database
   - Lens Library page
   - Second Brain — Master Prompts page

### 2. Twilio template (5 min + WhatsApp approval wait)
Twilio Console → Content Template Builder → Create:
- Name: `second_brain_digest`, Language: English, Category: **Utility**
- Content type: **Text**
- Body:
  ```
  🐣 The Hatchery: {{1}}

  Visit The Hatchery to read more.
  ```
  (No link variable — WhatsApp Business accounts without full verification
  have limited template capability; MedTracker's templates are Text-only for
  the same reason. Manasa checks Notion directly for the full digest.)
- Submit for WhatsApp approval (minutes to ~1 day, same as MedTracker's templates)
- Copy the Content SID (`HX...`) → `TWILIO_CONTENT_SID`

Delivery is opportunistic: set `TWILIO_WHATSAPP_FROM` to **MedTracker's
production sender** — your near-daily MedTracker traffic keeps the shared
24-hour window open, so most Tuesdays the FULL digest arrives as normal
WhatsApp messages (+ a link to its permanent Notion page). If the window is
closed (you skipped MedTracker >24h), the cron detects the async 63016
failure and falls back to this template: one-line teaser + Notion link.
Failure alerts always use the template. The digest always lives permanently
under Digest Log in Notion regardless of delivery path.

Note: shared sender means a reply to the digest lands in MedTracker's
webhook — expect the MedTracker bot to answer, harmlessly confused.

### 3. GitHub (2 min)
New private repo, push these four files.

### 4. Railway (10 min)
1. New Project → **Deploy from GitHub repo** → select the repo.
2. Service → Settings → **Cron Schedule**: `0 5 * * *`
   (05:00 UTC daily = 07:00 Stockholm in summer, 06:00 in winter — fine either way).
3. Settings → Start command: `python main.py`
4. Variables: paste everything from `.env.example` with real values.

### 5. Test (3 min)
Set `FORCE_DIGEST=1` in variables → trigger a manual run (Deployments → Redeploy)
→ digest should arrive on WhatsApp within ~2 min → **remove FORCE_DIGEST**.

## How it behaves
- **Digest prompt is fetched live** from the Master Prompts page (the code block
  containing "SECOND BRAIN DIGEST"). Edit the prompt in Notion → next Monday uses
  it. No redeploy. If the prompt can't be found, the run fails loudly.
- **Failures WhatsApp you** — no silent deaths.
- **Aging** (Alpha ≥3 months, anything inactive ≥12 months) is computed from the
  `Last active` field and announced in the digest; flips stay human, done in the
  Monday sweep. Known simplification: `Last active` is a proxy for "time in
  Alpha" — good enough for v1.
- **Near-empty weeks** (<4 new entries) are handled by the digest prompt itself:
  short note + one spark from an old Seed/Open Question.
- **Media**: YouTube links get transcripts + checkbox takeaways within a day.
  Non-YouTube links get a polite "add a manual note" checkbox instead.

## Maintenance contract
- New schema fields → update Master Prompts page first, then check whether
  `build_context()` in main.py needs to surface them.
- The two-week no-touch rule applies to this code too.
