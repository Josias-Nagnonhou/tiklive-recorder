"""
Google Drive : connexion (OAuth, autorisation « drive.file »), envoi des vidéos, et sauvegarde
de la mémoire de l'outil (state.json) dans le Drive pour survivre aux redémarrages du serveur.

La clé de connexion (refresh token) est chiffrée avant d'être écrite dans les journaux du
serveur, puis rangée dans la variable d'environnement GDRIVE_TOKEN_ENC.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("tiklive.drive")

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8000").rstrip("/")
REDIRECT_URI = BASE_URL + "/oauth/callback"
SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_NAME = "TikLive Recorder"
STATE_NAME = "tiklive_memoire.json"

_fernet = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256((CLIENT_SECRET + "|" + os.environ.get("APP_PASSWORD", "")).encode()).digest()))
_oauth_states: dict[str, str] = {}   # état OAuth -> "primary" ou "add"
MARGIN = 300 * 1024 * 1024           # on garde 300 Mo de marge sur chaque Drive


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def encrypt(tok: str) -> str:
    return _fernet.encrypt(tok.encode()).decode()


def decrypt(enc: str) -> str | None:
    try:
        return _fernet.decrypt(enc.encode()).decode()
    except (InvalidToken, Exception):
        return None


class DriveError(RuntimeError):
    def __init__(self, reason: str, message: str, status: int):
        super().__init__(f"{reason}: {message}")
        self.reason, self.message, self.status = reason, message, status

    @property
    def full(self) -> bool:
        m = self.message.lower()
        return self.reason in ("storageQuotaExceeded", "quotaExceeded") or ("quota" in m and "storage" in m)

    @property
    def temporary(self) -> bool:
        return self.status >= 500 or self.status == 429 or self.reason in (
            "rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError")


def _raise(r: httpx.Response):
    if r.status_code < 400:
        return
    reason, message = "", r.text[:300]
    try:
        err = r.json().get("error", {})
        message = err.get("message", message)
        reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
    except Exception:
        pass
    log.warning("Google Drive a refusé (%s) : %s — %s", r.status_code, reason, message)
    raise DriveError(reason or str(r.status_code), message, r.status_code)


# ---------------------------------------------------------------- Un compte Google Drive
class Account:
    def __init__(self, refresh: str, email: str = "", primary: bool = False):
        self.refresh, self.email, self.primary = refresh, email, primary
        self.access, self.exp = None, 0.0
        self.folder, self.state_id = None, None
        self._lock = threading.Lock()

    def _access(self) -> str:
        with self._lock:
            if self.access and time.time() < self.exp:
                return self.access
            r = httpx.post("https://oauth2.googleapis.com/token", data={
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "refresh_token": self.refresh, "grant_type": "refresh_token"}, timeout=30)
            j = r.json()
            if "access_token" not in j:
                raise RuntimeError(f"Connexion Google refusée : {j.get('error_description') or j.get('error')}")
            self.access, self.exp = j["access_token"], time.time() + j.get("expires_in", 3000) - 60
            return self.access

    def h(self):
        return {"Authorization": f"Bearer {self._access()}"}

    def about(self) -> dict:
        r = httpx.get("https://www.googleapis.com/drive/v3/about",
                      params={"fields": "storageQuota,user(emailAddress)"}, headers=self.h(), timeout=30)
        _raise(r)
        j = r.json()
        self.email = (j.get("user") or {}).get("emailAddress") or self.email
        q = j.get("storageQuota", {})
        limit, usage = int(q.get("limit") or 0), int(q.get("usage") or 0)
        return {"email": self.email, "primary": self.primary, "limit": limit, "usage": usage,
                "trash": int(q.get("usageInDriveTrash") or 0), "free": (limit - usage) if limit else None}

    def _find(self, name, mime=None, parent=None):
        q = [f"name = '{name}'", "trashed = false"]
        if mime:
            q.append(f"mimeType = '{mime}'")
        if parent:
            q.append(f"'{parent}' in parents")
        r = httpx.get("https://www.googleapis.com/drive/v3/files", headers=self.h(),
                      params={"q": " and ".join(q), "fields": "files(id)", "spaces": "drive"}, timeout=30)
        _raise(r)
        files = r.json().get("files", [])
        return files[0]["id"] if files else None

    def folder_id(self) -> str:
        if self.folder:
            return self.folder
        mime = "application/vnd.google-apps.folder"
        fid = self._find(FOLDER_NAME, mime)
        if not fid:
            r = httpx.post("https://www.googleapis.com/drive/v3/files", headers=self.h(),
                           json={"name": FOLDER_NAME, "mimeType": mime}, params={"fields": "id"}, timeout=30)
            _raise(r)
            fid = r.json()["id"]
        self.folder = fid
        return fid

    def upload_file(self, path: Path, name: str, progress=None) -> dict:
        """Envoi « reprenable » par morceaux de 16 Mo (adapté aux vidéos de plusieurs Go)."""
        size = path.stat().st_size
        mime = "video/mp4" if path.suffix == ".mp4" else "video/mp2t"
        r = httpx.post("https://www.googleapis.com/upload/drive/v3/files",
                       params={"uploadType": "resumable", "fields": "id,webViewLink"},
                       headers={**self.h(), "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                       json={"name": name, "parents": [self.folder_id()]}, timeout=60)
        _raise(r)
        session = r.headers["Location"]
        chunk = 16 * 1024 * 1024
        sent = 0
        with open(path, "rb") as f, httpx.Client(timeout=300) as c:
            while sent < size:
                f.seek(sent)
                data = f.read(chunk)
                end = sent + len(data) - 1
                for attempt in range(6):
                    try:
                        resp = c.put(session, content=data, headers={
                            **self.h(), "Content-Range": f"bytes {sent}-{end}/{size}"})
                        break
                    except httpx.HTTPError:
                        if attempt == 5:
                            raise
                        time.sleep(5 * (attempt + 1))
                if resp.status_code in (200, 201):
                    if progress:
                        progress(size, size)
                    return resp.json()
                if resp.status_code == 308:
                    rng = resp.headers.get("Range")
                    sent = int(rng.split("-")[1]) + 1 if rng else 0
                    if progress:
                        progress(sent, size)
                    continue
                _raise(resp)
                raise RuntimeError(f"Envoi refusé par Drive ({resp.status_code})")
        raise RuntimeError("Envoi interrompu.")

    def save_state(self, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode()
        fid = self.state_id or self._find(STATE_NAME, parent=self.folder_id())
        if fid:
            r = httpx.patch(f"https://www.googleapis.com/upload/drive/v3/files/{fid}",
                            params={"uploadType": "media"}, headers={**self.h(), "Content-Type": "application/json"},
                            content=body, timeout=60)
        else:
            meta = json.dumps({"name": STATE_NAME, "parents": [self.folder_id()]})
            boundary = "tiklive" + secrets.token_hex(8)
            multipart = (f"--{boundary}\r\nContent-Type: application/json\r\n\r\n{meta}\r\n"
                         f"--{boundary}\r\nContent-Type: application/json\r\n\r\n").encode() + body + \
                        f"\r\n--{boundary}--".encode()
            r = httpx.post("https://www.googleapis.com/upload/drive/v3/files",
                           params={"uploadType": "multipart", "fields": "id"},
                           headers={**self.h(), "Content-Type": f"multipart/related; boundary={boundary}"},
                           content=multipart, timeout=60)
        _raise(r)
        self.state_id = r.json().get("id", fid)

    def load_state(self) -> dict | None:
        fid = self._find(STATE_NAME, parent=self.folder_id())
        if not fid:
            return None
        self.state_id = fid
        r = httpx.get(f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media"},
                      headers=self.h(), timeout=60)
        _raise(r)
        return r.json()


# ---------------------------------------------------------------- Comptes connectés
PRIMARY: Account | None = None
EXTRAS: list[Account] = []


def accounts() -> list[Account]:
    return ([PRIMARY] if PRIMARY else []) + EXTRAS


def connected() -> bool:
    return bool(accounts())


def load_token():
    global PRIMARY
    enc = os.environ.get("GDRIVE_TOKEN_ENC", "")
    if enc:
        tok = decrypt(enc)
        if tok:
            PRIMARY = Account(tok, primary=True)
            log.info("Google Drive : connexion chargée.")
        else:
            log.warning("GDRIVE_TOKEN_ENC illisible (client secret ou mot de passe changé ?)")


def load_extras(stored: list[dict]):
    """Comptes supplémentaires, gardés (chiffrés) dans la mémoire de l'outil."""
    EXTRAS.clear()
    for a in stored or []:
        tok = decrypt(a.get("enc", ""))
        if tok:
            EXTRAS.append(Account(tok, a.get("email", "")))
    if EXTRAS:
        log.info("Google Drive : %d compte(s) supplémentaire(s) chargé(s).", len(EXTRAS))


