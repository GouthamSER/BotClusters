import os
import stat
import shutil
from os import path as opath, getenv
from logging import FileHandler, StreamHandler, INFO, basicConfig, error as log_error, info as log_info
from subprocess import run as srun
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

if not AUTO_UPDATE:
    log_info('Auto-update is disabled (AUTO_UPDATE is not True). Skipping update.')
elif not shutil.which("git"):
    log_error('Git executable not found in PATH. Skipping update.')
elif UPSTREAM_REPO:
    safe_remove_git()
    commands = (
        f"git init -q "
        f"&& git config --global user.email botclusters@local.host "
        f"&& git config --global user.name \"Goutham Josh\" "
        f"&& git add . "
        f"&& git commit -sm update -q "
        f"&& git remote add origin {UPSTREAM_REPO} "
        f"&& git fetch origin -q "
        f"&& git reset --hard origin/{UPSTREAM_BRANCH} -q"
    )
    update = srun(commands, shell=True)

    if update.returncode == 0:
        log_info('Successfully updated with latest commit from UPSTREAM_REPO')
    else:
        log_error('Something went wrong while updating, check UPSTREAM_REPO if valid or not!')
