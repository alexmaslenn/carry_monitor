#!/usr/bin/env python3
"""Write dashboards from a running Grafana back into grafana/dashboards/.

Provisioning is one-way: the files are mounted read-only and Grafana keeps UI
edits in its own database, so a dashboard changed in the browser exists in
exactly one place and is not in git. It survives a restart, but the next change
to the file on disk makes the provisioner overwrite it - so the edit is lost by a
routine `git pull`, silently, which is the worst way to lose work.

This closes that loop. Run it after editing in the UI, check `git diff`, commit.

  ./scripts/export-dashboards.py
  ./scripts/export-dashboards.py --url http://1.2.3.4 --folder Carry

Standard library only, so it runs on the monitoring box with nothing installed.
"""

import argparse
import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

# Assigned by Grafana's database, not by us. `id` is local to one instance and
# collides on a rebuilt box; a stale `version` can stop the provisioner applying
# the file at all. Neither belongs in a file that is meant to be portable.
VOLATILE_FIELDS = ("id", "version")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Minimal .env reader - just enough to find the admin password."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def api_get(url: str, user: str, password: str):
    request = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise SystemExit(
                f"401 from {url}\n"
                f"  Grafana rejected the credentials for user {user!r}.\n"
                f"  The password comes from GF_ADMIN_PASSWORD in .env, which is "
                f"this instance's own - it is not shared with aegis_monitor."
            ) from None
        raise SystemExit(f"{error.code} from {url}: {error.reason}") from None
    except urllib.error.URLError as error:
        raise SystemExit(
            f"Cannot reach {url}: {error.reason}\n"
            f"  Run this on the monitoring box, or pass --url with its address."
        ) from None


def existing_file_for(uid: str, out_dir: pathlib.Path) -> pathlib.Path | None:
    """Find the file that already holds this uid, so filenames stay put.

    Naming the file after the uid would be simpler, but it would orphan the
    original whenever a file and its uid disagree, leaving two provisioned copies
    of the same dashboard.
    """
    for path in sorted(out_dir.glob("*.json")):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("uid") == uid:
                return path
        except (json.JSONDecodeError, OSError):
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("GRAFANA_URL", "http://localhost"),
                        help="Grafana base URL (default: http://localhost)")
    parser.add_argument("--user", default="admin", help="Grafana user (default: admin)")
    parser.add_argument("--password", default=None,
                        help="default: GF_ADMIN_PASSWORD from the environment or .env")
    parser.add_argument("--folder", default="Carry",
                        help="only export dashboards in this folder (default: Carry)")
    parser.add_argument("--out", default=str(REPO_ROOT / "grafana" / "dashboards"),
                        help="where to write (default: grafana/dashboards)")
    args = parser.parse_args()

    password = (
        args.password
        or os.getenv("GF_ADMIN_PASSWORD")
        or read_env_file(REPO_ROOT / ".env").get("GF_ADMIN_PASSWORD", "")
    )
    if not password:
        print("No password. Set GF_ADMIN_PASSWORD in .env or pass --password.",
              file=sys.stderr)
        return 2

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip("/")

    found = api_get(f"{base}/api/search?type=dash-db&limit=500", args.user, password)
    wanted = [d for d in found if d.get("folderTitle", "") == args.folder]
    if not wanted:
        folders = sorted({d.get("folderTitle", "(General)") for d in found})
        print(f"No dashboards in folder {args.folder!r}. Found folders: {folders}",
              file=sys.stderr)
        return 1

    written = unchanged = 0
    for entry in sorted(wanted, key=lambda d: d.get("title", "")):
        uid = entry["uid"]
        dashboard = api_get(f"{base}/api/dashboards/uid/{uid}", args.user, password)["dashboard"]
        for field in VOLATILE_FIELDS:
            dashboard.pop(field, None)

        path = existing_file_for(uid, out_dir) or out_dir / f"{uid}.json"
        content = json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n"

        if path.is_file() and path.read_text(encoding="utf-8") == content:
            print(f"  unchanged  {path.name}  ({dashboard.get('title')})")
            unchanged += 1
            continue

        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"  WROTE      {path.name}  ({dashboard.get('title')})")
        written += 1

    print(f"\n{written} written, {unchanged} unchanged.")
    if written:
        print("Review with `git diff` before committing - an export also picks up "
              "incidental UI state such as the current time range.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