def accounts_info() -> list[dict]:
    out = []
    for a in accounts():
        try:
            out.append(a.about())
        except Exception as e:
            out.append({"email": a.email, "primary": a.primary, "error": str(e)[:150]})
    return out


def pick_account(size: int) -> Account:
    """Le Drive principal s'il a assez de place, sinon celui qui en a le plus."""
    best, best_free = None, -1
    for a in accounts():
        try:
            q = a.about()
        except Exception as e:
            log.warning("Drive %s illisible : %s", a.email or "?", e)
            continue
        free = q["free"] if q["free"] is not None else 10**15
        if free >= size + MARGIN:
            return a
        if free > best_free:
            best, best_free = a, free
    if best is None:
        raise RuntimeError("Aucun Google Drive joignable.")
    raise DriveError("storageQuotaExceeded", f"Tous les Drive sont pleins (le plus libre : {best_free // 2**20} Mo)", 403)


# ---------------------------------------------------------------- Compatibilité (compte principal)
def quota() -> dict:
    if not PRIMARY:
        raise RuntimeError("Google Drive n'est pas connecté.")
    return PRIMARY.about()


def upload_best(path: Path, name: str, progress=None) -> tuple[dict, Account]:
    acc = pick_account(path.stat().st_size)
    return acc.upload_file(path, name, progress), acc


