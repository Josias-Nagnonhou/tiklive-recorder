"""
Moteur d'enregistrement des lives TikTok.

- Trouve l'URL du flux vidéo d'un live (via yt-dlp, puis l'API web de TikTok en secours)
- Enregistre le flux avec ffmpeg, du début à la fin
- Si la connexion coupe mais que le live continue, reprend automatiquement
- À la fin, assemble tous les morceaux en un seul MP4 téléchargeable
- Surveille une liste de comptes et lance l'enregistrement dès qu'un live démarre
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

import notify as N

log = logging.getLogger("tiklive")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
REC_DIR = DATA_DIR / "recordings"
STATE_FILE = DATA_DIR / "state.json"
WATCH_INTERVAL = int(os.environ.get("WATCH_INTERVAL", "60"))  # secondes entre deux vérifications
TIKTOK_COOKIE = os.environ.get("TIKTOK_COOKIE", "")  # optionnel: "sessionid=xxx" pour les lives restreints
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

REC_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.RLock()


# --------------------------------------------------------------------------- #
#  État (sauvegardé sur disque pour survivre aux redémarrages)
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_SETTINGS = {"wa_phone": "", "wa_apikey": "", "retention_days": 7,
                    "notify_start": True, "notify_done": True, "notify_error": True, "notify_disk": True}


def _load_state() -> dict:
    st = {"recordings": {}, "watchlist": {}}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
        except Exception:
            log.exception("state.json illisible, on repart de zéro")
    st.setdefault("settings", {})
    for k, v in DEFAULT_SETTINGS.items():
        st["settings"].setdefault(k, v)
    return st


def fmt_size(b) -> str:
    b = b or 0
    return f"{b/1e9:.2f} Go" if b > 1e9 else f"{b/1e6:.0f} Mo"


def fmt_dur(sec) -> str:
    sec = int(sec or 0)
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h}h{m:02d}" if h else f"{m} min"


STATE = _load_state()
_procs: dict[str, subprocess.Popen] = {}   # id -> ffmpeg en cours
_stop_flags: dict[str, threading.Event] = {}


def save_state():
    with _lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATE, indent=2, ensure_ascii=False))
        tmp.replace(STATE_FILE)


def _update(rec_id: str, **fields):
    with _lock:
        STATE["recordings"][rec_id].update(fields)
        save_state()


# --------------------------------------------------------------------------- #
#  Lecture du lien / pseudo
# --------------------------------------------------------------------------- #
def parse_target(text: str) -> dict:
    """Accepte: lien TikTok (@pseudo/live), @pseudo, pseudo, ou lien direct .flv/.m3u8."""
    text = text.strip()
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.\-]+)", text)
    if m:
        return {"kind": "tiktok", "username": m.group(1).lower()}
    if re.match(r"^https?://", text):
        if "tiktok.com" in text or "vm.tiktok" in text or "vt.tiktok" in text:
            # lien court (vm.tiktok.com/xxx) : on suit la redirection
            try:
                r = httpx.get(text, headers={"User-Agent": UA}, follow_redirects=True, timeout=15)
                m = re.search(r"tiktok\.com/@([A-Za-z0-9_.\-]+)", str(r.url))
                if m:
                    return {"kind": "tiktok", "username": m.group(1).lower()}
            except Exception:
                pass
            raise ValueError("Je n'arrive pas à trouver le pseudo dans ce lien TikTok.")
        return {"kind": "direct", "url": text, "username": "flux-direct"}
    name = text.lstrip("@").strip()
    if re.fullmatch(r"[A-Za-z0-9_.\-]{2,40}", name):
        return {"kind": "tiktok", "username": name.lower()}
    raise ValueError("Lien ou pseudo non reconnu. Exemple : https://www.tiktok.com/@pseudo/live ou @pseudo")


# --------------------------------------------------------------------------- #
#  Trouver l'URL du flux vidéo d'un live
# --------------------------------------------------------------------------- #
class NotLive(Exception):
    pass


class TikTokUnreachable(Exception):
    """TikTok ne répond pas / bloque le serveur (différent de « pas en live »)."""


def _via_ytdlp(username: str) -> tuple[str, str]:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if TIKTOK_COOKIE:
        opts["http_headers"] = {"Cookie": TIKTOK_COOKIE}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{username}/live", download=False)
    if not info.get("is_live", True):
        raise NotLive()
    fmts = [f for f in (info.get("formats") or []) if f.get("url")]
    # On privilégie le FLV (plus stable pour l'enregistrement long), puis la meilleure qualité
    fmts.sort(key=lambda f: ((f.get("ext") == "flv"), f.get("height") or 0, f.get("tbr") or 0))
    url = fmts[-1]["url"] if fmts else info.get("url")
    if not url:
        raise NotLive()
    return url, info.get("title") or ""


def _via_webapi(username: str) -> tuple[str, str]:
    headers = {"User-Agent": UA, "Referer": "https://www.tiktok.com/"}
    if TIKTOK_COOKIE:
        headers["Cookie"] = TIKTOK_COOKIE
    with httpx.Client(headers=headers, follow_redirects=True, timeout=20) as c:
        room_id = None
        try:
            j = c.get(
                "https://www.tiktok.com/api-live/user/room/",
                params={"aid": "1988", "uniqueId": username, "sourceType": "54"},
            ).json()
            room_id = (j.get("data") or {}).get("user", {}).get("roomId")
        except Exception:
            pass
        if not room_id:
            html = c.get(f"https://www.tiktok.com/@{username}/live").text
            m = re.search(r'"roomId":"(\d+)"', html) or re.search(r"room_id=(\d+)", html)
            room_id = m.group(1) if m else None
        if not room_id:
            raise NotLive()
        data = c.get(
            "https://webcast.tiktok.com/webcast/room/info/",
            params={"aid": "1988", "room_id": room_id},
        ).json().get("data") or {}
    if data.get("status") != 2:  # 2 = en direct
        raise NotLive()
    stream = data.get("stream_url") or {}
    flv = stream.get("flv_pull_url") or {}
    for q in ("FULL_HD1", "HD1", "SD1", "SD2"):
        if flv.get(q):
            return flv[q], data.get("title") or ""
    # format plus récent : JSON imbriqué
    try:
        sd = json.loads(stream["live_core_sdk_data"]["pull_data"]["stream_data"])["data"]
        for q in ("origin", "uhd", "hd", "sd", "ld"):
            if q in sd and sd[q]["main"].get("flv"):
                return sd[q]["main"]["flv"], data.get("title") or ""
    except Exception:
        pass
    if stream.get("rtmp_pull_url"):
        return stream["rtmp_pull_url"], data.get("title") or ""
    raise NotLive()


def get_stream(target: dict) -> tuple[str, str]:
    """Renvoie (url_du_flux, titre). Lève NotLive si la personne n'est pas en live."""
    if target["kind"] == "direct":
        return target["url"], ""
    username = target["username"]
    errors, confirmed_offline = [], False
    for fn in (_via_ytdlp, _via_webapi):
        try:
            return fn(username)
        except NotLive:
            confirmed_offline = True
            errors.append(f"{fn.__name__}: pas en live")
        except Exception as e:
            msg = str(e)
            # yt-dlp signale un compte hors live par une erreur contenant "live"/"offline"
            if re.search(r"not (currently )?live|offline|is not live|room.*(ended|closed)", msg, re.I):
                confirmed_offline = True
            errors.append(f"{fn.__name__}: {msg[:200]}")
    log.info("@%s: %s", username, " | ".join(errors))
    if confirmed_offline:
        raise NotLive(" | ".join(errors))
    raise TikTokUnreachable(" | ".join(errors))


