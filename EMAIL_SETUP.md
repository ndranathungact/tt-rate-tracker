# Email Alerts via GitHub Actions (SMTP)

The tracker sends **formatted HTML emails** (rate card + next-day forecast) in
addition to — or instead of — the MacroDroid push. It's sent directly from
`scrape.py` using Python's standard-library `smtplib` (no extra dependencies).

Both channels are independent toggles:

| Toggle (workflow env) | Default | Effect |
|---|---|---|
| `NOTIFY_MACRODROID` | `true` | MacroDroid push on buy-change |
| `NOTIFY_EMAIL` | `false` | HTML email (change / signal / digest) |

---

## Pick a provider — you do *not* need a new Google account

The code is **provider-agnostic**: it just needs `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USERNAME`, `SMTP_PASSWORD`. Port `465` uses SSL; any other port (`587`,
`2525`) uses STARTTLS — handled automatically. We send ~1 email/day, so every
free tier below is wildly sufficient.

| Provider | Free tier | SMTP host / port | New account? | Domain needed? |
|---|---|---|---|---|
| **Brevo** (easiest, recommended) | 300/day | `smtp-relay.brevo.com` / `587` | sign up w/ existing email | no |
| **Mailjet** | 200/day | `in-v3.mailjet.com` / `587` | sign up | no |
| **SMTP2GO** | 1,000/mo | `mail.smtp2go.com` / `587` | sign up | no (25/hr until verified) |
| **Gmail** | ~500/day | `smtp.gmail.com` / `465` | use an *existing* Gmail | no |
| **Outlook/M365** | ~limited | `smtp.office365.com` / `587` | existing account | no |

**Recommendation:** if you'd rather not touch Google, use **Brevo** — sign up
with any email, verify your sender address, create an "SMTP key", and set
`SMTP_HOST=smtp-relay.brevo.com`, `SMTP_PORT=587`, `SMTP_USERNAME`=your Brevo
login, `SMTP_PASSWORD`=the SMTP key. Skip to Step 2. Otherwise follow Step 1 for
Gmail (uses an *existing* account — no new one required).

---

## Step 1 (Gmail option) — Create a Gmail App Password

A normal Google password won't work for SMTP; you need an **App Password**, which
requires 2-Step Verification.

1. Use a Google account for sending (a **dedicated/throwaway Gmail is ideal**, so
   the app password is isolated from your main account).
2. Google Account → **Security** → enable **2-Step Verification**.
3. Go to **https://myaccount.google.com/apppasswords**.
4. Create an app password named `tt-rate`. Google shows a **16-character**
   password like `abcd efgh ijkl mnop` — **copy it and remove the spaces**
   (`abcdefghijklmnop`).

> The app password only grants SMTP mail-send and is **revocable** any time from
> the same page. It never exposes your main Google password.

---

## Step 2 — Add the GitHub secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository
secret**, add these three:

| Secret name | Value |
|---|---|
| `SMTP_USERNAME` | the sending Gmail address, e.g. `you.tracker@gmail.com` |
| `SMTP_PASSWORD` | the 16-char app password (no spaces) |
| `EMAIL_TO` | where alerts go — your personal email (comma-separate for several) |

The workflow already wires these in. `EMAIL_FROM` defaults to `SMTP_USERNAME`.

---

## Step 3 — Turn the email channel on

In [.github/workflows/tt-rate.yml](.github/workflows/tt-rate.yml), flip:

```yaml
NOTIFY_EMAIL: "false"   ->   NOTIFY_EMAIL: "true"
```

(Keep it `false` until the three secrets exist, otherwise change-runs will go red
on a failed send.) To later **disable MacroDroid** and go email-only, set
`NOTIFY_MACRODROID: "false"`.

---

## Step 4 — Test

**Locally** (sends a real email using your shell's env vars):

```bash
SMTP_USERNAME="you.tracker@gmail.com" \
SMTP_PASSWORD="abcdefghijklmnop" \
EMAIL_TO="you@personal.com" \
python3 scrape.py --email-test
```

Add `--dry-run` to **build the email without sending** (prints the subject + a
snippet) — useful to check config wiring first.

**From GitHub:** Actions → *TT Rate Tracker* → **Run workflow** → tick **force**.
The first real buy-change (or a forced run) will email you.

---

## The three email templates

All share one design (rate card + forecast block); the framing differs:

| Template | When it sends | Subject example |
|---|---|---|
| **change** | the BUY rate moved today (or a forced run) | `HNB USD TT: Buy 334.50 (▲0.50) · BUY` |
| **signal** | forecast turned actionable (`BUY`/`SELL`) on a *flat* day | `USD BUY signal — buy 334.50 → 335.10 (+0.60)` |
| **digest** | opt-in once-a-day summary, even when nothing changed | `HNB USD TT daily — Buy 334.50 · HOLD` |

Every email shows: buy / sell / spread, the **day-over-day change** (▲/▼ vs the
previous close), and the **next-business-day forecast** (signal, predicted buy +
band, model, confidence, and live accuracy — rolling MAE + hit-rate). Before
~10 days of data it shows `WARMUP`.

### Dispatch policy (no spam)
The job runs ~33×/weekday but you get **at most one email per kind per day**,
deduped via `data/notify_state.json`:
- **change** fires whenever the rate actually moves.
- **signal** / **digest** only fire **after** `EMAIL_DAILY_AFTER_UTC` (default
  `05:30` UTC, i.e. once the morning rate has settled), once per day.
- Turn on the daily digest with `EMAIL_DAILY_DIGEST: "true"` in the workflow.

## Troubleshooting

- **`535 Username and Password not accepted`** → wrong/again-spaced credential, or
  (Gmail) 2-Step Verification/app-password not set, or (Brevo/Mailjet) you used
  your login password instead of the generated **SMTP key**.
- **Email lands in spam** → mark "not spam" / add the sender to contacts once.
- **Run goes red on email** → a secret is missing or `NOTIFY_EMAIL=true` was set
  before the secrets existed. Check the step log for the `[error] email send` line.
- **Wrong port** → `465` = SSL, `587`/`2525` = STARTTLS. The code auto-selects by
  port, so just set `SMTP_PORT` to match your provider's table row above.

## Security checklist

- [x] App password / SMTP key (not a main password), scoped to mail-send, revocable.
- [x] All credentials in GitHub Secrets — never committed.
- [x] SMTP over SSL (465) or STARTTLS (587) — never plaintext.
- [ ] Prefer a dedicated sender account so a leak can't touch your primary mailbox.
