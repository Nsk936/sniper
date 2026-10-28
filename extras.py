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
    prices = state.setdefault("prices", {})
    lst = prices.pop(key, [])  # on le remet en fin de dictionnaire : les plus anciens sortent d'abord
    lst.append(round(eur, 1))
    del lst[:-30]
    prices[key] = lst
    for k in list(prices)[:max(len(prices) - 3000, 0)]:  # évite que seen.json grossisse sans fin
        del prices[k]


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


def pepite_score(eur, med, highlighted, tier, item, rare=False, bonus=0):
    """Note /100 : remise vs marché + mots-clés rares + recherche prioritaire + état + vendeur."""
    score = 0
    if eur and med and eur < med:
        score += min(50, round((1 - eur / med) * 100))
    if highlighted:
        score += 45
    if tier == "top":
        score += 15
    if rare:
        score += 5
    st = (item.get("state") or "").lower()
    if any(w in st for w in ("neuf", "new", "nieuw", "nuevo", "nuovo")):
        score += 5
    if (item.get("rating") or 0) >= 4.7:
        score += 5
    return max(0, min(score + bonus, 100))


def caption(item, brand, eur, med, score, pepite, labels, price_txt):
    """Légende HTML courte et identique pour toutes les annonces (< 1024 caractères)."""
    e = html.escape
    title = (item["title"] or "").strip()[:110]
    lines = [f"<b>{e(brand.upper() + ' – ' if brand else '')}{e(title)}</b>", ""]
    badges = []
    if eur and med and eur < med * 0.9:
        badges.append(f"🔥 -{round((1 - eur / med) * 100)} % sous le marché")
    if pepite:
        badges.append(f"💎 Pépite {score}/100")
    if badges:
        lines += ["  ·  ".join(badges), ""]
    lines.append(f"💶 Prix · <b>{e(price_txt)}</b>")
    ts = []
    if item.get("size"):
        ts.append(f"📏 Taille · {e(str(item['size']))}")
    if item.get("state"):
        ts.append(f"✨ État · {e(str(item['state']))}")
    if ts:
        lines.append("   ".join(ts))
    if item.get("seller"):
        r = f"{item['rating']}/5".replace(".", ",") if item.get("rating") else ""
        n = f" ({item['reviews']} avis)" if item.get("reviews") else ""
        lines.append(f"⭐ Vendeur · {e(str(item['seller']))}" + (f" {r}" if r else "") + n)
    a = ago(item.get("posted"))
    if a:
        lines.append(f"🕒 En ligne · il y a {a}")
    extra = [l for l in labels if not l.startswith(("💎", "⭐ Pépite", "🔥"))]
    if extra:  # alertes supplémentaires (lot, baisse de prix, prix suspect...)
        lines += [""] + [e(l) for l in extra]
    return "\n".join(lines)[:1000]


def remember(state, item, brand):
    """Garde de quoi traiter les boutons (photos, vendeur, modèle) sans tout mettre dans le bouton."""
    meta = state.setdefault("meta", {})
    meta[item["id"]] = {"model": model_key(item["title"])[:40], "seller": item.get("seller") or "",
                        "photos": (item.get("photos") or [])[:10], "title": item["title"],
                        "url": item["url"], "brand": brand, "price": item.get("price"),
                        "currency": item.get("currency", "EUR"), "lang": lang_of(item.get("source", "")),
                        "eur": item.get("eur")}
    for k in list(meta)[:-400]:
        del meta[k]


def keyboard(item):
    url = item.get("url", "")
    if not url.startswith("http"):
        return None
    cb = lambda x: f"{x}:{item['id']}"[:64]
    return {"inline_keyboard": [
        [{"text": "🛒 Acheter", "url": url}, {"text": "🔗 Voir l'annonce", "url": url}],
        [{"text": "💬 Message tout prêt", "callback_data": cb("msg")}, {"text": "⭐ Favori", "callback_data": cb("fav")}],
        [{"text": "📷 Photos", "callback_data": cb("ph")}, {"text": "🚫 Ignorer", "callback_data": cb("ig")}],
    ]}


