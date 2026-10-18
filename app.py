"""
TikLive Recorder — interface web.
Lancer en local :  uvicorn app:app --host 0.0.0.0 --port 8000
Mot de passe : variable d'environnement APP_PASSWORD (identifiant libre).
"""

import base64
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

import recorder as R

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

app = FastAPI(title="TikLive Recorder")


@app.middleware("http")
async def auth(request: Request, call_next):
    if not APP_PASSWORD or request.url.path == "/health":
        return await call_next(request)
    h = request.headers.get("authorization", "")
    if h.startswith("Basic "):
        try:
            _, pwd = base64.b64decode(h[6:]).decode().split(":", 1)
            if secrets.compare_digest(pwd, APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="TikLive"'})


@app.on_event("startup")
def _startup():
    R.start_background()


class Target(BaseModel):
    target: str


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/state")
def state():
    recs = sorted(R.STATE["recordings"].values(), key=lambda r: r["started"], reverse=True)
    return {"recordings": recs, "watchlist": list(R.STATE["watchlist"].values()),
            "disk": R.disk_info(), "interval": R.WATCH_INTERVAL}


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


INDEX = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX
