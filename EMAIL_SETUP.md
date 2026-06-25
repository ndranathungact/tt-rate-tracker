# Email Alerts via GitHub Actions (Gmail SMTP)

The tracker can send a **formatted HTML email** (rate card + next-day forecast)
in addition to — or instead of — the MacroDroid push. It's sent directly from
`scrape.py` using Python's standard-library `smtplib` (no extra dependencies, no
third-party email service), authenticating to **Gmail SMTP** with an **App
Password**. Free, and well under Gmail's ~500-emails/day limit (we send ~1/day).

Both channels are independent toggles:

| Toggle (workflow env) | Default | Effect |
|---|---|---|
| `NOTIFY_MACRODROID` | `true` | MacroDroid push on buy-change |
| `NOTIFY_EMAIL` | `false` | HTML email on buy-change |

---

## Step 1 — Create a Gmail App Password

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

## What the email contains

- HNB USD buy / sell, the spread, and the **day-over-day change** (▲/▼ vs the
  previous business-day close).
- The **next-business-day forecast**: signal (`BUY`/`SELL`/`HOLD`/`WATCH`),
  predicted buy + uncertainty band, the chosen model, confidence, and the live
  accuracy (rolling MAE + directional hit-rate). During early data collection it
  shows `WARMUP`.

## Troubleshooting

- **`535 Username and Password not accepted`** → you used the normal password, or
  left spaces in the app password, or 2-Step Verification isn't on.
- **Email lands in spam** → mark "not spam" / add the sender to contacts once.
- **Run goes red on email** → a secret is missing or `NOTIFY_EMAIL=true` was set
  before the secrets existed. Check the step log for the `[error] email send` line.

## Security checklist

- [x] App password (not the main password), scoped to mail-send, revocable.
- [x] All credentials in GitHub Secrets — never committed.
- [x] SMTP over SSL (port 465) to `smtp.gmail.com`.
- [ ] Prefer a dedicated sender Gmail so a leak can't touch your primary account.
