"""
Instagram profile lookup via Instaloader (username only).

CLI equivalent::

    instaloader --login YOUR_ACCOUNT target_username

creates ``./target_username/`` in the **current working directory**. In the web app we
``chdir`` into a fixed base directory (see ``download_profile_to_folder``) under a lock,
so each run produces ``<INSTALOADER_DOWNLOAD_DIR>/<username>/`` with images and ``.json``
metadata (captions), matching the CLI layout without polluting the server process cwd.

Authentication (pick one):

- **2FA-friendly:** Log in once in a terminal with ``instaloader --login YOUR_USERNAME`` (enter
  password and 2FA when prompted). Instaloader saves a session file. Then set
  ``INSTAGRAM_LOGIN`` to that username and either leave ``INSTAGRAM_PASSWORD`` empty (default
  session path) or set ``INSTAGRAM_SESSION_FILE`` to the session file path. Use
  ``load_session_from_file`` — no password/2FA in the web app.
- **Password only (often breaks with 2FA):** ``INSTAGRAM_LOGIN`` + ``INSTAGRAM_PASSWORD``.

Post limit: environment variable ``INSTAGRAM_MAX_POSTS`` (integer; empty = no cap).
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import instaloader

# Instaloader changes process cwd during downloads; guard concurrent Flask requests.
_download_cwd_lock = threading.Lock()


@dataclass
class InstagramScanResult:
    username: str
    full_name: str
    biography: str
    followers: int
    following: int
    media_count: int
    is_verified: bool
    external_url: str | None
    recent_captions: list[str]


_SKIP_PATH = frozenset(
    {
        "p",
        "reel",
        "reels",
        "stories",
        "explore",
        "accounts",
        "direct",
        "tv",
    }
)


def normalize_instagram_username(raw: str) -> str:
    """Accept @handle, handle, or a full instagram.com URL; return bare username."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "instagram.com" in s.lower():
        if not s.startswith("http"):
            s = "https://" + s
        path = urlparse(s).path.strip("/")
        parts = [p.split("?")[0] for p in path.split("/") if p]
        i = 0
        while i < len(parts) and parts[i] in _SKIP_PATH:
            i += 1
        if i < len(parts) and parts[i] not in _SKIP_PATH:
            # /p/SHORTCODE/ has no username; first non-skip segment may be a post id — usernames are not all-digit
            cand = parts[i]
            if cand.isdigit():
                return ""
            return cand
        return ""
    return re.sub(r"^@+", "", s)


def authenticate_instaloader(L: instaloader.Instaloader) -> None:
    """
    Session file is the reliable path for 2FA accounts: complete 2FA once via CLI, then load file.

    Priority:
    1. ``INSTAGRAM_SESSION_FILE`` + ``INSTAGRAM_LOGIN`` → ``load_session_from_file(user, filename=...)``
    2. ``INSTAGRAM_LOGIN`` only (no password) → default session path (after ``instaloader --login``)
    3. ``INSTAGRAM_LOGIN`` + ``INSTAGRAM_PASSWORD`` → ``login()`` (often fails if 2FA is required)
    """
    user = os.environ.get("INSTAGRAM_LOGIN", "").strip()
    password = os.environ.get("INSTAGRAM_PASSWORD", "")
    session_file = os.environ.get("INSTAGRAM_SESSION_FILE", "").strip()

    if session_file:
        if not user:
            raise ValueError(
                "Set INSTAGRAM_LOGIN to the Instagram username for the saved session "
                "when using INSTAGRAM_SESSION_FILE."
            )
        path = Path(session_file).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Instagram session file not found: {path}")
        L.load_session_from_file(user, filename=str(path))
        return

    if user and password:
        L.login(user, password)
        return

    if user and not password:
        L.load_session_from_file(user)
        return


