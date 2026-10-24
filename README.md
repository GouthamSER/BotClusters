## 🎓 ***BotClusters v8.1***

> **Maintainer:** [Goutham Josh](https://github.com/GouthamJosh)  
> **Original Developer / Contributor:** [MysteryDemon](https://github.com/mysterydemon)

Have you encountered the problem where you have to host resource-efficient Telegram Bots for free and you can only host a bot per account, but you wanted to host multiple bots in one instance? Say no more...

**BotClusters** lets you run multiple bots in the same instance. Built with process isolation, supervisord auto-recovery, dynamic virtual environments, and a live web dashboard.

---

## 🔰 ***Repo Features***
- 🤖 **Web GUI Bot Manager**: *Add, configure, edit, clone, and delete bot clusters directly from the dashboard without editing config files*
- 📊 **Real-Time Resource Metrics**: *Live gauges for CPU load %, host RAM usage (MB / %), and Disk space*
- ⚡ **Per-Process Telemetry**: *Track memory consumption (RSS MB), CPU %, uptime, and PID for each bot individually*
- 🎛️ **Batch Cluster Operations**: *One-click "Start All", "Stop All", and "Restart All" cluster management*
- 💻 **Integrated Bot Console / Terminal**: *Execute diagnostic shell commands (`pip list`, `git status`, `python -c ...`) inside any bot directory*
- ⬇️ **One-Click Git Pull & Reload**: *Pull latest commits from bot repositories on demand without full server rebuilds*
- 📢 **Telegram Crash Alerts**: *Instant Telegram notifications if a bot repeatedly crashes or enters auto-pause*
- 💾 **Cluster Backup & Restore**: *Export all bot definitions and environment settings to JSON and restore anytime*
- 📜 **Advanced Log Streaming**: *Real-time SSE log stream with process filtering, STDOUT/STDERR isolation, live search, and file export*
- 🔄 **Auto Updates**: *Optional automatic updates through GitHub on restart*
- 🛡️ **Reliable Process Management**: *Powered by supervisord with automated process supervision and smart auto-pause recovery*
- 🔐 **Environment Control**: *Configure key-value environment variables per bot directly in the visual editor*
- 🎮 **Custom Execution**: *Configure custom entry scripts (`bot.py`, `main.py`, `app.py`, `start.sh`)*
- 🔒 **Private Repo Support**: *Clone and run bots from private repositories using GitHub tokens*
- 🎛️ **Cloud & Cross-Platform Integration**: *Ready for Render, Koyeb, Heroku, Docker, Codespaces, and standalone local environments*
- 🐍 **Multi-Python Support**: *Supports specific Python versions (`3.10` to `3.13`, as installed in the Docker image)*
- 🟩 **Node.js Support**: *Run Node.js bots (`node:20`, `node:22`, `node:latest`) with automatic `npm install`*

---

## 🚀 ***Quick Start***
1. **Fork and Star this repository**
2. **Configure your bots** in `cluster.env`
3. **Deploy to your preferred platform** using the buttons below

---

## #️⃣ Sample `Var.CLUSTERS`

| Config | Description | Required |
|----------|-------------|:---:|
| `botname` | Unique name for your bot | ✅ |
| `git_url` | Git repository URL | ✅ |
| `branch` | Repository branch name | ✅ |
| `run_command` | Bot execution command (`bot.py`, `main.py`, `start.sh`) | ✅ |
| `env` | Environment variables dictionary | ❌ |
| `runtime_version` | Python version (`3.10`–`3.13`) or Node.js (`node:20`, `node:22`, `node:latest`) | ❌ |
| `cron` | Auto-restart cron schedule, e.g. `0 */6 * * *` (7th item; use `null` as 6th item to skip the version; or set it in the bot form) | ❌ |

---

## ✅ Supported Python Versions

`python3.10` &bull; `python3.11` &bull; `python3.12` &bull; `python3.13`

---

## 🛠️ ***Setup Guide***

* **Format:**
```json
["botname", "git_url", "branch", "run_command", {ENV_DICT}, "runtime_version", "cron"]
```

* **For Public Repositories:**
```json
["bot01", "https://github.com/exampleuser/bot.git", "main", "bot.py", {"PORT": "8787"}]
```

* **For Private Repositories:**
```json
["bot02", "https://<your_username>:<your_github_token>@github.com/<your_username>/privatebot.git", "main", "main.py", {"PORT": "6060"}]
```

* **For Custom Python Version:**
```json
["bot03", "https://github.com/exampleuser/legacybot.git", "main", "main.py", {"PORT": "6060"}, "3.11"]
```

* **For Node.js Bots:**
```json
["bot04", "https://github.com/exampleuser/nodebot.git", "main", "index.js", {"BOT_TOKEN": "123:ABC"}, "node:22"]
```

* **For Module Entry Points (`python -m`):**
```json
["bot05", "https://github.com/exampleuser/pkgbot.git", "main", "-m mypkg.main", {}]
```

---

## ⚡ ***Deploy***

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new)

[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&builder=dockerfile&repository=github.com/GouthamJosh/BotClusters&branch=main&name=botclusters&ports=5000;http;/&env[CLUSTER_01]=)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://dashboard.heroku.com/new)

---

## 💻 ***Testing in GitHub Codespaces***

1. **Launch a Codespace** on this repository (**Code > Codespaces > Create codespace on main**).
2. **Configure your bot in `cluster.env`**:
   ```bash
   cp cluster.env.sample cluster.env
   nano cluster.env
   ```
3. **Build and test using Docker**:
   ```bash
   docker build -t botclusters .
   docker run -p 5000:5000 --env-file cluster.env botclusters
   ```
4. **Open Dashboard**: Codespaces will automatically detect port `5000` and prompt you to **Open in Browser**. Log in with `admin` / `password123`.

---

## 🔧 ***Latest fixes***
* Pull, webhook, auto-pull and rollback **no longer start a bot you stopped** - they restart only bots that are RUNNING.
* Dependencies now **reinstall when `requirements.txt` / `package.json` changes**, also at boot after an auto-update (hash stamp inside `venv/` or `node_modules/`).
* Node.js bots: pull / rollback re-run `npm install` when `package.json` changes.
* Dotted module run commands (`pkg.sub.main`) are no longer cut to `pkg`.
* Settings save is all-or-nothing: an invalid value no longer leaves earlier fields half-saved.
* Graceful shutdown no longer crashes on a changing process list; stale *Installing…* badges clear at boot immediately.

## 🆕 ***What changed in 8.1 (bug-fix release)***
* Live dashboard updates work again (`broadcast=` socket error fixed).
* Bots no longer vanish / show STOPPED when one bot is down (`supervisorctl status` exit code 3).
* `worker.py` now uses the same names/paths as the dashboard (no random name prefix), sets up ALL `CLUSTER_xx` (not only 30), and one bad repo no longer blocks the rest.
* `update.py` no longer deletes/overwrites `cluster.env`.
* Random per-install session secret, login lockout, constant-time password check.
* Clone / pip install runs in background with an *Installing…* / *Setup failed* card state.
* Per-bot cron restart now really works; global schedule only restarts bots that are running.
* Crash alerts (Telegram) fire even with no browser open; real CPU %; container-aware RAM gauge.
* Reserved bot names (`app`, `bots`, …) blocked - they could wipe dashboard files.

## ✨ ***New in 8.1 (features)***
* 📈 **RAM sparkline + restart counter** on every bot card (spot crash loops / memory leaks).
* ⏪ **Versions**: last commits per bot, one-click **rollback** (and roll forward).
* 🔗 **GitHub webhook auto-deploy**: copy the per-bot URL from *Versions* into GitHub → Webhooks (push). Push = pull + restart.
* ⬇ **Auto-pull schedule** (Settings → minutes): restarts a running bot only when its repo has new commits.
* 👁 **Secret-masked env editor**: tokens/keys/passwords hidden until you click the eye.
* ⧉ **Clone bot**: copy a bot's settings into a new one.

## 📝 ***Notes***
* Bots can be Python, Node.js, or bash scripts.
* It is not compulsory to set a Python version; only set it if your bot specifically requires an older/newer interpreter.
* Keep your tokens and sensitive information secure in `cluster.env`. **Never commit a real `cluster.env`** - add it to `.gitignore`.
* If your bot needs additional system or pip packages, define them in `install.sh`.
* **GUI login details (configurable via `ADMIN_USERNAME` and `ADMIN_PASSWORD`):**
  * Default `admin` / `password123` - **change it in Settings right after first login** (dashboard warns you).

---

## 👥 ***Credits & Contributors***

* **Maintainer:** [Goutham Josh](https://github.com/GouthamJosh)
* **Original Developer:** [MysteryDemon](https://github.com/mysterydemon)
* **Upstream Inspiration:** [MultiBots](https://github.com/bipinkrish/MultiBots)

---

## 🤝 ***Contributing***
Contributions are welcome! Please feel free to submit a Pull Request.
