"""Notifications WhatsApp via CallMeBot (gratuit, usage perso)."""

import logging
import os
import threading

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


def _send_all(text: str):
    send_ntfy(text)
    send_whatsapp(text)


def notify(event: str, text: str):
    """Envoie en arrière-plan (téléphone + WhatsApp), sans jamais bloquer l'enregistrement."""
    s = _settings()
    if not s.get(f"notify_{event}", True):
        return
    threading.Thread(target=_send_all, args=(text,), daemon=True).start()


def link() -> str:
    return f"\n👉 {BASE_URL}" if BASE_URL else ""
