# Gamblineers review generator

Two pipelines live in this repo during the transition.

| | file | status |
|---|---|---|
| **New** | `app.py` (+ `writeReviewAgent.py`, `gdocs.py`, `linking.py`, `history.py`, `config.py`) | Claude Opus 5, single pass |
| Legacy | `writeReviewAdam.py` | Claude section drafts → GPT-3.5 fine-tune voice rewrite → QC pass |

The legacy app is untouched and still deployable, so it remains the fallback until the
new one has run a few real batches.

## Switching production over

Streamlit serves whichever file its deployment config names. To cut over, point it at
`app.py`. Nothing else needs changing, and pointing it back at `writeReviewAdam.py`
reverts instantly.

## Secrets

Add these in Streamlit (Settings → Secrets). The `[service_account]` table is the same
one the legacy app already uses.

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
APP_PASSWORD      = "something-only-the-team-knows"
FOLDER_ID         = "<Drive folder id where reviews are created>"

# Optional; these have working defaults.
# SPREADSHEET_ID    = "1ZneRUz90Ne06pr8CCax8vp30tOtPpKJQCw5ikE-uB_0"
# GAMBLINEERS_SITE  = "Gamblineers"

[service_account]
type = "service_account"
project_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "cryptocurrencysa@writereview-460210.iam.gserviceaccount.com"
# ... remaining fields from the JSON key
```

`APP_PASSWORD` is required. With it unset the app refuses to run rather than defaulting
to open, because it holds an API key that spends money per click.

## How the new pipeline works

1. **Data** is read straight from the `Casino Data` sheet's `Data`, `Bonuses`,
   `Comments` and `StatusLog` tabs. It deliberately does *not* read `TempOutput`, whose
   formulas pre-narrow the comparison pool to five casinos and use volatile `RAND()` to
   pick hedge words ("almost"/"over") for otherwise fixed numbers.
2. **Everything countable is computed in Python** and handed over as a finished fact:
   provider set-difference against the 13 major studios, casino age, and rank-in-field
   for games/providers/cryptos/restricted-countries. Every factual error found during
   development was arithmetic left to the model.
3. **One Opus 5 call** writes the whole review, having seen the full 78-casino field, the
   editorial criteria as a reference table, Adam's real published writing as voice
   anchors, and the last N reviews so this one reads differently.
4. **Internal links** are added afterward from the live sitemap, so a fabricated URL is
   structurally impossible — the writer is forbidden from producing links itself.
5. **Upload** validates the markup twice (a round-trip reconstruction and an independent
   mid-word-boundary check) before any API call, then creates a formatted Doc in the
   folder. Titles are versioned; nothing is ever deleted.

## Anti-repetition

The last N published reviews are read from the Drive folder and fed back in, with the
openers, SEO-keyword sentences and recurring phrases extracted explicitly so the
constraint is unmissable. Drive rather than local disk because a hosted container's
filesystem is ephemeral — local history would vanish on redeploy and repetition would
return silently.

## Checking output quality

```bash
.venv/bin/python voice_metrics.py reviews_agent      # vs Adam's real reviews
.venv/bin/python voice_metrics.py --targets          # show the targets
```

Targets are measured from `examples_reviews.txt` (Adam's genuine pre-pipeline reviews),
with `examples.txt` (guide pages) as fallback. The two registers differ materially — his
guide pages ask ~7 reader-questions per 1k words where his reviews ask 0.5 — so measuring
review output against guide-page targets produces phantom problems.

Bands are wide on purpose: they catch a register collapse, not a style preference.

## CLI

The generator also runs standalone, which is the fastest way to test a change:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python writeReviewAgent.py "BitStarz"
.venv/bin/python writeReviewAgent.py "BitStarz" --dry-run   # inspect the prompt, no API call
```

`--dry-run` writes the assembled prompt to a file and spends nothing. Use it after any
prompt edit.

## Editing prompt text

`VOICE`, `CRITERIA`, `examples.txt` and `examples_reviews.txt` all sit in the cached
prompt prefix. Editing any of them invalidates the cache, so a batch's first review pays
full price again. Edit between batches, not during one.
