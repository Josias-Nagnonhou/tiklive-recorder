"""Photos de profil TikTok : récupérées par le serveur et gardées en cache (les liens TikTok expirent)."""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("tiklive.avatars")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
AV_DIR = DATA_DIR / "avatars"
AV_DIR.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
REFRESH = 7 * 86400          # on rafraîchit la photo une fois par semaine
RETRY = 3600                 # après un échec, on réessaie au bout d'une heure
_last_try: dict[str, float] = {}
_busy: set[str] = set()
_lock = threading.Lock()


def path_for(username: str) -> Path:
    safe = re.sub(r"[^a-z0-9_.\-]", "", username.lower())[:40] or "x"
    return AV_DIR / f"{safe}.jpg"


def _unescape(url: str) -> str:
    try:
        return json.loads(f'"{url}"')  # décode les / du JSON intégré à la page
    except Exception:
        return url.replace("\\u002F", "/")


def _find_url(username: str, c: httpx.Client) -> str | None:
    # 1) API live (répond aussi quand la personne n'est pas en live)
    try:
        j = c.get("https://www.tiktok.com/api-live/user/room/",
                  params={"aid": "1988", "uniqueId": username, "sourceType": "54"}).json()
        u = (j.get("data") or {}).get("user") or {}
        for k in ("avatarLarger", "avatarMedium", "avatarThumb"):
            if u.get(k):
                return u[k]
    except Exception:
        pass
    # 2) Page de profil publique
    try:
        html = c.get(f"https://www.tiktok.com/@{username}").text
        for k in ("avatarLarger", "avatarMedium", "avatarThumb"):
            m = re.search(rf'"{k}":"(https:[^"]+)"', html)
            if m:
                return _unescape(m.group(1))
        m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        if m:
            return m.group(1).replace("&amp;", "&")
    except Exception:
        pass
    return None


def fetch(username: str, cookie: str = "") -> bool:
    headers = {"User-Agent": UA, "Referer": "https://www.tiktok.com/"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=20) as c:
            url = _find_url(username, c)
            if not url:
                log.info("Photo de profil introuvable pour @%s", username)
                return False
            r = c.get(url)
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image") or len(r.content) < 500:
                return False
        tmp = path_for(username).with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(path_for(username))
        log.info("Photo de profil enregistrée pour @%s", username)
        return True
    except Exception as e:
        log.info("Photo de @%s non récupérée : %s", username, e)
        return False


def ensure(username: str, cookie: str = "", force: bool = False):
    """Lance la récupération en arrière-plan si la photo manque ou est ancienne."""
    if not username or username == "flux-direct":
        return
    p = path_for(username)
    fresh = p.exists() and time.time() - p.stat().st_mtime < REFRESH
    with _lock:
        if (fresh and not force) or username in _busy:
            return
        if not force and time.time() - _last_try.get(username, 0) < RETRY:
            return
        _busy.add(username)
        _last_try[username] = time.time()

    def run():
        try:
            fetch(username, cookie)
        finally:
            with _lock:
                _busy.discard(username)
    threading.Thread(target=run, daemon=True).start()