# --------------------------------------------------------------------------- #
#  Enregistrement
# --------------------------------------------------------------------------- #
def _ffmpeg_record(stream_url: str, out: Path) -> subprocess.Popen:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-user_agent", UA]
    if stream_url.startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "30000000"]
    cmd += ["-i", stream_url, "-map", "0", "-c", "copy", "-f", "mpegts", str(out)]
    errlog = open(out.with_suffix(".log"), "ab")  # fichier plutôt que pipe : évite tout blocage
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errlog)


def _finalize(rec_id: str):
    """Assemble les morceaux .ts en un seul .mp4."""
    rec = STATE["recordings"][rec_id]
    folder = REC_DIR / rec_id
    parts = sorted(p for p in folder.glob("part*.ts") if p.stat().st_size > 1024)
    if not parts:
        _update(rec_id, status="error", error="Aucune vidéo n'a pu être enregistrée.")
        N.notify("error", f"⚠️ TikLive : l'enregistrement du live de @{rec['username']} a échoué "
                          f"(aucune image reçue).{N.link()}")
        return
    _update(rec_id, status="finalizing")
    final = REC_DIR / f"{rec['filename']}.mp4"
    listfile = folder / "parts.txt"
    listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", "-bsf:a", "aac_adtstoasc",
         "-movflags", "+faststart", str(final)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not final.exists() or final.stat().st_size < 1024:
        # secours : on garde le flux brut, toujours lisible (VLC, CapCut…)
        final = REC_DIR / f"{rec['filename']}.ts"
        with open(final, "wb") as out:
            for p in parts:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out)
        log.warning("remux mp4 échoué (%s), fichier .ts conservé", r.stderr[-300:])
    shutil.rmtree(folder, ignore_errors=True)
    dur = _probe_duration(final)
    status = rec.get("end_status", "done")
    size = final.stat().st_size
    _update(rec_id, status=status, file=final.name, size=size, duration=dur,
            ended=rec.get("ended") or _now())
    note = {"stopped": " (arrêté manuellement)", "interrupted": " (interrompu par un redémarrage du serveur)"}
    keep = STATE["settings"].get("retention_days", 7)
    N.notify("done", f"✅ TikLive : la vidéo du live de @{rec['username']} est prête{note.get(status, '')}.\n"
                     f"⏱ {fmt_dur(dur)} · 💾 {fmt_size(size)}\n"
                     f"Pense à la télécharger : elle sera supprimée automatiquement dans {keep} jours.{N.link()}")


