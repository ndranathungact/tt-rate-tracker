# MacroDroid Webhook — Secure Setup for HNB TT Rate Alerts

This guide walks you through creating a MacroDroid webhook on your Android phone
so that, whenever the HNB USD TT rate changes, GitHub Actions pings your phone
and you get a notification. It also covers how to keep the webhook **secure**.

> **The golden rule:** your MacroDroid webhook URL is a secret. Anyone who has it
> can trigger your macro. Treat it like a password — it goes into GitHub
> **Secrets**, never into the code or a commit.

---

## 1. Install MacroDroid

1. Install **MacroDroid** from the Google Play Store.
2. Open it once and grant the permissions it asks for (notifications at minimum).

---

## 2. Create the macro with a Webhook trigger

1. Tap **Add Macro** (the **+** button).
2. **Trigger** → tap **+** → **Connectivity** → **Webhook (Url)**.
3. The trigger configuration screen appears. Fill it in like this:

   | Field on screen | What to enter | Why |
   |---|---|---|
   | **Identifier** | A non-obvious event name, e.g. `usd_rate_a7f3` | Becomes the last part of your URL. Avoid guessable names like `usd`. |
   | **Save body to string variable** | **Leave OFF / skip** | The body is only used for `POST` JSON. This project sends data in the URL (a `GET`), so there is no body to read. |
   | **Save query parameters to dictionary** | **Turn ON**, create a dictionary named **`rate`** | This is where the rate values land — see the table below. |
   | **Save headers to dictionary** (if shown) | Skip | Not needed. |

4. Your full webhook URL is now:

   ```
   https://trigger.macrodroid.com/<your-device-id>/usd_rate_a7f3
   ```

   - `<your-device-id>` is a long UUID unique to your phone — **this is the secret part**.
   - A guessable identifier plus a leaked device id is what an attacker would need,
     so keep both private.
5. **Copy the full URL** — you'll paste it into GitHub in Step 5. Save the trigger.

---

## 3. The values you receive (the `rate` dictionary)

GitHub sends the rate as query parameters on the URL, for example:

```
.../usd_rate_a7f3?buy=333.00&sell=341.50&currency=USD&updated_on=...&changed=true&message=HNB%20USD%20TT%20rate%20...
```

Because you ticked **Save query parameters to dictionary** (named `rate`), each
parameter becomes a key in that dictionary:

| Dictionary key | Example                          | Meaning                          |
|----------------|----------------------------------|----------------------------------|
| `buy`          | `333.00`                         | TT buying rate (bank buys USD)   |
| `sell`         | `341.50`                         | TT selling rate (bank sells USD) |
| `currency`     | `USD`                            | Currency code                    |
| `updated_on`   | `2026-06-25T04:35:59.000Z`       | When HNB last changed the rate   |
| `changed`      | `true`                           | Always `true` here — the event only fires on a BUY-rate change |
| `message`      | `HNB USD TT rate — Buy 333.00 …` | Ready-made human-readable string |
| `token`        | your shared secret (Step 6)      | Only present if you set one      |

To reference a key, tap the magic-text **`{x}`** button in any text field, pick
the **`rate`** dictionary, then choose the key. It inserts a token that looks
like `[v=rate[buy]]`. **Always use the picker** rather than hand-typing brackets —
it guarantees the right syntax for your MacroDroid version.

---

## 4. Add an action (the notification)

1. Still editing the macro → **Actions** → **+** → **Notification** →
   **Display Notification**.
2. **Title:** `HNB USD TT Rate`
3. **Text:** tap **`{x}`** → pick dictionary **`rate`** → key **`message`**
   (inserts `[v=rate[message]]`). That alone reads:
   *"HNB USD TT rate — Buy 333.00 / Sell 341.50 LKR"*.
   Prefer your own layout? Use:

   ```
   Buy [v=rate[buy]] / Sell [v=rate[sell]] LKR
   ```
4. (Optional) Add a second action like **Text-to-Speech** or **Vibrate** if you
   want it to be loud.
5. Leave **Constraints** empty for now (Step 6 adds an optional security one).
6. Tap the **✓** to save. Give the macro a name like **HNB USD TT Alert**.

---

## 5. Give the URL to GitHub (as a Secret — not in code)

1. On GitHub, open your repo → **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret**.
2. **Name:** `MACRODROID_WEBHOOK_URL`
   **Value:** the full URL you copied in Step 2.
3. Click **Add secret**.

That's it — the workflow already reads `secrets.MACRODROID_WEBHOOK_URL`.

---

## 6. (Recommended) Add a shared secret so only *you* can trigger it

A device id can leak (screen-share, screenshot, shoulder-surfing). Add a second
factor so a leaked URL alone isn't enough:

1. Make up a random token, e.g. `kP9w2Lz8Qy` (use a password generator).
2. On GitHub add another Actions secret:
   **Name:** `WEBHOOK_SHARED_SECRET`  **Value:** `kP9w2Lz8Qy`
   The script will then append `&token=kP9w2Lz8Qy` to every call.
3. In MacroDroid, on the macro, add a **Constraint**:
   **Constraints** → **+** → **MacroDroid Specific** → **Magic Text / Variable
   comparison** → compare `[v=rate[token]]` **equals** `kP9w2Lz8Qy`
   (insert the left side via the `{x}` picker: dictionary `rate`, key `token`).
4. Now the macro only runs when the token matches. Drop the URL alone and
   nothing fires.

> Rotate the token (and/or recreate the webhook to get a new device id) if you
> ever suspect it leaked. Update the GitHub secret(s) to match.

---

## 7. Test it

**A — straight from your browser/phone** (no GitHub needed):
Paste your webhook URL into a browser and append test values:

```
https://trigger.macrodroid.com/<device-id>/usd_rate_a7f3?buy=333.00&sell=341.50&message=Test&token=kP9w2Lz8Qy
```

Your phone should show the notification within a few seconds. (MacroDroid must
be running and allowed to run in the background — disable battery optimisation
for it: Android Settings → Apps → MacroDroid → Battery → Unrestricted.)

**B — from GitHub:**
Repo → **Actions** → **TT Rate Tracker** → **Run workflow** → tick **force** →
**Run**. The first real run also fires (the rate counts as "changed" the first
time it's seen).

---

## Security checklist

- [x] Webhook URL stored only in **GitHub Actions Secrets**, never committed.
- [x] Non-obvious `<event-name>` chosen.
- [x] Optional `WEBHOOK_SHARED_SECRET` token enforced by a macro constraint.
- [x] Always HTTPS (MacroDroid URLs are HTTPS by default — don't downgrade).
- [x] Battery optimisation disabled for MacroDroid so alerts aren't missed.
- [ ] Plan to rotate the token / recreate the webhook if it ever leaks.
