"""
wraith.updater — dead-simple self-update via GitHub Releases.

TARGET below is "github:OWNER/REPO". The updater reads that repo's LATEST
release (tag = version, the .exe asset = installer, release notes = changelog).
To ship an update you just publish a GitHub release vX.Y.Z and attach
Wraith-Setup-X.Y.Z.exe — friends click "Check for Updates" and get it.

Also accepts a plain JSON-manifest URL ({version,url,notes}) for non-GitHub
hosting. Either form can be overridden WITHOUT rebuilding by dropping an
`update_url.txt` next to Wraith.exe (contents = the github:… or the URL).
"""

from __future__ import annotations

import json
import ssl
import subprocess
import urllib.request

from . import __version__
from .runtime import app_dir, NO_WINDOW

# EDIT THIS to your repo (or ship update_url.txt). Form: "github:owner/repo".
DEFAULT_TARGET = "github:jtiberius9-spec/Wraith"


def target() -> str:
    f = app_dir() / "update_url.txt"
    try:
        if f.is_file():
            u = f.read_text(encoding="utf-8").strip()
            if u:
                return u
    except OSError:
        pass
    return DEFAULT_TARGET


def _ver(v) -> tuple:
    out = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = ""
        for c in part:
            if c.isdigit():
                digits += c
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_newer(latest: str, current: str = __version__) -> bool:
    return _ver(latest) > _ver(current)


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": "Wraith-Updater"})
    return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())


def check(timeout: float = 8.0) -> dict | None:
    """Return {version,url,notes} if a NEWER release is available, else None."""
    t = target()
    try:
        if t.startswith("github:"):
            return _check_github(t.split(":", 1)[1].strip("/"), timeout)
        with _open(t, timeout) as r:                      # plain JSON manifest
            data = json.loads(r.read().decode("utf-8"))
        if data.get("version") and data.get("url") and is_newer(data["version"]):
            return data
    except Exception:
        return None
    return None


def _check_github(repo: str, timeout: float) -> dict | None:
    with _open(f"https://api.github.com/repos/{repo}/releases/latest", timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data.get("tag_name") or data.get("name") or ""
    if not tag or not is_newer(tag):
        return None
    url = next((a["browser_download_url"] for a in data.get("assets", [])
                if a.get("name", "").lower().endswith(".exe")), None)
    if not url:
        return None
    return {"version": tag, "url": url, "notes": (data.get("body") or "").strip()[:300]}


def download(url: str, dest, progress=None, timeout: float = 60.0):
    """Download url -> dest, calling progress(fraction 0..1) as it goes."""
    with _open(url, timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(got / total)
    return dest


def run_installer(path) -> None:
    """Launch the downloaded installer (it upgrades in place). Caller should
    then close Wraith so the installer can replace the files."""
    subprocess.Popen([str(path)], creationflags=NO_WINDOW)
