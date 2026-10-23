"""Notifications WhatsApp via CallMeBot (gratuit, usage perso)."""

import logging
import os
import random
import threading
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("tiklive.notify")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")  # fourni automatiquement par Render


def _settings():
    import recorder as R
    return R.STATE.setdefault("settings", {})


def send_whatsapp(text: str, phone: str | None = None, apikey: str | None = None) -> tuple[bool, str]:
    s = _settings()
    phone = (phone or s.get("wa_phone") or os.environ.get("WA_PHONE", "")).strip().replace(" ", "")
    apikey = (apikey or s.get("wa_apikey") or os.environ.get("WA_APIKEY", "")).strip()
    if not phone or not apikey:
        return False, "WhatsApp n'est pas encore configuré."
    if not phone.startswith("+"):
        phone = "+" + phone
    try:
        r = httpx.get("https://api.callmebot.com/whatsapp.php",
                      params={"phone": phone, "text": text, "apikey": apikey}, timeout=30)
        body = r.text[:300]
        ok = r.status_code == 200 and "error" not in body.lower()
        if not ok:
            log.warning("CallMeBot a refusé le message: %s %s", r.status_code, body)
        return ok, ("Message envoyé ✅" if ok else f"Refusé par CallMeBot : {body}")
    except Exception as e:
        log.warning("WhatsApp injoignable: %s", e)
        return False, f"Impossible de joindre CallMeBot : {e}"


def send_ntfy(text: str, topic: str | None = None) -> tuple[bool, str]:
    """Notification sur le téléphone via l'appli gratuite ntfy (sans compte)."""
    topic = (topic or _settings().get("ntfy_topic") or "").strip()
    if not topic:
        return False, "Notifications téléphone non configurées."
    lines = text.split("\n", 1)
    payload = {"topic": topic, "title": lines[0][:120], "message": lines[1] if len(lines) > 1 else lines[0],
               "tags": ["red_circle"] if "🔴" in text else (["white_check_mark"] if "✅" in text else [])}
    if BASE_URL:
        payload["click"] = BASE_URL
    for word in text.split():
        if word.startswith("https://drive.google.com"):
            payload["click"] = word
    try:
        r = httpx.post("https://ntfy.sh/", json=payload, timeout=30)
        ok = r.status_code == 200
        return ok, ("Notification envoyée ✅ Regarde ton téléphone." if ok else f"Refusé par ntfy : {r.text[:200]}")
    except Exception as e:
        return False, f"Impossible de joindre ntfy : {e}"


# ntfy.sh limite le nombre de messages par adresse IP. Le serveur Render partage son IP avec
# d'autres clients, qui peuvent épuiser cette limite : on réessaie donc pendant plusieurs heures
# au lieu d'abandonner au premier refus (erreur 429).
RETRY_DELAYS = [10, 30, 60, 120, 300, 600, 900, 1800, 1800, 3600, 3600, 3600]  # ≈ 4 h au total
HISTORY_MAX = 40


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _history_add(entry: dict) -> dict:
    import recorder as R
    with R._lock:
        h = R.STATE.setdefault("notifications", [])
        h.insert(0, entry)
        del h[HISTORY_MAX:]
        R.save_state()
    return entry


def _history_update(entry: dict, **fields):
    import recorder as R
    with R._lock:
        entry.update(fields)
        R.save_state()


def _deliver_phone(text: str, entry: dict):
    for attempt, delay in enumerate([0] + RETRY_DELAYS):
        if delay:
            time.sleep(delay + random.uniform(0, delay * 0.2))
        ok, msg = send_ntfy(text)
        if ok:
            late = f" (après {attempt + 1} essais)" if attempt else ""
            _history_update(entry, phone="ok", phone_at=_now(), phone_note=f"Reçue sur ton téléphone{late}")
            return
        if "non configurées" in msg:
            _history_update(entry, phone="off", phone_note="Notifications téléphone non configurées")
            return
        log.warning("ntfy refusé (essai %d) : %s", attempt + 1, msg)
        _history_update(entry, phone="retry", phone_note=f"Service saturé, nouvel essai en cours ({attempt + 1})")
    _history_update(entry, phone="failed", phone_note="Pas pu être envoyée après 4 h d'essais")


def _send_all(text: str, entry: dict):
    threading.Thread(target=_deliver_phone, args=(text, entry), daemon=True).start()
    ok, _ = send_whatsapp(text)
    if ok:
        _history_update(entry, whatsapp="ok")


def notify(event: str, text: str):
    """Envoie en arrière-plan (téléphone + WhatsApp), sans jamais bloquer l'enregistrement.
    Chaque notification est aussi gardée dans l'historique visible dans l'outil."""
    s = _settings()
    entry = {"at": _now(), "event": event, "text": text, "phone": "pending", "phone_note": "Envoi en cours"}
    _history_add(entry)
    if not s.get(f"notify_{event}", True):
        _history_update(entry, phone="off", phone_note="Désactivée dans tes réglages")
        return
    threading.Thread(target=_send_all, args=(text, entry), daemon=True).start()


def link() -> str:
    return f"\n👉 {BASE_URL}" if BASE_URL else ""
