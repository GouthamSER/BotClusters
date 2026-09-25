## 🎓 ***BotClusters v8.0***

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
- 🐍 **Multi-Python Support**: *Supports specific Python versions (`3.8` to `3.13`)*

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
| `python_version` | Custom Python Version (e.g. `3.9`, `3.10`, `3.11`) | ❌ |
| `cron` | Restart cron schedule | ❌ |

---

## ✅ Supported Python Versions

`python3.8` &bull; `python3.9` &bull; `python3.10` &bull; `python3.11` &bull; `python3.12` &bull; `python3.13`

---

## 🛠️ ***Setup Guide***

* **Format:**
```json
["botname", "git_url", "branch", "run_command", {ENV_DICT}, "python_version"]
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
["bot03", "https://github.com/exampleuser/legacybot.git", "main", "main.py", {"PORT": "6060"}, "3.9"]
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

## 📝 ***Notes***
* Ensure all your bots are compatible with Python (or bash).
* It is not compulsory to set a Python version; only set it if your bot specifically requires an older/newer interpreter.
* Keep your tokens and sensitive information secure in `cluster.env`.
* If your bot needs additional system or pip packages, define them in `install.sh`.
* **GUI login details (configurable via `ADMIN_USERNAME` and `ADMIN_PASSWORD`):**
  * `Username`: `admin`
  * `Password`: `password123`

---

## 👥 ***Credits & Contributors***

* **Maintainer:** [Goutham Josh](https://github.com/GouthamJosh)
* **Original Developer:** [MysteryDemon](https://github.com/mysterydemon)
* **Upstream Inspiration:** [MultiBots](https://github.com/bipinkrish/MultiBots)

---

## 🤝 ***Contributing***
Contributions are welcome! Please feel free to submit a Pull Request.