def save_state(data: dict):
    """Sauvegarde sur le Drive principal ; s'il est plein, sur un autre compte."""
    data = {**data, "saved_at": time.time()}
    last = None
    for a in accounts():
        try:
            a.save_state(data)
            return
        except Exception as e:
            last = e
    if last:
        raise last


def load_state() -> dict | None:
    """Prend la sauvegarde la plus récente parmi tous les comptes connus."""
    best = None
    for a in accounts():
        try:
            st = a.load_state()
        except Exception as e:
            log.warning("Mémoire illisible sur %s : %s", a.email or "?", e)
            continue
        if st and (best is None or st.get("saved_at", 0) > best.get("saved_at", 0)):
            best = st
    return best


def folder_id() -> str:
    return PRIMARY.folder_id() if PRIMARY else accounts()[0].folder_id()


# ---------------------------------------------------------------- OAuth
def auth_url(mode: str = "primary") -> str:
    st = secrets.token_urlsafe(24)
    _oauth_states[st] = mode
    q = httpx.QueryParams({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code",
                           "scope": SCOPE, "access_type": "offline", "prompt": "consent select_account",
                           "state": st})
    return f"https://accounts.google.com/o/oauth2/v2/auth?{q}"


def exchange(code: str, state: str) -> tuple[str, str, str]:
    """Renvoie (mode, refresh_token, email)."""
    global PRIMARY
    if state not in _oauth_states:
        raise ValueError("Lien de connexion expiré, recommence depuis la page Réglages.")
    mode = _oauth_states.pop(state)
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"}, timeout=30)
    j = r.json()
    if "refresh_token" not in j:
        raise ValueError(f"Google n'a pas donné de clé : {j.get('error_description') or j}")
    acc = Account(j["refresh_token"])
    acc.access, acc.exp = j.get("access_token"), time.time() + j.get("expires_in", 3000) - 60
    email = acc.about()["email"]
    if mode == "add":
        for a in accounts():
            if a.email and a.email.lower() == email.lower():
                raise ValueError(f"Le compte {email} est déjà connecté à l'outil.")
        EXTRAS.append(acc)
    else:
        acc.primary = True
        PRIMARY = acc
    return mode, j["refresh_token"], email


def remove_extra(email: str) -> bool:
    for a in list(EXTRAS):
        if a.email.lower() == email.lower():
            EXTRAS.remove(a)
            return True
    return False