def _probe_duration(path: Path) -> float | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
        return round(float(r.stdout.strip()), 1)
    except Exception:
        return None


def _record_loop(rec_id: str, target: dict, stream_url: str):
    stop = _stop_flags[rec_id]
    folder = REC_DIR / rec_id
    folder.mkdir(parents=True, exist_ok=True)
    part = 0
    try:
        while not stop.is_set():
            part += 1
            out = folder / f"part{part:03d}.ts"
            proc = _ffmpeg_record(stream_url, out)
            _procs[rec_id] = proc
            _update(rec_id, status="recording", parts=part)
            # Pendant l'enregistrement, on met à jour la taille
            while proc.poll() is None:
                if stop.wait(3):
                    break
                size = sum(p.stat().st_size for p in folder.glob("part*.ts") if p.exists())
                _update(rec_id, size=size)
            if proc.poll() is None:  # arrêt demandé
                try:
                    proc.stdin.write(b"q"); proc.stdin.flush()
                    proc.wait(timeout=10)
                except Exception:
                    proc.send_signal(signal.SIGINT)
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        proc.kill()
                break
            logf = out.with_suffix(".log")
            err = logf.read_text(errors="ignore")[-300:] if logf.exists() else ""
            if err:
                log.info("[%s] ffmpeg terminé: %s", rec_id, err)
            if target["kind"] == "direct":
                break
            # Le flux s'est coupé : le live est-il fini, ou juste une coupure réseau ?
            still_live = False
            for attempt in range(4):
                if attempt and stop.wait(10):  # 1re vérification immédiate, puis toutes les 10 s
                    break
                try:
                    stream_url, _t = get_stream(target)
                    still_live = True
                    break
                except (NotLive, TikTokUnreachable):
                    continue
            if not still_live:
                break
            log.info("[%s] coupure détectée, le live continue → reprise", rec_id)
    except Exception as e:
        log.exception("[%s] erreur d'enregistrement", rec_id)
        _update(rec_id, error=str(e)[:300])
    finally:
        _procs.pop(rec_id, None)
        with _lock:
            STATE["recordings"][rec_id]["ended"] = _now()
            if stop.is_set():
                STATE["recordings"][rec_id]["end_status"] = "stopped"
            save_state()
        _finalize(rec_id)
        _stop_flags.pop(rec_id, None)


def active_for(username: str) -> str | None:
    for rid, r in STATE["recordings"].items():
        if r["username"] == username and r["status"] in ("recording", "starting", "finalizing"):
            return rid
    return None


def start_recording(text_or_target, source: str = "manuel") -> dict:
    target = parse_target(text_or_target) if isinstance(text_or_target, str) else text_or_target
    if target["kind"] == "tiktok":
        existing = active_for(target["username"])
        if existing:
            return STATE["recordings"][existing]
    stream_url, title = get_stream(target)  # lève NotLive
    rec_id = uuid.uuid4().hex[:10]
    stamp = datetime.now().strftime("%Y-%m-%d_%Hh%M")
    rec = {
        "id": rec_id, "username": target["username"], "title": title, "source": source,
        "status": "starting", "started": _now(), "ended": None, "size": 0, "parts": 0,
        "file": None, "duration": None, "error": None,
        "filename": f"{target['username']}_{stamp}_{rec_id[:4]}",
    }
    with _lock:
        STATE["recordings"][rec_id] = rec
        save_state()
    _stop_flags[rec_id] = threading.Event()
    threading.Thread(target=_record_loop, args=(rec_id, target, stream_url), daemon=True).start()
    log.info("▶ Enregistrement lancé: @%s (%s)", target["username"], rec_id)
    if target["kind"] == "tiktok":
        auto = "Live détecté automatiquement" if source == "surveillance" else "Enregistrement lancé"
        N.notify("start", f"🔴 TikLive : {auto} pour @{target['username']}"
                          f"{(' — « ' + title[:80] + ' »') if title else ''}.\n"
                          f"Je t'envoie un message dès que la vidéo est prête.")
    return rec


def stop_recording(rec_id: str) -> bool:
    ev = _stop_flags.get(rec_id)
    if ev:
        ev.set()
        return True
    return False