def process_callbacks(state):
    """Traite les clics (favori, photos, ignorer, message) et les commandes tapées dans le chat autorisé."""
    res = api("getUpdates", offset=state.get("tg_offset", 0), timeout=0,
              allowed_updates=["callback_query", "message"])
    if not res or not res.get("ok"):
        return
    ig = state.setdefault("ignore", {"models": [], "sellers": []})
    meta = state.get("meta", {})
    allowed_chat = str(os.getenv("TELEGRAM_CHAT_ID", ""))
    for u in res["result"]:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message")
        if msg and str(msg.get("chat", {}).get("id")) == allowed_chat and str(msg.get("text", "")).startswith("/"):
            reply = handle_command(state, msg["text"])
            if reply:
                extra = {"message_thread_id": msg["message_thread_id"]} if msg.get("message_thread_id") else {}
                api("sendMessage", chat_id=msg["chat"]["id"], text=reply, **extra)
            continue
        cq = u.get("callback_query")
        if not cq or str((cq.get("message") or {}).get("chat", {}).get("id")) != allowed_chat:
            continue  # on n'obéit qu'au chat configuré
        kind, _, iid = cq.get("data", "").partition(":")
        m = meta.get(iid)
        answer = "Je n'ai plus cette annonce en mémoire."
        chat = cq["message"]["chat"]["id"]
        thread = cq["message"].get("message_thread_id")
        extra = {"message_thread_id": thread} if thread else {}
        if m and kind == "ig":
            if m["model"] and m["model"] not in ig["models"]:
                ig["models"].append(m["model"])
            if m["seller"] and m["seller"] not in ig["sellers"]:
                ig["sellers"].append(m["seller"])
            learn(state, m.get("brand", ""), m["model"], -2)
            answer = "🚫 Noté : ce modèle (et ce vendeur) ne seront plus signalés."
        elif m and kind == "fav":
            favs = state.get("favs")
            if not isinstance(favs, dict):  # ancien format (liste d'identifiants)
                favs = state["favs"] = {i: {} for i in (favs or [])}
            favs[iid] = {"url": m["url"], "title": m["title"], "eur": m.get("eur"), "t": now_s()}
            learn(state, m.get("brand", ""), m["model"], +2)
            answer = "⭐ Ajouté aux favoris (je te préviens s'il baisse ou disparaît)."
        elif m and kind == "ph":
            photos = m.get("photos", [])
            if len(photos) > 1:
                api("sendMediaGroup", chat_id=chat, media=[{"type": "photo", "media": p} for p in photos[1:10]], **extra)
                answer = "📷 Photos envoyées."
            else:
                answer = "Une seule photo disponible pour cette annonce."
        elif m and kind == "msg":
            api("sendMessage", chat_id=chat, parse_mode="HTML", **extra,
                text="💬 Message à copier (appui long) :\n<code>" + html.escape(offer_message(m)) + "</code>\n" + m["url"])
            answer = "💬 Message envoyé."
        api("answerCallbackQuery", callback_query_id=cq["id"], text=answer)


