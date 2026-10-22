"""Fonctions annexes du bot : score d'affaire, tailles, journal, sauvegardes, alertes de panne,
résumé du matin, boutons Telegram, canal ntfy, lecture des e-mails d'alertes (IMAP)."""
import csv
import email
import html
import imaplib
import json
import os
import re
import shutil
import statistics
import time
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
JOURNAL = BASE_DIR / "journal.csv"
BACKUP_DIR = BASE_DIR / "backups"


def now_s():
    return int(time.time())


# ---------------------------------------------------------------- prix / affaires
def model_key(title):
    """Clé de regroupement grossière : les 4 premiers mots « utiles » du titre."""
    words = re.findall(r"[a-z0-9\-]{3,}", (title or "").lower())
    stop = {"the", "and", "pour", "avec", "pas", "men", "homme", "size", "taille", "neuf", "very", "good"}
    words = [w for w in words if w not in stop and not w.isdigit()]
    return " ".join(words[:4])


def record_price(state, key, eur, keep=60):
    if eur is None or not key:
        return
    lst = state.setdefault("prices", {}).setdefault(key, [])
    lst.append(round(eur, 1))
    del lst[:-keep]


def median_for(state, key, min_samples=5):
    lst = state.get("prices", {}).get(key, [])
    return statistics.median(lst) if len(lst) >= min_samples else None


def deal_labels(eur, med, cfg):
    """Étiquettes (texte) selon le prix comparé à la médiane déjà vue pour ce modèle."""
    out = []
    if eur is None or not med:
        return out
    ratio = eur / med
    if ratio <= cfg.get("deal_ratio", 0.65):
        out.append(f"💎 Bonne affaire : {round((1 - ratio) * 100)} % sous le prix habituel (médiane ≈ {med:.0f} €)")
    if ratio < cfg.get("suspect_ratio", 0.3):
        out.append("⚠️ Prix très bas : méfiance (contrefaçon / arnaque possible)")
    resale = med * cfg.get("resale_factor", 0.85)
    if resale > eur * 1.25:
        out.append(f"💰 Revente estimée ≈ {resale:.0f} € (marge ≈ {resale - eur:.0f} €, estimation indicative)")
    return out


# ---------------------------------------------------------------- tailles
SIZE_ALIASES = {
    "xs": ["xs", "extra small"], "s": ["s", "small"], "m": ["m", "medium"], "l": ["l", "large"],
    "xl": ["xl", "extra large"], "xxl": ["xxl", "2xl"],
}


def size_ok(text, wanted):
    """True si aucune taille n'est détectable ou si l'une des tailles voulues apparaît."""
    if not wanted:
        return True
    t = " " + re.sub(r"[^a-z0-9/]+", " ", text.lower()) + " "
    all_tokens = {a for v in SIZE_ALIASES.values() for a in v}
    found_any = any(f" {a} " in t for a in all_tokens)
    for w in wanted:
        for a in SIZE_ALIASES.get(w.lower(), [w.lower()]):
            if f" {a} " in t:
                return True
    return not found_any  # taille non indiquée -> on garde, taille différente -> on écarte


# ---------------------------------------------------------------- détections de texte
LOT_WORDS = ["lot ", "bundle", "pack ", "job lot", "joblot", "wholesale", "lote ", "set of", "x2 ", "x3 ", "2x ", "3x "]


def is_lot(text):
    t = " " + text.lower() + " "
    return any(w in t for w in LOT_WORDS)


def fingerprint(item, eur):
    return f"{re.sub(r'[^a-z0-9]', '', (item['title'] or '').lower())[:40]}|{round(eur or 0)}"


# ---------------------------------------------------------------- journal / compteurs / sauvegardes
def journal_add(row):
    new = not JOURNAL.exists()
    with open(JOURNAL, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["date", "recherche", "site", "titre", "prix_eur", "niveau", "etiquettes", "lien"])
        w.writerow(row)


def bump(state, query):
    st = state.setdefault("stats", {})
    st[query] = st.get(query, 0) + 1