def scan_profile(username: str, *, max_posts: int = 12) -> InstagramScanResult:
    """
    Load public profile metadata and up to `max_posts` recent post captions.
    May raise instaloader exceptions if the profile is private or login is required.
    """
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )
    authenticate_instaloader(L)

    profile = instaloader.Profile.from_username(L.context, username)

    recent_captions: list[str] = []
    for i, post in enumerate(profile.get_posts()):
        if i >= max_posts:
            break
        cap = (post.caption or "").strip()
        if cap:
            recent_captions.append(cap)

    return InstagramScanResult(
        username=profile.username,
        full_name=profile.full_name or "",
        biography=profile.biography or "",
        followers=profile.followers,
        following=profile.followees,
        media_count=profile.mediacount,
        is_verified=profile.is_verified,
        external_url=profile.external_url or None,
        recent_captions=recent_captions,
    )


def scan_profile_as_dict(username: str, **kwargs: Any) -> dict[str, Any]:
    return asdict(scan_profile(username, **kwargs))


def _parse_max_posts() -> int | None:
    raw = os.environ.get("INSTAGRAM_MAX_POSTS", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    if n <= 0:
        return None
    return n


def _make_post_limit_filter(max_posts: int) -> Callable[[instaloader.Post], bool]:
    remaining = max_posts

    def _post_filter(_post: instaloader.Post) -> bool:
        nonlocal remaining
        if remaining <= 0:
            return False
        remaining -= 1
        return True

    return _post_filter


def download_profile_to_folder(
    username: str,
    base_dir: Path,
    *,
    max_posts: int | None = None,
) -> Path:
    """
    Mirror the CLI: running ``instaloader target`` from ``base_dir`` creates
    ``base_dir/<username>/`` with images and sidecar JSON (captions, metadata).

    Uses a lock around ``os.chdir`` because Instaloader writes relative to cwd.
    """
    base_dir = base_dir.resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    if max_posts is None:
        max_posts = _parse_max_posts()

    L = instaloader.Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=True,
        download_geotags=False,
        download_comments=False,
        save_metadata=True,
        compress_json=False,
    )
    authenticate_instaloader(L)

    post_filter: Callable[[instaloader.Post], bool] | None = None
    if max_posts is not None:
        post_filter = _make_post_limit_filter(max_posts)

    with _download_cwd_lock:
        prev = os.getcwd()
        try:
            os.chdir(base_dir)
            L.download_profile(username, fast_update=False, post_filter=post_filter)
        finally:
            os.chdir(prev)

    # Folder name matches Instagram's canonical username (may differ in case from input).
    for p in base_dir.iterdir():
        if p.is_dir() and p.name.lower() == username.lower():
            return p
    return base_dir / username


def load_captions_from_folder(profile_dir: Path) -> list[str]:
    """
    Read post captions from Instaloader's JSON sidecar files in a downloaded profile folder.
    Each file like ``2024-01-01_12-00-00_UTC.json`` contains the post's metadata.
    Returns captions in chronological order (oldest first by filename sort).
    """
    profile_dir = Path(profile_dir).resolve()
    if not profile_dir.is_dir():
        return []

    captions: list[str] = []
    for json_file in sorted(profile_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            node = data.get("node", {})
            edges = node.get("edge_media_to_caption", {}).get("edges", [])
            for edge in edges:
                text = edge.get("node", {}).get("text", "").strip()
                if text and text not in captions:
                    captions.append(text)
        except Exception:
            continue

    return captions


def summarize_download_folder(profile_dir: Path) -> dict[str, Any]:
    """Lightweight listing for the UI (names only; no file contents)."""
    profile_dir = profile_dir.resolve()
    if not profile_dir.is_dir():
        return {"exists": False, "file_count": 0, "sample_files": []}

    files: list[str] = []
    for root, _dirs, names in os.walk(profile_dir):
        for n in names:
            rel = str(Path(root, n).relative_to(profile_dir))
            files.append(rel)
    files.sort()
    sample = files[:24]
    return {
        "exists": True,
        "file_count": len(files),
        "sample_files": sample,
    }
