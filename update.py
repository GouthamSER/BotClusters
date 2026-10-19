import os
import stat
import shutil
import subprocess
from os import path as opath, getenv
from logging import FileHandler, StreamHandler, INFO, basicConfig, error as log_error, info as log_info
from dotenv import load_dotenv

load_dotenv('cluster.env', override=True)

basicConfig(
    format="[%(asctime)s] [%(name)s | %(levelname)s] - %(message)s [%(filename)s:%(lineno)d]",
    datefmt="%m/%d/%Y, %H:%M:%S %p",
    handlers=[FileHandler('log.txt', mode='w', encoding='utf-8'), StreamHandler()],
    level=INFO
)

AUTO_UPDATE = getenv("AUTO_UPDATE", "false").strip().lower() in ("true", "1", "yes")
UPSTREAM_REPO = getenv("UPSTREAM_REPO", "https://github.com/MysteryDemon/BotClusters")
UPSTREAM_BRANCH = getenv("UPSTREAM_BRANCH", "main")

# Files that hold the user's own data. An update must never lose or overwrite them.
PRESERVE = ("cluster.env", ".secret_key", "worker_state.json")


def remove_readonly(func, path, exc_info):
    """Clear the read-only bit and retry the removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def safe_remove_git():
    if opath.exists('.git'):
        try:
            shutil.rmtree('.git', onerror=remove_readonly)
        except Exception as e:
            log_error(f"Failed to remove .git directory: {e}")


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def update():
    # Back up user data first: `git reset --hard` overwrites any file that also exists upstream
    # (e.g. a committed cluster.env would replace all bots/settings saved from the dashboard).
    backup = {}
    for name in PRESERVE:
        if opath.exists(name):
            with open(name, "rb") as f:
                backup[name] = f.read()

    safe_remove_git()
    try:
        # No `git add .` / commit: untracked files (bots, logs, cluster.env) are left alone.
        for step in (("git", "init", "-q"),
                     ("git", "remote", "add", "origin", UPSTREAM_REPO),
                     ("git", "fetch", "origin", UPSTREAM_BRANCH, "-q"),
                     ("git", "reset", "--hard", f"origin/{UPSTREAM_BRANCH}", "-q")):
            res = run(*step)
            if res.returncode != 0:
                err = (res.stderr or res.stdout).replace(UPSTREAM_REPO, "<UPSTREAM_REPO>")
                raise RuntimeError(f"{step[1]} failed: {err.strip()[-300:]}")
        return True
    except Exception as e:
        log_error(f"Update error: {e}")
        return False
    finally:
        for name, data in backup.items():
            with open(name, "wb") as f:
                f.write(data)


if not AUTO_UPDATE:
    log_info('Auto-update is disabled (AUTO_UPDATE is not True). Skipping update.')
elif not shutil.which("git"):
    log_error('Git executable not found in PATH. Skipping update.')
elif UPSTREAM_REPO:
    if update():
        log_info('Successfully updated with latest commit from UPSTREAM_REPO')
    else:
        log_error('Something went wrong while updating, check UPSTREAM_REPO / UPSTREAM_BRANCH!')