def daily_backup(files, keep=7):
    BACKUP_DIR.mkdir(exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    for f in files:
        f = Path(f)
        if not f.exists():
            continue
        dest = BACKUP_DIR / f"{f.stem}-{day}{f.suffix}"
        if not dest.exists():
            shutil.copy2(f, dest)
        for old in sorted(BACKUP_DIR.glob(f"{f.stem}-*{f.suffix}"))[:-keep]:
            old.unlink()


# ---------------------------------------------------------------- pannes
def update_health(state, sid, ok, cfg, notify):
    """Compte les passes en échec par site ; prévient une fois après `outage_hours`, puis au retour."""
    h = state.setdefault("health", {}).setdefault(sid, {"since": None, "alerted": False})
    if ok:
        if h["alerted"]:
            notify(f"✅ {sid} refonctionne.")
        h.update(since=None, alerted=False)
        return
    if h["since"] is None:
        h["since"] = now_s()
    hours = (now_s() - h["since"]) / 3600
    if hours >= cfg.get("outage_hours", 2) and not h["alerted"]:
        notify(f"🔧 {sid} ne répond plus depuis {hours:.0f} h (blocage ou changement du site).")
        h["alerted"] = True


# ---------------------------------------------------------------- résumé du matin
def digest_due(state, hour):
    today = datetime.now().strftime("%Y-%m-%d")
    return hour is not None and datetime.now().hour >= int(hour) and state.get("digest_day") != today


def build_digest(state, top=15):
    items = sorted(state.get("digest", []), key=lambda x: x.get("score", 9))[:top]
    lines = ["☀️ Résumé du jour"]
    if not items:
        lines.append("Rien de nouveau en priorité basse.")
    for it in items:
        lines.append(f"• {it['price']:.0f} € — {it['title'][:70]}\n  {it['url']}")
    stats = sorted(state.get("stats", {}).items(), key=lambda kv: -kv[1])[:8]
    if stats:
        lines.append("\n📊 Mots-clés les plus productifs : " + ", ".join(f"{q} ({n})" for q, n in stats))
    return "\n".join(lines)


# ---------------------------------------------------------------- ntfy (2e canal gratuit)
def ntfy_send(title, text, url=None):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return False
    headers = {"Title": title.encode("utf-8")[:200], "Priority": "high"}
    if url:
        headers["Click"] = url
    try:
        return requests.post(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), headers=headers, timeout=15).ok
    except requests.RequestException:
        return False


# ---------------------------------------------------------------- légende photo + boutons Telegram
def api(method, **payload):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def detect_brand(title, brands):
    """brands : {"Nike": ["nike"], "Stone Island": ["stone island", "stoneisland"]...}"""
    t = (title or "").lower()
    for name, words in brands.items():
        if any(w in t for w in words):
            return name
    return ""


def ago(ts):
    if not ts:
        return None
    mins = max(int((time.time() - float(ts)) / 60), 0)
    if mins < 60:
        return f"{mins} min"
    if mins < 1440:
        return f"{mins // 60} h"
    return f"{mins // 1440} j"


def caption(item, brand, eur, med, pepite, labels, price_txt):
    """Légende HTML courte et identique pour toutes les annonces (< 1024 caractères)."""
    e = html.escape
    title = (item["title"] or "").strip()[:110]
    head = f"<b>{e(brand.upper() + ' – ' if brand else '')}{e(title)}</b>"
    badges = []
    if eur and med and eur < med * 0.9:
        badges.append(f"🔥 -{round((1 - eur / med) * 100)} % sous le marché")
    if pepite:
        badges.append("💎 Pépite")
    lines = [head]
    if badges:
        lines.append(" · ".join(badges))
    lines.append(f"💶 {e(price_txt)}")
    if item.get("size"):
        lines.append(f"📏 {e(str(item['size']))}")
    if item.get("state"):
        lines.append(f"✨ {e(str(item['state']))}")
    if item.get("seller"):
        r = f" {item['rating']}/5" if item.get("rating") else ""
        n = f" ({item['reviews']} avis)" if item.get("reviews") else ""
        lines.append(f"⭐ {e(str(item['seller']))}{r}{n}")
    a = ago(item.get("posted"))
    if a:
        lines.append(f"🕒 En ligne il y a {a}")
    for l in labels:  # alertes supplémentaires (lot, baisse de prix, prix suspect...)
        if not l.startswith(("💎", "⭐ Pépite", "🔥")):
            lines.append(e(l))
    lines.append(f"📍 {e(item.get('source', ''))}")
    return "\n".join(lines)[:1000]