def ignored(state, item):
    ig = state.get("ignore", {})
    title = (item["title"] or "").lower()
    return (model_key(item["title"])[:40] in ig.get("models", [])
            or any(w in title for w in ig.get("words", []))
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


# ================================================================== v4 : nouvelles fonctions
# ---------------------------------------------------------------- prix dynamique / marchés / frais
def dynamic_ok(eur, med, cfg):
    """Annonce au-dessus du budget mais très en dessous du prix du marché (ex. 95 € pour une pièce à 250 €)."""
    d = cfg.get("dynamic_max", {})
    if not d.get("enabled", True) or not (eur and med):
        return False
    return eur <= med * float(d.get("ratio", 0.45)) and eur <= float(d.get("cap_eur", 250))


def record_market(state, mkey, market, eur, keep=20):
    if eur is None or not mkey:
        return
    pm = state.setdefault("pm", {})
    k = f"{mkey}@{market}"
    lst = pm.pop(k, [])
    lst.append(round(eur, 1))
    del lst[:-keep]
    pm[k] = lst
    for old in list(pm)[:max(len(pm) - 3000, 0)]:
        del pm[old]


def market_label(state, mkey, market, eur, ratio=1.4, min_samples=5):
    """Le même modèle se vend nettement plus cher sur un autre marché ?"""
    if not (mkey and eur):
        return None
    best = None
    for k, lst in state.get("pm", {}).items():
        m, _, mk = k.rpartition("@")
        if m != mkey or mk == market or len(lst) < min_samples:
            continue
        med = statistics.median(lst)
        if med >= eur * ratio and (not best or med > best[0]):
            best = (med, mk)
    return f"🌍 Se vend ≈ {best[0]:.0f} € sur {best[1]} (écart ≈ {best[0] - eur:.0f} €)" if best else None


def fees_label(item, eur):
    """Vinted : protection acheteur ≈ 0,70 € + 5 % (estimation, hors livraison)."""
    if str(item.get("source", "")).startswith("vinted") and item.get("currency") == "EUR" and eur:
        return f"🧾 Avec protection acheteur ≈ {eur * 1.05 + 0.7:.2f} € (hors livraison)".replace(".", ",")
    return None


# ---------------------------------------------------------------- vendeurs
def seller_check(item, cfg):
    """(écarter ?, étiquettes). Filtre configurable : seller_min_rating, étiquette vendeur pro / sans avis."""
    labels = []
    rating, reviews = item.get("rating"), item.get("reviews")
    if item.get("pro"):
        labels.append("🏪 Vendeur professionnel")
    if rating is not None and reviews and int(reviews) >= 3 and rating < float(cfg.get("seller_min_rating", 3.5)):
        return True, labels
    if item.get("seller") and (reviews in (0, "0")):
        labels.append("🆕 Vendeur sans avis")
    return False, labels


# ---------------------------------------------------------------- apprentissage (favoris / ignorés)
def pref_bonus(state, brand, mkey):
    p = state.get("prefs", {})
    return max(-15, min(15, p.get("b:" + brand, 0) + p.get("m:" + mkey, 0)))


def learn(state, brand, mkey, delta):
    p = state.setdefault("prefs", {})
    if brand:
        p["b:" + brand] = max(-10, min(10, p.get("b:" + brand, 0) + delta))
    if mkey:
        p["m:" + mkey] = max(-10, min(10, p.get("m:" + mkey, 0) + delta * 2))


# ---------------------------------------------------------------- doublons par photo (nécessite Pillow)
def dhash(data, size=8):
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data)).convert("L").resize((size + 1, size))
    except Exception:
        return None
    px = list(img.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
    return bits


def dup_check(state, item, eur, max_dist=4, price_tol=0.15):
    """True si une annonce déjà envoyée a la même photo (à peu près) et le même prix (± 15 %)."""
    if not item.get("photo"):
        return False
    try:
        r = requests.get(item["photo"], timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        h = dhash(r.content) if r.ok and len(r.content) < 4_000_000 else None
    except requests.RequestException:
        return False
    if h is None:
        return False
    ph = state.setdefault("ph", [])
    for oh, oeur, oid in ph:
        if oid != item["id"] and bin(oh ^ h).count("1") <= max_dist and (
                not eur or not oeur or abs(oeur - eur) <= price_tol * max(eur, oeur)):
            return True
    ph.append([h, eur or 0, item["id"]])
    del ph[:-1500]
    return False


# ---------------------------------------------------------------- message de négociation
OFFER = {
    "fr": "Bonjour, votre annonce « {t} » est-elle toujours disponible ? Je vous propose {o} et je peux régler tout de suite. Merci !",
    "en": "Hi, is « {t} » still available? I can offer {o} and pay right away. Thanks!",
    "de": "Hallo, ist „{t}“ noch verfügbar? Ich biete {o} und zahle sofort. Danke!",
    "it": "Ciao, «{t}» è ancora disponibile? Ti propongo {o} e pago subito. Grazie!",
    "es": "Hola, ¿sigue disponible «{t}»? Te ofrezco {o} y pago ahora mismo. ¡Gracias!",
    "pt": "Olá, o artigo «{t}» ainda está disponível? Proponho {o} e pago já. Obrigado!",
    "pl": "Dzień dobry, czy „{t}” jest nadal dostępne? Proponuję {o} i płacę od razu. Dziękuję!",
    "nl": "Hoi, is «{t}» nog beschikbaar? Ik bied {o} en betaal direct. Bedankt!",
}
LANG_BY_TLD = {"fr": "fr", "be": "fr", "lu": "fr", "de": "de", "at": "de", "it": "it", "es": "es", "pt": "pt",
               "pl": "pl", "nl": "nl", "ie": "en", "co.uk": "en", "gb": "en", "com": "en", "us": "en", "ca": "en"}


def lang_of(sid):
    kind, _, arg = sid.partition(":")
    if kind in ("marktplaats", "2dehands"):
        return "nl"
    if kind in ("2ememain",):
        return "fr"
    if kind == "willhaben":
        return "de"
    if kind == "subito":
        return "it"
    return LANG_BY_TLD.get(arg, "en")


def offer_message(meta, discount=0.15):
    price = meta.get("price")
    cur = meta.get("currency", "EUR")
    amount = f"{round(price * (1 - discount))} {'€' if cur == 'EUR' else cur}" if price else "un prix correct"
    return OFFER.get(meta.get("lang", "en"), OFFER["en"]).format(t=meta.get("title", "")[:80], o=amount)


# ---------------------------------------------------------------- bilans
def day_stats(state):
    return state.setdefault("day", {"scanned": 0, "alerts": 0})


def week_add(state, title, eur, score, url):
    w = state.setdefault("week", [])
    w.append({"t": title[:80], "p": round(eur or 0), "s": score, "u": url})
    w.sort(key=lambda x: -x["s"])
    del w[30:]


def build_report(state):
    d = state.get("day", {})
    down = [s for s, h in state.get("health", {}).items() if h.get("since")]
    txt = [f"🤖 Bilan : {d.get('scanned', 0)} annonces examinées, {d.get('alerts', 0)} alertes envoyées."]
    txt.append("Sites en difficulté : " + (", ".join(down) if down else "aucun"))
    return "\n".join(txt)


def weekly_due(state):
    today = datetime.now().strftime("%Y-%m-%d")
    return datetime.now().weekday() == 0 and state.get("week_day") != today


def build_weekly(state):
    w = state.get("week", [])[:5]
    if not w:
        return None
    return "🏆 Meilleures affaires de la semaine\n" + "\n".join(
        f"• {x['s']}/100 — {x['p']} € — {x['t']}\n  {x['u']}" for x in w)


# ---------------------------------------------------------------- commandes Telegram
HELP = ("Commandes : /pause · /reprendre · /stats · /ignorer mot · /taille M L (ou /taille tout) · /aide")


def handle_command(state, text):
    """Renvoie la réponse à envoyer (ou None). Les commandes viennent seulement du chat autorisé."""
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    ig = state.setdefault("ignore", {"models": [], "sellers": []})
    if cmd == "/pause":
        state["paused"] = True
        return "⏸️ Bot en pause (plus d'alertes). /reprendre pour relancer."
    if cmd in ("/reprendre", "/start"):
        state["paused"] = False
        return "▶️ Bot relancé."
    if cmd == "/stats":
        return build_report(state)
    if cmd == "/ignorer" and args:
        w = " ".join(args).lower()
        ig.setdefault("words", [])
        if w not in ig["words"]:
            ig["words"].append(w)
        return f"🚫 Les annonces contenant « {w} » seront ignorées."
    if cmd == "/taille":
        if not args or args[0].lower() == "tout":
            state["sizes"] = []
            return "📏 Filtre de taille désactivé."
        state["sizes"] = [a.upper() for a in args]
        return f"📏 Tailles gardées : {', '.join(state['sizes'])} (annonces sans taille indiquée gardées)."
    if cmd in ("/aide", "/help"):
        return HELP
    return None
