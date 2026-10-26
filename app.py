"""
TikLive Recorder — interface web.
Lancer en local :  uvicorn app:app --host 0.0.0.0 --port 8000
Mot de passe : variable d'environnement APP_PASSWORD (identifiant libre).
"""

import base64
import json
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

import avatars as A
import gdrive as G
import notify as N
import recorder as R

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_USERNAME = os.environ.get("APP_USERNAME", "").strip().lower()

app = FastAPI(title="TikLive Recorder")
PUBLIC_PATHS = {"/health", "/accueil", "/confidentialite", "/oauth/callback", "/favicon.ico", "/apple-touch-icon.png"}
STATIC_DIR = Path(__file__).parent / "static"

_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t}</title><style>body{{font-family:system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.6;color:#222}}</style>
</head><body><h1>{t}</h1>{b}</body></html>"""


@app.middleware("http")
async def auth(request: Request, call_next):
    if not APP_PASSWORD or request.url.path in PUBLIC_PATHS or request.url.path.startswith("/static/"):
        return await call_next(request)
    h = request.headers.get("authorization", "")
    if h.startswith("Basic "):
        try:
            user, pwd = base64.b64decode(h[6:]).decode().split(":", 1)
            user_ok = not APP_USERNAME or secrets.compare_digest(user.strip().lower(), APP_USERNAME)
            if user_ok and secrets.compare_digest(pwd, APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="TikLive"'})


@app.on_event("startup")
def _startup():
    R.start_background()


class Target(BaseModel):
    target: str


@app.get("/accueil", response_class=HTMLResponse)
def accueil():
    return _PAGE.format(t="TikLive Recorder", b="<p>TikLive Recorder est un outil personnel et privé qui enregistre "
        "des lives TikTok publics pour son propriétaire, et peut déposer les vidéos dans son propre Google Drive.</p>"
        "<p>Accès réservé au propriétaire. <a href='/confidentialite'>Règles de confidentialité</a></p>")


@app.get("/confidentialite", response_class=HTMLResponse)
def confidentialite():
    return _PAGE.format(t="Règles de confidentialité", b="<p>TikLive Recorder est utilisé uniquement par son "
        "propriétaire. L'accès à Google Drive sert seulement à déposer les vidéos enregistrées dans un dossier "
        "créé par l'outil (autorisation « drive.file »). L'outil ne lit pas, ne partage pas et ne vend aucune autre "
        "donnée. Les identifiants Google sont stockés uniquement sur le serveur privé de l'outil et peuvent être "
        "révoqués à tout moment depuis https://myaccount.google.com/permissions.</p>"
        "<p>Contact : propriétaire de l'outil (Josias).</p>")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/apple-touch-icon.png")
def apple_icon():
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")


@app.get("/static/{name}")
def static_file(name: str):
    path = (STATIC_DIR / name).resolve()
    if path.parent != STATIC_DIR.resolve() or not path.is_file():
        raise HTTPException(404)
    types = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}
    return FileResponse(path, media_type=types.get(path.suffix, "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/avatar/{username}")
def avatar(username: str):
    p = A.path_for(username)
    if not p.exists():
        A.ensure(username, R.TIKTOK_COOKIE)
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/state")
def state():
    recs = sorted(R.STATE["recordings"].values(), key=lambda r: r["started"], reverse=True)
    return {"recordings": recs, "watchlist": list(R.STATE["watchlist"].values()),
            "disk": R.disk_info(), "interval": R.WATCH_INTERVAL,
            "notifications": R.STATE.get("notifications", [])[:15]}


@app.post("/api/record")
def record(t: Target):
    try:
        rec = R.start_recording(t.target)
        return {"ok": True, "recording": rec}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except R.TikTokUnreachable:
        return JSONResponse({"ok": False, "message": "TikTok ne répond pas au serveur pour le moment "
                             "(blocage ou coupure réseau). Réessaie dans quelques minutes."}, 200)
    except R.NotLive:
        return JSONResponse({"ok": False, "not_live": True,
                             "message": "Cette personne n'est pas en live en ce moment."}, 200)


@app.post("/api/recordings/{rec_id}/drive")
def resend_drive(rec_id: str):
    rec = R.STATE["recordings"].get(rec_id)
    if not rec or not rec.get("file"):
        raise HTTPException(404, "Cette vidéo n'est plus sur le serveur.")
    if rec["status"] in R.ACTIVE:
        raise HTTPException(400, "Envoi déjà en cours.")
    if not G.connected():
        raise HTTPException(400, "Google Drive n'est pas connecté.")
    import threading
    threading.Thread(target=R.upload_to_drive, args=(rec_id, True), daemon=True).start()
    return {"ok": True}


@app.get("/api/drive/quota")
def drive_quota():
    if not G.connected():
        return {"connected": False}
    try:
        return {"connected": True, **G.quota()}
    except Exception as e:
        return {"connected": True, "error": str(e)[:200]}


@app.post("/api/stop/{rec_id}")
def stop(rec_id: str):
    if not R.stop_recording(rec_id):
        raise HTTPException(404, "Aucun enregistrement actif avec cet identifiant.")
    return {"ok": True}


@app.delete("/api/recordings/{rec_id}")
def delete(rec_id: str):
    if not R.delete_recording(rec_id):
        raise HTTPException(400, "Impossible de supprimer (introuvable ou encore en cours).")
    return {"ok": True}


@app.get("/download/{rec_id}")
def download(rec_id: str):
    rec = R.STATE["recordings"].get(rec_id)
    if not rec or not rec.get("file"):
        raise HTTPException(404, "Fichier pas encore prêt.")
    path = R.REC_DIR / rec["file"]
    if not path.exists():
        raise HTTPException(404, "Fichier introuvable.")
    media = "video/mp4" if path.suffix == ".mp4" else "video/mp2t"
    return FileResponse(path, media_type=media, filename=path.name)


@app.post("/api/watch")
def watch(t: Target):
    try:
        return {"ok": True, "watch": R.add_watch(t.target)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/watch/{username}")
def unwatch(username: str):
    R.remove_watch(username)
    return {"ok": True}


class Settings(BaseModel):
    wa_phone: str | None = None
    wa_apikey: str | None = None
    retention_days: int | None = None
    notify_start: bool | None = None
    notify_done: bool | None = None
    notify_error: bool | None = None
    notify_disk: bool | None = None
    drive_delete_local: bool | None = None


def _public_settings():
    s = dict(R.STATE["settings"])
    key = s.get("wa_apikey") or ""
    s["wa_apikey"] = ("•" * max(0, len(key) - 2) + key[-2:]) if key else ""
    s["wa_configured"] = bool(s.get("wa_phone") and key)
    s["drive_available"] = G.configured()
    s["drive_connected"] = G.connected()
    return s


@app.get("/api/settings")
def get_settings():
    return _public_settings()


@app.post("/api/settings")
def set_settings(body: Settings):
    data = body.model_dump(exclude_none=True)
    if "wa_apikey" in data and "•" in data["wa_apikey"]:
        data.pop("wa_apikey")  # champ masqué non modifié
    if "retention_days" in data:
        data["retention_days"] = max(0, min(365, data["retention_days"]))
    with R._lock:
        R.STATE["settings"].update(data)
        R.save_state()
    return _public_settings()


@app.get("/oauth/start")
def oauth_start():
    if not G.configured():
        raise HTTPException(400, "Google Drive n'est pas configuré sur le serveur.")
    return RedirectResponse(G.auth_url())


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return _PAGE.format(t="Connexion annulée", b=f"<p>Google a répondu : {error or 'aucun code'}.</p>"
                            "<p><a href='/'>Retour à l'outil</a></p>")
    try:
        refresh = G.exchange(code, state)
    except Exception as e:
        return _PAGE.format(t="Connexion impossible", b=f"<p>{e}</p><p><a href='/'>Retour à l'outil</a></p>")
    # Clé chiffrée (illisible sans le code secret + le mot de passe) : à ranger dans GDRIVE_TOKEN_ENC
    logging.getLogger("tiklive").warning("GDRIVE_TOKEN_ENC_TO_SAVE=%s", G.encrypt(refresh))
    try:
        G.folder_id()
        with R._lock:
            snap = json.loads(json.dumps(R.STATE))
        G.save_state(snap)
    except Exception as e:
        logging.getLogger("tiklive").warning("Première sauvegarde Drive échouée: %s", e)
    return _PAGE.format(t="✅ Google Drive connecté", b="<p>Tes prochaines vidéos seront envoyées automatiquement "
                        f"dans le dossier « {G.FOLDER_NAME} » de ton Drive.</p><p><a href='/'>Retour à l'outil</a></p>")


@app.post("/api/settings/test-phone")
def test_phone():
    ok, msg = N.send_ntfy("🔔 TikLive Recorder\nTest réussi ! Tu recevras ici les alertes de tes lives.")
    return {"ok": ok, "message": msg}


@app.post("/api/settings/test")
def test_whatsapp():
    ok, msg = N.send_whatsapp("🔔 TikLive Recorder : test réussi ! Tu recevras ici les alertes de tes lives.")
    return {"ok": ok, "message": msg}


INDEX = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX
