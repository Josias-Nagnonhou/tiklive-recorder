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
_lock = threading.Lock()
_tok = {"refresh": None, "access": None, "exp": 0}
_ids = {"folder": None, "state": None}
_oauth_states: set[str] = set()


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def connected() -> bool:
    return bool(_tok["refresh"])


def encrypt(tok: str) -> str:
    return _fernet.encrypt(tok.encode()).decode()


def load_token():
    enc = os.environ.get("GDRIVE_TOKEN_ENC", "")
    if enc:
        try:
            _tok["refresh"] = _fernet.decrypt(enc.encode()).decode()
            log.info("Google Drive : connexion chargée.")
        except InvalidToken:
            log.warning("GDRIVE_TOKEN_ENC illisible (client secret ou mot de passe changé ?)")


def set_refresh(tok: str):
    _tok.update(refresh=tok, access=None, exp=0)


# ---------------------------------------------------------------- OAuth
def auth_url() -> str:
    st = secrets.token_urlsafe(24)
    _oauth_states.add(st)
    q = httpx.QueryParams({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code",
                           "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": st})
    return f"https://accounts.google.com/o/oauth2/v2/auth?{q}"


def exchange(code: str, state: str) -> str:
    if state not in _oauth_states:
        raise ValueError("Lien de connexion expiré, recommence depuis la page Réglages.")
    _oauth_states.discard(state)
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"}, timeout=30)
    j = r.json()
    if "refresh_token" not in j:
        raise ValueError(f"Google n'a pas donné de clé : {j.get('error_description') or j}")
    set_refresh(j["refresh_token"])
    _tok.update(access=j.get("access_token"), exp=time.time() + j.get("expires_in", 3000) - 60)
    return j["refresh_token"]


def _access() -> str:
    with _lock:
        if _tok["access"] and time.time() < _tok["exp"]:
            return _tok["access"]
        if not _tok["refresh"]:
            raise RuntimeError("Google Drive n'est pas connecté.")
        r = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "refresh_token": _tok["refresh"], "grant_type": "refresh_token"}, timeout=30)
        j = r.json()
        if "access_token" not in j:
            raise RuntimeError(f"Connexion Google refusée : {j.get('error_description') or j.get('error')}")
        _tok.update(access=j["access_token"], exp=time.time() + j.get("expires_in", 3000) - 60)
        return _tok["access"]


def _h():
    return {"Authorization": f"Bearer {_access()}"}


# ---------------------------------------------------------------- Fichiers
def _find(name: str, mime: str | None = None, parent: str | None = None) -> str | None:
    q = [f"name = '{name}'", "trashed = false"]
    if mime:
        q.append(f"mimeType = '{mime}'")
    if parent:
        q.append(f"'{parent}' in parents")
    r = httpx.get("https://www.googleapis.com/drive/v3/files", headers=_h(),
                  params={"q": " and ".join(q), "fields": "files(id)", "spaces": "drive"}, timeout=30)
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def folder_id() -> str:
    if _ids["folder"]:
        return _ids["folder"]
    mime = "application/vnd.google-apps.folder"
    fid = _find(FOLDER_NAME, mime)
    if not fid:
        r = httpx.post("https://www.googleapis.com/drive/v3/files", headers=_h(),
                       json={"name": FOLDER_NAME, "mimeType": mime}, params={"fields": "id"}, timeout=30)
        r.raise_for_status()
        fid = r.json()["id"]
    _ids["folder"] = fid
    return fid


def upload_file(path: Path, name: str, progress=None) -> dict:
    """Envoi « reprenable » par morceaux de 16 Mo (adapté aux vidéos de plusieurs Go)."""
    size = path.stat().st_size
    mime = "video/mp4" if path.suffix == ".mp4" else "video/mp2t"
    r = httpx.post("https://www.googleapis.com/upload/drive/v3/files",
                   params={"uploadType": "resumable", "fields": "id,webViewLink"},
                   headers={**_h(), "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                   json={"name": name, "parents": [folder_id()]}, timeout=60)
    r.raise_for_status()
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
                        **_h(), "Content-Range": f"bytes {sent}-{end}/{size}"})
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
            raise RuntimeError(f"Envoi refusé par Drive ({resp.status_code}) : {resp.text[:200]}")
    raise RuntimeError("Envoi interrompu.")


# ---------------------------------------------------------------- Mémoire de l'outil
def save_state(data: dict):
    body = json.dumps(data, ensure_ascii=False).encode()
    fid = _ids["state"] or _find(STATE_NAME, parent=folder_id())
    if fid:
        r = httpx.patch(f"https://www.googleapis.com/upload/drive/v3/files/{fid}",
                        params={"uploadType": "media"}, headers={**_h(), "Content-Type": "application/json"},
                        content=body, timeout=60)
    else:
        meta = json.dumps({"name": STATE_NAME, "parents": [folder_id()]})
        boundary = "tiklive" + secrets.token_hex(8)
        multipart = (f"--{boundary}\r\nContent-Type: application/json\r\n\r\n{meta}\r\n"
                     f"--{boundary}\r\nContent-Type: application/json\r\n\r\n").encode() + body + \
                    f"\r\n--{boundary}--".encode()
        r = httpx.post("https://www.googleapis.com/upload/drive/v3/files",
                       params={"uploadType": "multipart", "fields": "id"},
                       headers={**_h(), "Content-Type": f"multipart/related; boundary={boundary}"},
                       content=multipart, timeout=60)
    r.raise_for_status()
    _ids["state"] = r.json().get("id", fid)


def load_state() -> dict | None:
    fid = _find(STATE_NAME, parent=folder_id())
    if not fid:
        return None
    _ids["state"] = fid
    r = httpx.get(f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media"},
                  headers=_h(), timeout=60)
    r.raise_for_status()
    return r.json()
