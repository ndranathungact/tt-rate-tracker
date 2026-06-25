# Reliable Triggering with cron-job.org

GitHub's built-in `schedule` (cron) is best-effort and unreliable — especially
for new/low-activity repos, where the first runs are delayed by hours or dropped
entirely. This guide sets up a **free, punctual** trigger: **cron-job.org** calls
GitHub's API every 15 minutes to run the workflow via `workflow_dispatch`.

```
cron-job.org (precise 15-min clock)
      │  POST .../actions/workflows/tt-rate.yml/dispatches   (Bearer token)
      ▼
GitHub Actions runs the workflow  ──►  scrape.py  ──►  MacroDroid webhook
```

Nothing in the repo changes — the workflow already accepts `workflow_dispatch`.

---

## Step 1 — Create a fine-grained GitHub token

This token lets cron-job.org start the workflow. We scope it to **one repo,
Actions only**, so a leak is low-impact (an attacker could at most trigger this
one workflow, which just posts the rate to your webhook).

1. GitHub → click your avatar → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Fill in:
   - **Token name:** `tt-rate-cron`
   - **Expiration:** 90 days (or custom — you'll need to rotate it when it expires).
   - **Resource owner:** `ndranathungact`
   - **Repository access:** **Only select repositories** → choose
     **`tt-rate-tracker`**.
   - **Permissions** → **Repository permissions** → find **Actions** → set to
     **Read and write**. (Metadata: Read-only is added automatically — leave it.)
     Everything else: **No access**.
3. **Generate token** and **copy it now** (`github_pat_…`). You won't see it again.

---

## Step 2 — Test the token from your machine (recommended)

Confirm the token works *before* wiring up cron-job.org. Run this (paste your
token in place of `YOUR_TOKEN`):

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "User-Agent: tt-rate-cron" \
  https://api.github.com/repos/ndranathungact/tt-rate-tracker/actions/workflows/tt-rate.yml/dispatches \
  -d '{"ref":"main"}'
```

- **Success looks like:** `HTTP/2 204` (no body). A run appears under the repo's
  **Actions** tab within a few seconds.
- `401` → token wrong/expired. `403` → missing the **Actions: Read and write**
  permission (or missing `User-Agent`). `404` → repo/workflow name or
  resource-owner wrong, or the token can't see the repo.

---

## Step 3 — Create the cron-job.org job

1. Sign up (free) at **https://cron-job.org** and verify your email.
2. **Create cronjob** and set:

   **Common**
   - **Title:** `TT Rate Trigger`
   - **URL:**
     ```
     https://api.github.com/repos/ndranathungact/tt-rate-tracker/actions/workflows/tt-rate.yml/dispatches
     ```
   - **Schedule:** the rate changes once per business day in the morning, so poll
     **every 20 minutes, 03:00–13:59 UTC, Monday–Friday** (≈08:30–19:30 SL time).
     In cron-job.org's schedule grid set: **minutes** `0,20,40`; **hours** `3–13`;
     **days of week** `Mon–Fri`. (Use UTC — cron-job.org lets you pick the
     timezone; pick UTC to match these numbers.) No need to poll nights/weekends.

   **Advanced → Request**
   - **Request method:** `POST`
   - **Headers** (add each one):
     | Key | Value |
     |---|---|
     | `Accept` | `application/vnd.github+json` |
     | `Authorization` | `Bearer YOUR_TOKEN` |
     | `X-GitHub-Api-Version` | `2022-11-28` |
     | `User-Agent` | `tt-rate-cron` |
     | `Content-Type` | `application/json` |
   - **Request body:**
     ```json
     {"ref":"main"}
     ```
3. (Recommended) Turn on **notifications when the job fails**, so you hear about
   it if the token expires.
4. **Save.** GitHub returns `204` on success, which cron-job.org treats as a
   pass (green).

---

## Step 4 — Verify it's running on schedule

After 15–30 minutes you should see **`workflow_dispatch`** runs appearing
regularly:

```bash
gh run list --repo ndranathungact/tt-rate-tracker --limit 10
```

You'll see a new run roughly every 15 minutes with event `workflow_dispatch`
(that's cron-job.org). Remember: most runs post **nothing** to your phone — they
only alert when HNB's **buy** rate actually changes. A green run with no
notification is the system working correctly.

---

## Notes & housekeeping

- **The GitHub `schedule:` block still exists** in the workflow as an unreliable
  fallback. It's harmless (the change-detection logic prevents duplicate alerts)
  and free on a public repo. If you'd rather have cron-job.org be the *only*
  trigger, delete the `schedule:` lines from
  [.github/workflows/tt-rate.yml](.github/workflows/tt-rate.yml).
- **Token rotation:** when the token nears expiry, generate a new one (Step 1)
  and update the `Authorization` header in cron-job.org. Revoke the old one.
- **If it ever leaks:** revoke the token on GitHub immediately
  (Settings → Developer settings → the token → Revoke). Worst case before
  revocation is someone triggering this one workflow — no repo write, no secrets.

## Security checklist

- [x] Token is **fine-grained**, **single-repo**, **Actions-only**.
- [x] Token has a finite **expiration** with a plan to rotate.
- [x] Token lives only in cron-job.org's header field — never committed to the repo.
- [x] All calls are HTTPS to `api.github.com`.
- [ ] Failure notifications enabled in cron-job.org (so token expiry is noticed).
