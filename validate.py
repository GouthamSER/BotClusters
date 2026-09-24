"""
Run: python validate.py
Checks every CLUSTER_xx env var against config.json slots.
Catches bad JSON BEFORE you deploy — no need to wait for Koyeb logs.
"""
import os
import json
from dotenv import load_dotenv

load_dotenv('cluster.env', override=True)

REQUIRED_FIELDS = ["name", "git_url", "branch", "run_command"]  # positions 0-3

def check(name, raw):
    if raw is None:
        return "SKIP", "not set"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return "FAIL", f"bad JSON — {e.msg} at line {e.lineno} col {e.colno}"

    if not isinstance(data, list):
        return "FAIL", "must be a JSON array, e.g. [\"name\", \"url\", \"branch\", \"file.py\", {...}]"
    if len(data) < 4:
        return "FAIL", f"needs at least 4 items (name, git_url, branch, run_command), got {len(data)}"
    if len(data) >= 5 and not isinstance(data[4], dict):
        return "FAIL", "5th item (env vars) must be a {} object"
    if not str(data[1]).startswith(("http://", "https://", "git@")):
        return "FAIL", f"git_url looks wrong: {data[1]!r}"

    return "OK", f"{data[0]}  ->  {data[1]}"


def main():
    with open("config.json") as f:
        config = json.load(f)

    print("Checking cluster env vars...\n")
    ok = fail = skip = 0
    for cluster in config.get("clusters", []):
        name = cluster["name"]
        raw = os.getenv(name)
        status, detail = check(name, raw)
        icon = {"OK": "✅", "FAIL": "❌", "SKIP": "⚪"}[status]
        print(f"{icon} {name}: {detail}")
        if status == "OK":
            ok += 1
        elif status == "FAIL":
            fail += 1
        else:
            skip += 1

    print(f"\n{ok} ok, {fail} broken, {skip} unused (of {len(config.get('clusters', []))} slots)")
    if fail:
        print("\nFix the ❌ lines above before deploying — those bots will silently not start.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