def remember(state, item, brand):
    """Garde de quoi traiter les boutons (photos, vendeur, modèle) sans tout mettre dans le bouton."""
    meta = state.setdefault("meta", {})
    meta[item["id"]] = {"model": model_key(item["title"])[:40], "seller": item.get("seller") or "",
                        "photos": (item.get("photos") or [])[:10], "title": item["title"],
                        "url": item["url"], "brand": brand}
    for k in list(meta)[:-400]:
        del meta[k]


def keyboard(item):
    url = item.get("url", "")
    if not url.startswith("http"):
        return None
    cb = lambda x: f"{x}:{item['id']}"[:64]
    return {"inline_keyboard": [
        [{"text": "🛒 Acheter", "url": url}, {"text": "🔗 Voir l'annonce", "url": url}],
        [{"text": "💬 Contacter le vendeur", "url": url}, {"text": "⭐ Favori", "callback_data": cb("fav")}],
        [{"text": "📷 Photos", "callback_data": cb("ph")}, {"text": "🚫 Ignorer", "callback_data": cb("ig")}],
    ]}


def process_callbacks(state):
    """Traite les clics (favori, photos, ignorer) faits depuis la dernière passe."""
    res = api("getUpdates", offset=state.get("tg_offset", 0), timeout=0, allowed_updates=["callback_query"])
    if not res or not res.get("ok"):
        return
    ig = state.setdefault("ignore", {"models": [], "sellers": []})
    meta = state.get("meta", {})
    for u in res["result"]:
        state["tg_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        kind, _, iid = cq.get("data", "").partition(":")
        m = meta.get(iid)
        answer = "Je n'ai plus cette annonce en mémoire."
        chat = (cq.get("message") or {}).get("chat", {}).get("id")
        if m and kind == "ig":
            if m["model"] and m["model"] not in ig["models"]:
                ig["models"].append(m["model"])
            if m["seller"] and m["seller"] not in ig["sellers"]:
                ig["sellers"].append(m["seller"])
            answer = "🚫 Noté : ce modèle (et ce vendeur) ne seront plus signalés."
        elif m and kind == "fav":
            favs = state.setdefault("favs", [])
            if iid not in favs:
                favs.append(iid)
            answer = "⭐ Ajouté aux favoris."
        elif m and kind == "ph":
            photos = m.get("photos", [])
            if len(photos) > 1 and chat:
                api("sendMediaGroup", chat_id=chat, media=[{"type": "photo", "media": p} for p in photos[1:10]])
                answer = "📷 Photos envoyées."
            else:
                answer = "Une seule photo disponible pour cette annonce."
        api("answerCallbackQuery", callback_query_id=cq["id"], text=answer)


def ignored(state, item):
    ig = state.get("ignore", {})
    return (model_key(item["title"])[:40] in ig.get("models", [])
            or (item.get("seller") and item["seller"] in ig.get("sellers", [])))


# ---------------------------------------------------------------- e-mails d'alertes (Leboncoin, Vestiaire, Depop, Wallapop, Facebook...)
URL_RE = re.compile(r"https?://[^\s<>\"')]+")


def _decode(s):
    out = ""
    for part, enc in decode_header(s or ""):
        out += part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part
    return out


def mail_items(cfg):
    """Lit les e-mails non lus des expéditeurs listés et renvoie des annonces {id,title,url,...}.
    Variables : IMAP_HOST, IMAP_USER, IMAP_PASSWORD (mot de passe d'application)."""
    host, user, pw = os.getenv("IMAP_HOST"), os.getenv("IMAP_USER"), os.getenv("IMAP_PASSWORD")
    if not (host and user and pw):
        return []
    senders = [s.lower() for s in cfg.get("senders", [])]
    out = []
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, pw)
        M.select("INBOX")
        _, data = M.search(None, "UNSEEN")
        for num in data[0].split()[-50:]:
            _, msg_data = M.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            sender = _decode(msg.get("From", "")).lower()
            if senders and not any(s in sender for s in senders):
                continue
            subject = _decode(msg.get("Subject", ""))
            body = ""
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    body += (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            urls = [u for u in URL_RE.findall(body) if not re.search(r"unsubscribe|desabonn|\.(png|jpg|gif)", u, re.I)]
            if urls:
                out.append({"id": f"mail:{msg.get('Message-ID', num.decode())}", "title": subject,
                            "price": None, "currency": "EUR", "url": urls[0],
                            "extra": f"✉️ via alerte e-mail ({sender[:40]})", "source": "email"})
            M.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out
