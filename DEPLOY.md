# Running the Discord alerts on a home server

Every weekday morning this job pulls the previous business day's SEC Form 4 filings (what
company insiders report when they trade their own company's stock). It posts the purchases
that pass your rules to a Discord channel, one post per alert, each linking the filing on
sec.gov. A night's run takes a few minutes. It only makes outbound HTTPS requests (to
www.sec.gov and discord.com) and opens no ports.

A post looks like this (real data, October 2026):

> **GME · GameStop Corp. — cluster buy**
> **2 insiders · $40,113,538 · 2026-10-01 to 2026-10-13**
> Cohen Ryan (Director, President, CEO and Chairman): 1,544,130 sh @ $25.81 = $39,858,998, +4% to holding
> Turner Nat (Director): 10,462 sh @ $24.33 = $254,540
> SEC filing: [1](https://www.sec.gov/Archives/edgar/data/1326380/000092189526002784/0000921895-26-002784-index.htm)
> *Cluster buy · Not investment advice.*

Paths below assume the project lives in `/opt/insider-tracker` and runs as a user called
`insider`. If you change either, change it in `deploy/insider-alerts.service` too.

## What you need

- Linux with systemd (Debian 12, Ubuntu 22.04+ and Raspberry Pi OS bookworm all work), or any
  Unix with cron.
- Python 3.11 or newer (check with `python3 --version`), plus git. On Debian/Ubuntu:
  `sudo apt install git python3-venv`.
- About 250 MB of disk a year. The database grows by roughly 20 MB a month, and nothing needs pruning.
- A Discord server where you can create webhooks (Manage Webhooks permission).

## 1. Install

```sh
sudo useradd --system --home-dir /opt/insider-tracker --shell /usr/sbin/nologin insider
sudo git clone https://github.com/root-house-code/insider-tracker.git /opt/insider-tracker
cd /opt/insider-tracker
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo mkdir -p data && sudo chown insider:insider data
.venv/bin/python tests/test_parsers.py                # offline, no network: a PASS line per test, no traceback
```

The code stays owned by root, so the job can read it but never change it. It can only write
to `data/`.

## 2. Your SEC identity

The SEC requires every automated request to say who is making it: a name and an email in the
User-Agent header. This is **not** an API key or an account; there's nothing to sign up for.
The SEC uses it to contact, or block, whoever runs a misbehaving tool. Use your own name and
email, never someone else's. The SEC's fair-access limit is 10
requests a second; the tool waits 0.12 s between requests, so it stays under that.

## 3. Create the Discord webhook

1. Make a channel for the alerts, e.g. `#insider-buys`. Making it read-only for members is
   a good idea, so only the bot posts there.
2. Channel settings → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.
3. Optional: do the same in a private channel for error messages
   (`DISCORD_ADMIN_WEBHOOK_URL` below). Without one, errors go to the alerts channel.

The webhook URL works like a password: anyone who has it can post to that channel (they can't
read it). Keep it only in `.env`.

## 4. Settings

```sh
cd /opt/insider-tracker
sudo cp deploy/env.example .env
sudo nano .env                                # your SEC_USER_AGENT and DISCORD_WEBHOOK_URL
sudo chown insider:insider .env && sudo chmod 600 .env
sudo cp alerts.example.toml alerts.toml       # the rules; tuned in step 6
```

## 5. Seed three weeks of filings

Cluster rules look back 14 days, so the database needs some history before the first run:

```sh
sudo -u insider sh deploy/run.sh form4 --days 21 --all
```

This takes about 30-60 minutes. A business day brings 300-1,500 Form 4 filings, and they are
fetched one at a time within the SEC's rate limit. It's safe to interrupt; the next run picks
up where this one stopped.

`deploy/run.sh` runs any command the way the nightly job does: from the project folder, with
`.env` loaded. `sudo -u insider sh deploy/run.sh --help` lists the commands.

## 6. Tune the rules before going live

```sh
sudo -u insider sh deploy/run.sh alerts --backtest 20   # what the rules would have posted, day by day
sudo -u insider sh deploy/run.sh alerts --dry-run       # what would post right now
```

Neither command posts or records anything. Edit `alerts.toml`, then run them again until the
volume feels right. Every setting is explained in the file. A mistake in the file stops the
run with a message naming the bad key; it never posts with a half-read file.

For a sense of scale, here is how the settings change the volume. These counts are a backtest over the 20 days
to 2026-10-14, every company:

| Rule | Setting | Posts in 20 days | About per week |
|---|---|---:|---:|
| Cluster buy | as shipped: 2+ insiders, $25k+ each, $250k+ total | 43 | 15 |
| Cluster buy | 3+ insiders | 15 | 5 |
| Cluster buy | $1M+ total | 31 | 11 |
| Cluster buy | $100k+ each, $1M+ total | 26 | 9 |
| Cluster buy | as shipped, plus `cooldown_days = 7` | 18 | 6 |
| Large buy | as shipped: officer or director, $500k+ in one filing | 69 | 24 |
| Large buy | $1M+ | 49 | 17 |
| Large buy | $2M+ | 28 | 10 |
| Large buy | $1M+, `roles = ["officer"]` | 28 | 10 |

Things worth knowing when you tune:

- **What never posts:** sales, option exercises, grants, trades under pre-scheduled 10b5-1
  plans, and filings that claim implausible amounts. One real filing put a $2.27M total in
  the per-share price field, a "$10 trillion" buy. The dry run lists what it skipped and why.
- **Repeat buyers:** someone buying every day is one story, not five posts. `cooldown_days`
  allows at most one post per company (cluster) or per insider and company (large buy) in that
  many days.
- **Recurring programs:** some companies' directors all buy the same dollar amount on the same
  day each year as part of their pay, e.g. HEICO's eight directors at $175,142 each. These look
  like a cluster but aren't one. Add them to `exclude_tickers`.
- **Amendments** (Form 4/A) that restate a trade already posted don't post again. A cluster
  re-posts only when a new trade joins it, marked "(update)", with the new insiders flagged.
- **Renaming a rule** makes it forget what it already posted.
- **Several channels:** give a rule `webhook_env = "DISCORD_WEBHOOK_BIG_BUYS"` and add that
  variable to `.env`.

## 7. First live run

```sh
sudo -u insider sh deploy/run.sh alerts
```

Only filings from the last 4 days (`max_age_days`) are eligible, so this posts a handful of
recent alerts rather than three weeks of backlog. After this, each run posts only what's new.

## 8. Schedule it

With systemd:

```sh
sudo cp deploy/insider-alerts.service deploy/insider-alerts.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/insider-alerts.service   # no output = fine
systemd-analyze calendar "Tue..Sat *-*-* 06:00:00 America/New_York"     # next run, in your time zone
sudo systemctl daemon-reload
sudo systemctl enable --now insider-alerts.timer
sudo systemctl start insider-alerts.service       # one run now, to check it works under systemd
journalctl -u insider-alerts -n 30
```

The timer fires Tuesday to Saturday at 6:00 New York time, whatever time zone the server uses.
By then the SEC has published the previous business day's index, so each morning's posts cover
yesterday's filings. If the server was off at that time, the job runs as soon as it's back up.

With cron instead, use `sudo crontab -u insider -e`. Cron uses the server's time zone, so
adjust the hour to 6:00 New York time:

```cron
0 6 * * 2-6  sh /opt/insider-tracker/deploy/run.sh alerts --ingest-days 4 >> /opt/insider-tracker/data/alerts.log 2>&1
```

## Day to day

- **A normal night** logs `Form 4: stored N transactions` and then
  `Alerts: X matched, posted Y`. Check with `journalctl -u insider-alerts --since yesterday`.
- **When something breaks**, a ⚠️ message goes to the admin webhook, or to the alerts channel
  if you didn't set one. The cases:
  - the Form 4 download failed;
  - a post was rejected (it retries next run; nothing is marked posted until Discord accepts it);
  - no new filings have arrived for more than 3 weekdays.

  The last one usually means the SEC is refusing the server. The SEC answers 403 both for "not
  published yet" and for "blocked", so a single quiet night is normal, but several are not.
- **Changing rules:** edit `alerts.toml`. It takes effect on the next run; backtest first.
- **Updating the code:**

  ```sh
  cd /opt/insider-tracker && sudo git pull && sudo .venv/bin/pip install -r requirements.txt
  .venv/bin/python tests/test_parsers.py
  ```

- **Backups:** `data/insider_tracker.db` is also the bot's memory of what it has posted. If you
  lose it, re-seed (step 5). The next run then re-posts up to 4 days of alerts, and nothing older.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `status=226/NAMESPACE` in `systemctl status` | `data/` doesn't exist: `sudo mkdir -p /opt/insider-tracker/data && sudo chown insider:insider /opt/insider-tracker/data` |
| `Set SEC_USER_AGENT to something like...` | `.env` is missing, unreadable by `insider`, or lacks an email |
| `alert rules: ...` | A mistake in `alerts.toml`; the message names it |
| `Discord refused the post (HTTP 401 ...)` or `(HTTP 404 ...)` | The webhook was deleted or mistyped: make a new one and update `.env` |
| 403 errors from sec.gov in the log | The SEC is refusing requests: check `SEC_USER_AGENT` is a real name and email. A block for going over the rate limit usually lifts within about 10 minutes |
| A post you expected didn't appear | `alerts --dry-run` shows what passes now. The filing may be over 4 days old, or held back by `cooldown_days` |

## Security

- **The webhook URL is a secret.** It lives only in `.env`, which is mode 600 and excluded from
  git. The tool never prints it, not even in error messages. If it leaks, delete the webhook in
  Discord and make a new one; nothing else needs changing.
- **Text from filings can't hijack the channel.** Filers write the names and titles in each
  post, so every post disables @mentions (no `@everyone` pings) and escapes Discord markdown
  (no disguised links).
- **Small footprint.** The job runs as an unprivileged user. Under systemd it can write only
  `data/`, with no new privileges, a private `/tmp` and a read-only system. It never listens on
  a port.
- **Keep the browser UI private.** The project also has a local web UI (`ui`). It refuses to
  listen on anything but localhost; keep it that way. To use it from another machine, tunnel:
  `ssh -L 8765:127.0.0.1:8765 you@server`, then on the server run
  `sudo -u insider sh deploy/run.sh ui --no-browser` and browse to http://127.0.0.1:8765.

## Legal and courtesy

- **Form 4 filings are public records**, free to repost. Every post links its source on sec.gov.
- **Not investment advice.** Keep the footer. If you ever charge for access, or tailor alerts to
  particular people's holdings, talk to a lawyer first. Selling investment signals can bring
  securities-adviser rules into play.
- **The bot posts Form 4 data only.** The project can also collect members of Congress's trade
  disclosures, but the law (5 U.S.C. §13107) bars using those for commercial purposes, so keep
  them out of anything paid. The UI's price charts aren't in the posts either: they come from
  Yahoo Finance, whose terms don't allow redistributing the data.
- **Be a good SEC citizen.** Use your real identity, don't lower the request spacing in
  `insider_tracker/config.py`, and don't run several copies against the SEC at once.