def delete_recording(rec_id: str) -> bool:
    with _lock:
        rec = STATE["recordings"].get(rec_id)
        if not rec or rec["status"] in ("recording", "starting", "finalizing"):
            return False
        if rec.get("file"):
            (REC_DIR / rec["file"]).unlink(missing_ok=True)
        shutil.rmtree(REC_DIR / rec_id, ignore_errors=True)
        del STATE["recordings"][rec_id]
        save_state()
    return True


# --------------------------------------------------------------------------- #
#  Surveillance de comptes
# --------------------------------------------------------------------------- #
def add_watch(text: str) -> dict:
    target = parse_target(text)
    if target["kind"] != "tiktok":
        raise ValueError("La surveillance fonctionne avec un pseudo ou un lien TikTok.")
    u = target["username"]
    with _lock:
        STATE["watchlist"].setdefault(u, {"username": u, "added": _now(),
                                          "last_check": None, "last_status": "en attente"})
        save_state()
    _wake.set()
    return STATE["watchlist"][u]


def remove_watch(username: str):
    with _lock:
        STATE["watchlist"].pop(username, None)
        save_state()


_wake = threading.Event()


def _watch_loop():
    while True:
        for u in list(STATE["watchlist"].keys()):
            if u not in STATE["watchlist"]:
                continue
            status = "pas en live"
            if active_for(u):
                status = "🔴 en cours d'enregistrement"
            else:
                try:
                    start_recording({"kind": "tiktok", "username": u}, source="surveillance")
                    status = "🔴 live détecté → enregistrement lancé"
                except NotLive:
                    pass
                except TikTokUnreachable:
                    status = "erreur: TikTok ne répond pas au serveur"
                except Exception as e:
                    status = f"erreur: {str(e)[:120]}"
            prev = (STATE["watchlist"].get(u) or {}).get("last_status", "")
            if status.startswith("erreur") and not prev.startswith("erreur"):
                N.notify("error", f"⚠️ TikLive : je n'arrive plus à vérifier @{u} ({status[8:]}). "
                                  f"Je continue d'essayer.{N.link()}")
            with _lock:
                if u in STATE["watchlist"]:
                    STATE["watchlist"][u].update(last_check=_now(), last_status=status)
                    save_state()
        _wake.wait(WATCH_INTERVAL)
        _wake.clear()


def recover_after_restart():
    """Si le serveur a redémarré pendant un enregistrement, on sauve ce qui a été capté."""
    for rid, r in list(STATE["recordings"].items()):
        if r["status"] in ("recording", "starting", "finalizing"):
            log.warning("Récupération de l'enregistrement interrompu %s", rid)
            with _lock:
                r["ended"] = r.get("ended") or _now()
                r["end_status"] = "interrupted"
                save_state()
            _finalize(rid)


LOW_DISK = 2e9  # alerte sous 2 Go libres


def cleanup_once():
    """Supprime les vidéos terminées depuis plus de N jours ; alerte si le disque est presque plein."""
    days = float(STATE["settings"].get("retention_days") or 0)
    removed = []
    if days > 0:
        limit = time.time() - days * 86400
        for rid, r in list(STATE["recordings"].items()):
            if r["status"] in ("recording", "starting", "finalizing") or not r.get("ended"):
                continue
            try:
                ended = datetime.fromisoformat(r["ended"]).timestamp()
            except Exception:
                continue
            if ended < limit and delete_recording(rid):
                removed.append(r["username"])
    if removed:
        log.info("Nettoyage : %d vidéo(s) supprimée(s)", len(removed))
    free = shutil.disk_usage(DATA_DIR).free
    with _lock:
        warned = STATE.setdefault("flags", {}).get("disk_warned", False)
        if free < LOW_DISK and not warned:
            STATE["flags"]["disk_warned"] = True
            save_state()
            N.notify("disk", f"💾 TikLive : il ne reste que {fmt_size(free)} d'espace sur le serveur. "
                             f"Télécharge puis supprime quelques vidéos pour ne pas bloquer les prochains lives."
                             f"{N.link()}")
        elif free > LOW_DISK * 1.5 and warned:
            STATE["flags"]["disk_warned"] = False
            save_state()
    return removed


def _cleanup_loop():
    while True:
        try:
            cleanup_once()
        except Exception:
            log.exception("erreur pendant le nettoyage")
        time.sleep(3600)


def start_background():
    recover_after_restart()
    threading.Thread(target=_watch_loop, daemon=True).start()
    threading.Thread(target=_cleanup_loop, daemon=True).start()


def disk_info() -> dict:
    total, used, free = shutil.disk_usage(DATA_DIR)
    rec_bytes = sum(p.stat().st_size for p in REC_DIR.rglob("*") if p.is_file())
    return {"free": free, "total": total, "recordings": rec_bytes}
