#!/usr/bin/env python3
"""Bot d'alertes d'annonces (Vinted, eBay, OLX, Marktplaats, ...) -> notifications Telegram.

Usage :
    python sniper.py                    # une passe
    python sniper.py --loop 10          # repasse toutes les 10 minutes
    python sniper.py --dry-run          # affiche sans envoyer ni enregistrer
    python sniper.py --check            # teste chaque site (OK / KO) avec une recherche "nike"
    python sniper.py --only vinted,olx  # limite à certains sites (préfixes d'identifiants)
    python sniper.py --test-telegram    # envoie un message de test

Configuration : config.json (recherches, groupes de sites) + variables d'environnement (secrets).
"""
import argparse
import base64
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

import extras

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "seen.json"
MAX_SEEN = 20000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class SourceDisabled(Exception):
    """Source inutilisable pour cette passe (clé manquante, module absent...)."""


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"Fichier illisible, ignoré : {path.name}")
    return default


def pause(lo=1.5, hi=3.5):
    time.sleep(random.uniform(lo, hi))


def parse_price(value):
    """'1 200 Kč' -> 1200.0 ; '45,50 €' -> 45.5 ; 'VB' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d[\d\s., ]*", str(value))
    if not m:
        return None
    s = m.group(0).strip().replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + "." + tail if len(tail) <= 2 else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        if len(tail) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def to_eur(price, currency, fx):
    if price is None:
        return None
    rate = (fx or {}).get(currency or "EUR", 1.0 if (currency or "EUR") == "EUR" else None)
    return None if rate is None else price * rate


def soup_of(html):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise SourceDisabled("module manquant : pip install beautifulsoup4")
    return BeautifulSoup(html, "html.parser")


def telegram_send(text, markup=None, silent=False, photo=None, html=False, thread=None):
    """Envoie un message (avec photo si possible) ; réessaie en cas de coupure réseau.
    Si la photo ne charge pas, renvoie le message sans photo. Renvoie True si envoyé."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("Telegram non configuré (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        print(text)
        return False
    base = {"chat_id": chat_id}
    if markup:
        base["reply_markup"] = markup
    if silent:
        base["disable_notification"] = True
    if thread:
        base["message_thread_id"] = thread
    if html:
        base["parse_mode"] = "HTML"

    def post(method, payload):
        """Renvoie (ok, status). Gère les coupures réseau et la limite d'envoi (429)."""
        r = None
        for attempt in range(4):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
            except requests.RequestException as e:  # coupure réseau, connexion réinitialisée...
                log(f"Telegram : erreur réseau ({type(e).__name__}), nouvel essai")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 429:  # limite d'envoi : on attend le délai demandé
                try:
                    time.sleep(min(int(r.json()["parameters"]["retry_after"]) + 1, 60))
                except (KeyError, ValueError, TypeError):
                    time.sleep(5)
                continue
            if not r.ok:
                log(f"Échec Telegram {method} : {r.status_code} {r.text[:160]}")
                return False, r.status_code
            time.sleep(0.5)
            return True, 200
        return False, (r.status_code if r is not None else 0)

    if photo:
        ok, _ = post("sendPhoto", {**base, "photo": photo, "caption": text[:1024]})
        if ok:
            return True
        try:  # Telegram n'a pas pu lire l'adresse : on télécharge la photo nous-mêmes et on l'envoie
            img = requests.get(photo, timeout=15, headers={"User-Agent": USER_AGENT, "Referer": photo})
            if img.ok and img.content[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb", b"\x89PNG", b"RIFF"):
                data = {k: (json.dumps(v) if isinstance(v, dict) else str(v)) for k, v in base.items()}
                data["caption"] = text[:1024]
                r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data,
                                  files={"photo": ("photo.jpg", img.content)}, timeout=30)
                if r.ok:
                    return True
        except requests.RequestException:
            pass
        log("Photo refusée : envoi du message sans photo")
    ok, _ = post("sendMessage", {**base, "text": text[:4000], "disable_web_page_preview": bool(photo)})
    return ok


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
class Source:
    """Un site. Sous-classes : implémenter search(query) qui renvoie des annonces."""

    def __init__(self, sid, currency="EUR"):
        self.id = sid
        self.currency = currency
        try:
            # Imite l'empreinte réseau de Chrome : indispensable contre les erreurs 403 anti-bot.
            from curl_cffi import requests as cffi_requests
            self.session = cffi_requests.Session(impersonate="chrome")
            self.session.headers.update({"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
        except ImportError:
            self.session = requests.Session()
            self.session.headers.update(
                {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
            )

    def get(self, url, **kw):
        kw.setdefault("timeout", 20)
        r = self.session.get(url, **kw)
        self.ok(r)
        return r

    @staticmethod
    def ok(r):
        """Comme raise_for_status, mais avec l'adresse et le début de la réponse (pour diagnostiquer)."""
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} sur {str(r.url)[:80]} -> {r.text[:100]!r}")

    def item(self, raw_id, title, price, currency, url, extra="", **meta):
        """meta facultatif : photo, photos (liste), size, state, seller, rating, reviews, posted (epoch)."""
        return {**{k: v for k, v in meta.items() if v not in (None, "", [])},
            "id": f"{self.id}:{raw_id}",
            "title": title or "",
            "price": price,
            "currency": currency or self.currency,
            "url": url,
            "extra": extra,
            "source": self.id,
        }

    def search(self, query):
        raise NotImplementedError


VINTED_KR = {"se": "SEK", "dk": "DKK"}


class Vinted(Source):
    """API interne du site (non officielle, peut casser). Même API sur tous les pays."""

    def __init__(self, domain="fr"):
        super().__init__(f"vinted:{domain}")
        self.base = f"https://www.vinted.{domain}"
        self.ready = False
        self.api_works = True

    def _init_session(self):
        self.get(self.base)
        self.ready = True

    API_HEADERS = {"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}

    def _api(self, query):
        params = {"search_text": query, "order": "newest_first", "per_page": 30, "page": 1}
        url = f"{self.base}/api/v2/catalog/items"
        headers = dict(self.API_HEADERS, Referer=f"{self.base}/catalog?search_text={query}")
        r = self.session.get(url, params=params, headers=headers, timeout=20)
        if r.status_code in (401, 403, 404):  # session expirée : on se reconnecte une fois
            self._init_session()
            r = self.session.get(url, params=params, headers=headers, timeout=20)
        self.ok(r)
        for it in r.json().get("items", []):
            price, currency = it.get("price"), it.get("currency")
            if isinstance(price, dict):
                currency = price.get("currency_code", currency)
                price = price.get("amount")
            extra = " · ".join(
                x for x in (it.get("size_title"), it.get("status"), it.get("brand_title")) if x
            )
            ph = it.get("photo") or {}
            user = it.get("user") or {}
            rep_ = user.get("feedback_reputation")
            yield self.item(
                it["id"], it.get("title"), parse_price(price), currency or "EUR",
                it.get("url") or f"{self.base}/items/{it['id']}", extra,
                photo=ph.get("full_size_url") or ph.get("url"),
                photos=[ph.get("full_size_url") or ph.get("url")] if ph else None,
                size=it.get("size_title"), state=it.get("status"), seller=user.get("login"),
                rating=round(float(rep_) * 5, 1) if isinstance(rep_, (int, float)) and rep_ <= 1 else None,
                reviews=user.get("feedback_count") or user.get("item_count"),
                posted=((ph.get("high_resolution") or {}).get("timestamp")),
            )

    def _html(self, query):
        """Plan B : lit les cartes d'annonces de la page de recherche (title="... 12,00 €")."""
        r = self.session.get(f"{self.base}/catalog", params={"search_text": query,
                             "order": "newest_first"}, timeout=20)
        self.ok(r)
        seen = set()
        for a in soup_of(r.text).select('a[href*="/items/"]'):
            m = re.search(r"/items/(\d+)", a.get("href", ""))
            title = a.get("title") or ""
            if not m or m.group(1) in seen or not title:
                continue
            seen.add(m.group(1))
            pm = re.findall(r"(\d[\d\s.,]*)\s?(€|£|\$|zł|Kč|lei|Ft|kr)", title)
            price = parse_price(pm[-1][0]) if pm else None
            cur = {"€": "EUR", "£": "GBP", "$": "USD", "zł": "PLN", "Kč": "CZK", "lei": "RON",
                   "Ft": "HUF", "kr": VINTED_KR.get(self.id.split(":")[1], "SEK")
                   }.get(pm[-1][1] if pm else "", "EUR")
            photo = None
            node = a
            for _ in range(4):  # la photo est parfois dans un élément voisin du lien
                img = node.find("img") if node else None
                if img:
                    photo = (img.get("src") or img.get("data-src") or (img.get("srcset") or "").split(" ")[0]) or None
                    if photo:
                        break
                node = node.parent
            yield self.item(m.group(1), title.split(", ")[0], price, cur,
                            urljoin(self.base, a["href"]), title, photo=photo,
                            photos=[photo] if photo else None)

    def search(self, query):
        if not self.ready:
            self._init_session()
        items = None
        if self.api_works:
            try:
                items = list(self._api(query))
            except Exception as api_err:
                if "429" in str(api_err):  # trop de requêtes : inutile d'insister avec le plan B
                    raise
                self.api_works = False  # l'API ne marche pas ici : on passe à la page web
                log(f"[{self.id}] API indisponible ({str(api_err)[:60]}), lecture de la page web")
        if items is None:
            items = list(self._html(query))
        for it in items:  # même identifiant d'un pays à l'autre : pas de doublons
            raw = it["id"].rsplit(":", 1)[-1]
            it["id"] = f"vinted:{raw}"
            yield it


EBAY = {"us": ("EBAY_US", "USD"), "gb": ("EBAY_GB", "GBP"), "de": ("EBAY_DE", "EUR"),
        "fr": ("EBAY_FR", "EUR"), "it": ("EBAY_IT", "EUR"), "es": ("EBAY_ES", "EUR"),
        "nl": ("EBAY_NL", "EUR"), "at": ("EBAY_AT", "EUR"), "ie": ("EBAY_IE", "EUR"),
        "ca": ("EBAY_CA", "CAD")}


def ebay_meta(it):
    """Photo, vendeur, note et date de mise en ligne d'un résumé d'annonce eBay."""
    seller = it.get("seller") or {}
    imgs = [it.get("image", {}).get("imageUrl")] + [i.get("imageUrl") for i in it.get("additionalImages", [])]
    imgs = [i for i in imgs if i]
    posted = None
    try:
        posted = datetime.strptime(it["itemCreationDate"][:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (KeyError, ValueError):
        pass
    pct = seller.get("feedbackPercentage")
    return dict(photo=imgs[0] if imgs else None, photos=imgs, state=it.get("condition"),
                seller=seller.get("username"),
                rating=round(float(pct) / 20, 1) if pct else None, reviews=seller.get("feedbackScore"),
                posted=posted, size=None)


class Ebay(Source):
    """API Browse officielle (clés gratuites sur developer.ebay.com)."""

    def __init__(self, country="fr"):
        marketplace, currency = EBAY.get(country, EBAY["fr"])
        super().__init__(f"ebay:{country}", currency)
        self.marketplace = marketplace
        self.client_id = os.getenv("EBAY_CLIENT_ID")
        self.client_secret = os.getenv("EBAY_CLIENT_SECRET")
        self.token = None
        self.token_expiry = 0

    _lock = threading.Lock()
    _shared = {"token": None, "expiry": 0}

    def _get_token(self):
        """Un seul jeton partagé par tous les sites eBay (évite de saturer l'authentification)."""
        with Ebay._lock:
            sh = Ebay._shared
            if sh["token"] and time.time() < sh["expiry"] - 60:
                return sh["token"]
            if not self.client_id or not self.client_secret:
                raise SourceDisabled("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET manquants")
            creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            for attempt in range(3):
                r = requests.post(
                    "https://api.ebay.com/identity/v1/oauth2/token",
                    headers={"Authorization": f"Basic {creds}",
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "client_credentials",
                          "scope": "https://api.ebay.com/oauth/api_scope"},
                    timeout=20,
                )
                if r.status_code < 400:
                    data = r.json()
                    sh["token"] = data["access_token"]
                    sh["expiry"] = time.time() + int(data.get("expires_in", 7200))
                    return sh["token"]
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))  # eBay met parfois du temps à propager les clés
            raise RuntimeError(f"eBay refuse les clés (HTTP {r.status_code}) : {r.text[:200]}")

    def search(self, query):
        r = self.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            params={"q": query, "sort": "newlyListed", "limit": 30},
            headers={"Authorization": f"Bearer {self._get_token()}",
                     "X-EBAY-C-MARKETPLACE-ID": self.marketplace},
        )
        for it in r.json().get("itemSummaries", []):
            price = it.get("price", {})
            yield self.item(
                it["itemId"], it.get("title"), parse_price(price.get("value")),
                price.get("currency"), it.get("itemWebUrl", ""), it.get("condition", ""),
                **ebay_meta(it),
            )


class EbayEnding(Ebay):
    """Enchères eBay qui se terminent dans les prochaines heures (souvent sans enchérisseur)."""

    def __init__(self, country="fr", hours=6):
        super().__init__(country)
        self.id = f"ebay:{country}"  # mêmes identifiants que les annonces eBay normales
        self.hours = hours

    def search(self, query):
        end = datetime.now(timezone.utc) + timedelta(hours=self.hours)
        r = self.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            params={"q": query, "sort": "endingSoonest", "limit": 30,
                    "filter": "buyingOptions:{AUCTION},itemEndDate:[..%s]" % end.strftime("%Y-%m-%dT%H:%M:%S.000Z")},
            headers={"Authorization": f"Bearer {self._get_token()}",
                     "X-EBAY-C-MARKETPLACE-ID": self.marketplace},
        )
        for it in r.json().get("itemSummaries", []):
            price = it.get("price", {})
            bids = it.get("bidCount", 0)
            yield self.item(
                it["itemId"], it.get("title"), parse_price(price.get("value")), price.get("currency"),
                it.get("itemWebUrl", ""),
                f"⏳ Enchère finit {str(it.get('itemEndDate', ''))[:16].replace('T', ' ')} UTC · {bids} enchère(s)",
                **ebay_meta(it),
            )


OLX_CUR = {"pl": "PLN", "ro": "RON", "bg": "BGN", "pt": "EUR", "ua": "UAH", "kz": "KZT"}


class Olx(Source):
    """API publique du site (olx.pl, .ro, .bg, .pt, .ua, .kz)."""

    def __init__(self, country):
        super().__init__(f"olx:{country}", OLX_CUR.get(country, "EUR"))
        self.base = f"https://www.olx.{country}"

    def search(self, query):
        r = self.get(
            f"{self.base}/api/v1/offers/",
            params={"offset": 0, "limit": 40, "query": query, "sort_by": "created_at:desc"},
            headers={"Accept": "application/json"},
        )
        for it in r.json().get("data", []):
            price, currency = None, None
            for p in it.get("params", []):
                if p.get("key") == "price":
                    v = p.get("value") or {}
                    price, currency = parse_price(v.get("value")), v.get("currency")
            photos = [ph.get("link", "").replace("{width}", "800").replace("{height}", "800")
                      for ph in it.get("photos", []) if ph.get("link")]
            posted = None
            try:
                posted = datetime.fromisoformat(it["created_time"]).timestamp()
            except (KeyError, ValueError, TypeError):
                pass
            size = next((str(p.get("value", {}).get("label")) for p in it.get("params", [])
                         if p.get("key") in ("size", "clothing_size", "rozmiar")), None)
            yield self.item(it["id"], it.get("title"), price, currency, it.get("url", ""),
                            photo=photos[0] if photos else None, photos=photos, posted=posted,
                            size=size, seller=(it.get("user") or {}).get("name"))


MARKTPLAATS = {
    "marktplaats": "https://www.marktplaats.nl",
    "2dehands": "https://www.2dehands.be",
    "2ememain": "https://www.2ememain.be",
}


class Marktplaats(Source):
    """Marktplaats (NL), 2dehands / 2ememain (BE) : même moteur."""

    def __init__(self, kind):
        super().__init__(kind)
        self.base = MARKTPLAATS[kind]

    def search(self, query):
        r = self.get(
            f"{self.base}/lrp/api/search",
            params={"query": query, "limit": 30, "offset": 0,
                    "sortBy": "SORT_INDEX", "sortOrder": "DECREASING"},
            headers={"Accept": "application/json"},
        )
        for it in r.json().get("listings", []):
            cents = (it.get("priceInfo") or {}).get("priceCents")
            yield self.item(
                it["itemId"], it.get("title"), cents / 100 if cents else None, "EUR",
                urljoin(self.base, it.get("vipUrl", "")),
            )


class Wallapop(Source):
    """EXPÉRIMENTEL : API interne de Wallapop (Espagne)."""

    def __init__(self):
        super().__init__("wallapop")

    def search(self, query):
        data = self.get(
            "https://api.wallapop.com/api/v3/general/search",
            params={"keywords": query, "order_by": "newest", "source": "search_box",
                    "latitude": 40.4168, "longitude": -3.7038},
            headers={"X-DeviceOS": "0", "Accept": "application/json, text/plain, */*",
                     "Origin": "https://es.wallapop.com"},
        ).json()
        items = (((data.get("data") or {}).get("section") or {}).get("payload") or {}).get("items") \
            or data.get("search_objects") or []
        for it in items:
            price, currency = it.get("price"), it.get("currency")
            if isinstance(price, dict):
                currency = price.get("currency") or currency
                price = price.get("amount")
            slug = it.get("web_slug") or it.get("slug") or it["id"]
            yield self.item(it["id"], it.get("title"), parse_price(price), currency,
                            f"https://es.wallapop.com/item/{slug}")


class Depop(Source):
    """EXPÉRIMENTEL : API interne de Depop (souvent protégée par Cloudflare)."""

    def __init__(self):
        super().__init__("depop")

    def search(self, query):
        data = self.get(
            "https://webapi.depop.com/api/v2/search/products/",
            params={"what": query, "itemsPerPage": 24, "country": "fr", "currency": "EUR",
                    "sort": "newlyListed"},
            headers={"Accept": "application/json"},
        ).json()
        for it in data.get("products", []):
            price = it.get("price") or {}
            slug = it.get("slug") or str(it.get("id"))
            title = it.get("description") or slug.replace("-", " ")
            yield self.item(
                it.get("id", slug), title, parse_price(price.get("priceAmount")),
                price.get("currencyName") or "EUR", f"https://www.depop.com/products/{slug}/",
            )


class Kleinanzeigen(Source):
    """EXPÉRIMENTEL : lecture de la page HTML (peut être bloquée)."""

    def __init__(self):
        super().__init__("kleinanzeigen")

    def search(self, query):
        html = self.get(
            "https://www.kleinanzeigen.de/s-suchanfrage.html",
            params={"keywords": query, "sortingField": "SORTING_DATE"},
        ).text
        for art in soup_of(html).select("article.aditem"):
            adid = art.get("data-adid")
            a = art.select_one("a.ellipsis") or art.select_one("a[href*='/s-anzeige/']")
            if not (adid and a):
                continue
            price = art.select_one(".aditem-main--middle--price-shipping--price")
            yield self.item(adid, a.get_text(strip=True),
                            parse_price(price.get_text()) if price else None, "EUR",
                            urljoin("https://www.kleinanzeigen.de", a.get("href", "")))


class Willhaben(Source):
    """EXPÉRIMENTEL : lit les données JSON intégrées à la page."""

    def __init__(self):
        super().__init__("willhaben")

    def search(self, query):
        html = self.get(
            "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz",
            params={"keyword": query, "sort": 1, "rows": 30},
        ).text
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise RuntimeError("données de page introuvables (blocage ou page modifiée)")
        data = json.loads(m.group(1))
        adverts = (((data.get("props") or {}).get("pageProps") or {}).get("searchResult") or {}) \
            .get("advertSummaryList", {}).get("advertSummary", [])
        for adv in adverts:
            attrs = {a.get("name"): (a.get("values") or [None])[0]
                     for a in (adv.get("attributes") or {}).get("attribute", [])}
            seo = attrs.get("SEO_URL")
            url = f"https://www.willhaben.at/iad/{seo}" if seo \
                else f"https://www.willhaben.at/iad/object?adId={adv.get('id')}"
            yield self.item(adv["id"], adv.get("description"), parse_price(attrs.get("PRICE")),
                            "EUR", url)


class Bazos(Source):
    """EXPÉRIMENTEL : lecture de la page HTML (bazos.cz / bazos.sk)."""

    def __init__(self, country):
        super().__init__(f"bazos:{country}", "CZK" if country == "cz" else "EUR")
        self.base = f"https://www.bazos.{country}"

    def search(self, query):
        html = self.get(f"{self.base}/search.php", params={"hledat": query, "Submit": "Hledat"}).text
        for div in soup_of(html).select("div.inzeraty"):
            a = div.select_one("h2.nadpis a")
            if not a:
                continue
            m = re.search(r"/inzerat/(\d+)", a.get("href", ""))
            if not m:
                continue
            price = div.select_one("div.inzeratycena")
            yield self.item(m.group(1), a.get_text(strip=True),
                            parse_price(price.get_text()) if price else None, None,
                            urljoin(self.base, a["href"]))


class Subito(Source):
    """EXPÉRIMENTEL : API interne de Subito (Italie)."""

    def __init__(self):
        super().__init__("subito")

    def search(self, query):
        data = self.get("https://hades.subito.it/v1/search/items",
                        params={"q": query, "sort": "datedesc", "lim": 30, "start": 0},
                        headers={"Accept": "application/json"}).json()
        for ad in data.get("ads", []):
            price = None
            for f in ad.get("features", []):
                if f.get("uri") == "/price":
                    price = parse_price(((f.get("values") or [{}])[0]).get("key"))
            yield self.item(ad["urn"], ad.get("subject"), price, "EUR",
                            (ad.get("urls") or {}).get("default", ""))


ML_SITES = {"mx": ("MLM", "MXN"), "br": ("MLB", "BRL"), "ar": ("MLA", "ARS"),
            "cl": ("MLC", "CLP"), "co": ("MCO", "COP")}


class MercadoLibre(Source):
    """EXPÉRIMENTEL : API publique MercadoLibre (pas de tri par date : résultats par pertinence)."""

    def __init__(self, country):
        site, cur = ML_SITES[country]
        super().__init__(f"ml:{country}", cur)
        self.site = site

    def search(self, query):
        data = self.get(f"https://api.mercadolibre.com/sites/{self.site}/search",
                        params={"q": query, "limit": 50}).json()
        for it in data.get("results", []):
            yield self.item(it["id"], it.get("title"), parse_price(it.get("price")),
                            it.get("currency_id"), it.get("permalink", ""))


def make_source(sid):
    kind, _, arg = sid.partition(":")
    if kind == "vinted":
        return Vinted(arg or "fr")
    if kind == "ebay":
        return Ebay(arg or "fr")
    if kind == "ebayend":
        return EbayEnding(arg or "fr")
    if kind == "olx":
        return Olx(arg)
    if kind in MARKTPLAATS:
        return Marktplaats(kind)
    if kind == "ml":
        return MercadoLibre(arg)
    if kind == "bazos":
        return Bazos(arg or "cz")
    simple = {"subito": Subito, "wallapop": Wallapop, "depop": Depop, "kleinanzeigen": Kleinanzeigen,
              "willhaben": Willhaben}
    if kind in simple:
        return simple[kind]()
    raise ValueError(f"site inconnu : {sid}")


# --------------------------------------------------------------------------
# Logique principale
# --------------------------------------------------------------------------
def expand(names, groups, _depth=0):
    """Transforme une liste de groupes/identifiants en identifiants de sites uniques."""
    out = []
    for n in names:
        subs = expand(groups[n], groups, _depth + 1) if n in groups and _depth < 5 else [n]
        out.extend(x for x in subs if x not in out)
    return out


def queries_of(search):
    q = search.get("queries") or search.get("query") or []
    return [q] if isinstance(q, str) else list(q)


def matches(item, search, fx=None, default_exclude=()):
    # On cherche dans le titre ET dans la marque / l'état (champ "extra") quand le site les donne.
    text = " " + (item["title"] + " " + (item.get("extra") or "")).lower() + " "
    for word in search.get("must_include", []):
        if word.lower() not in text:
            return False
    any_of = search.get("must_include_any", [])
    if any_of and not any(w.lower() in text for w in any_of):
        return False
    for group in search.get("require", []):  # chaque groupe : au moins un de ses mots
        if not any(w.lower() in text for w in group):
            return False
    allow = [w.lower() for w in search.get("allow", [])]
    for word in [w for w in default_exclude if w.lower() not in allow] + search.get("exclude", []):
        if word.lower() in text:
            return False
    if item["price"] is not None:
        eur = to_eur(item["price"], item["currency"], fx)
        if eur is not None:
            if search.get("max_price") and eur > search["max_price"]:
                return False
            if search.get("min_price") and eur < search["min_price"]:
                return False  # prix trop bas : souvent une contrefaçon ou une annonce bidon
    return True


def price_text(item, fx=None):
    if item["price"] is None:
        return "prix ?"
    price = f"{item['price']:.2f} {item['currency']}"
    eur = to_eur(item["price"], item["currency"], fx)
    if item["currency"] != "EUR" and eur is not None:
        price += f" (≈ {eur:.0f} €)"
    return price


def format_item(search_name, item, fx=None):
    price = price_text(item, fx)
    lines = [f"🔔 {search_name}", item["title"], f"💶 {price}"]
    if item["extra"]:
        lines.append(item["extra"])
    lines.append(f"📍 {item.get('source', '')}")
    lines.append(item["url"])
    return "\n".join(lines)


_GENERIC = {}


def enrich_photo(item):
    """Si le site n'a pas donné de photo, on la lit sur la page de l'annonce (balises og:image)."""
    if item.get("photo") or not str(item.get("url", "")).startswith("http"):
        return
    try:
        if "s" not in _GENERIC:
            _GENERIC["s"] = Source("generic")
        r = _GENERIC["s"].session.get(item["url"], timeout=12)
        if r.status_code >= 400:
            return
        urls = []
        for m in soup_of(r.text).find_all("meta"):
            if m.get("property") in ("og:image", "og:image:secure_url") or m.get("name") == "twitter:image":
                u = m.get("content")
                if u and u.startswith("http") and u not in urls:
                    urls.append(u)
        if urls:
            item["photo"], item["photos"] = urls[0], urls[:10]
    except Exception:
        pass  # sans photo, le message part quand même


def fetch_source(sid, queries, pace=None, deadline=None):
    """Interroge un site pour toutes ses requêtes (en série, avec pauses)."""
    results = {}
    try:
        source = make_source(sid)
    except Exception as e:
        return sid, {q: e for q in queries}
    fails = 0
    for q in queries:
        if deadline and time.time() > deadline:
            break  # temps imparti épuisé : le reste sera fait aux passes suivantes
        try:
            results[q] = list(source.search(q))
            fails = 0
        except Exception as e:
            results[q] = e
            fails += 1
            if isinstance(e, SourceDisabled) or fails >= 3 or "429" in str(e):
                break  # inutile d'insister : on réessaiera à la prochaine passe
        pause(*(pace or {}).get(sid.split(":")[0], (1.5, 3.5)))
    return sid, results


def run_once(config, dry_run=False, only=None):
    state = load_json(STATE_FILE, {"seen": {}, "initialized": []})
    seen = state.setdefault("seen", {})
    initialized = set(state.setdefault("initialized", []))
    fx = config.get("fx", {})
    groups = config.get("groups", {})
    default_exclude = config.get("default_exclude", [])
    notify_existing = config.get("notify_existing", True)
    backlog_max = int(config.get("backlog_max", 60))
    backlog_sent = 0
    pace = config.get("pace", {})
    cooldown = state.setdefault("cooldown", {})
    now = time.time()
    paused = {s for s, until in cooldown.items() if until > now}
    if not dry_run:
        try:
            extras.process_callbacks(state)
        except Exception as ex:  # les boutons ne doivent jamais bloquer la passe
            log(f"Boutons Telegram ignorés : {type(ex).__name__}: {ex}")
    save_lock = threading.Lock()

    def save_state():
        with save_lock:
            if len(seen) > MAX_SEEN:
                state["seen"] = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SEEN])
            state["initialized"] = sorted(initialized)
            state["cooldown"] = {s: u for s, u in cooldown.items() if u > time.time()}
            STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")
            try:
                extras.daily_backup([STATE_FILE, extras.JOURNAL])
            except OSError as ex:
                log(f"Sauvegarde impossible : {ex}")

    def hard_stop():
        log("Temps maximum atteint : sauvegarde de l'état et arrêt propre.")
        try:
            save_state()
        finally:
            os._exit(0)

    watchdog = threading.Timer(float(config.get("hard_limit_min", 11)) * 60, hard_stop)
    watchdog.daemon = True
    if os.getenv("GITHUB_ACTIONS") == "true" and not dry_run:  # sur GitHub : jamais de coupure brutale
        watchdog.start()
    last = state.setdefault("last", {})
    intervals = config.get("interval_min", {})

    def due(sid):
        """Un site peut avoir sa propre fréquence (config « interval_min », clé = site ou type)."""
        mins = intervals.get(sid, intervals.get(sid.split(":")[0]))
        return not mins or now - last.get(sid, 0) >= mins * 60 - 30

    def sources_of(search):
        sids = expand(search.get("sources", ["vinted:fr"]), groups)
        return [s for s in sids if s not in paused and (not only or any(s.startswith(p) for p in only))]

    plan = {}
    for search in config.get("searches", []):
        for sid in sources_of(search):
            lst = plan.setdefault(sid, [])
            lst.extend(q for q in queries_of(search) if q not in lst)

    if not (os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET")):
        ebay = [s for s in plan if s.startswith("ebay")]
        if ebay:
            log("eBay ignoré : clés manquantes (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")
        for s in ebay:
            del plan[s]
    for kind, n in config.get("rotate", {}).items():  # n sites de ce type par passe, en alternance
        ids = sorted(s for s in plan if s.split(":")[0] == kind)
        if len(ids) > n:
            rot = state.setdefault("rot", {})
            start = rot.get(kind, 0) % len(ids)
            keep = {ids[(start + i) % len(ids)] for i in range(n)}
            for s in ids:
                if s not in keep:
                    del plan[s]
            if not dry_run:
                rot[kind] = (start + n) % len(ids)
    slices = config.get("slice", {})
    qrot = state.setdefault("qrot", {})
    for sid in list(plan):  # n mots-clés par passe et par site (défaut : slice_default), en alternance
        n = slices.get(sid.split(":")[0], config.get("slice_default"))
        if n:
            qs = plan[sid]
            if len(qs) > n:
                start = qrot.get(sid, 0) % len(qs)
                plan[sid] = [qs[(start + i) % len(qs)] for i in range(n)]
                if not dry_run:
                    qrot[sid] = (start + n) % len(qs)
    for sid in [x for x in plan if not due(x)]:
        del plan[sid]
    if not dry_run:
        for sid in plan:
            last[sid] = now
    if paused:
        log(f"En pause (trop de requêtes récemment) : {', '.join(sorted(paused))}")
    log(f"{len(plan)} site(s), {sum(len(v) for v in plan.values())} requête(s) au total")
    start_time = time.time()
    budget = float(config.get("time_budget_min", 6))
    deadline = time.time() + budget * 60 if budget else None
    with ThreadPoolExecutor(max_workers=int(config.get("workers", 8))) as ex:
        fetched = dict(ex.map(lambda kv: fetch_source(kv[0], kv[1], pace, deadline), plan.items()))

    errors = {}
    alerts = 0
    send_deadline = start_time + float(config.get("send_budget_min", 9)) * 60
    max_alerts = int(config.get("max_alerts_per_pass", 60))
    per_site = int(config.get("max_alerts_per_site", 3))
    sent_by_site = {}
    xcfg = config.get("extras", {})
    wanted_sizes = config.get("sizes", [])
    pricelog = state.setdefault("pl", {})
    fprints = state.setdefault("fp", {})
    digest = state.setdefault("digest", [])
    flat_highlight = [h.lower() for h in config.get("highlight", [])]

    def notify_plain(text):
        if not dry_run:
            telegram_send(text, silent=False)

    try:
        for search in config.get("searches", []):
            tier = search.get("tier", "normal")
            for sid in sources_of(search):
                res = fetched.get(sid, {})
                for q in queries_of(search):
                    if q not in res:
                        continue
                    items = res[q]
                    if isinstance(items, Exception):
                        errors.setdefault(sid, f"{type(items).__name__}: {str(items)[:100]}")
                        if "429" in str(items) and not dry_run:  # on laisse le site respirer
                            cooldown[sid] = now + 60 * float(config.get("cooldown_min", 45))
                        continue
                    key = f"{sid}|{q}"
                    first_run = key not in initialized
                    for item in items:
                        low = " " + (item["title"] + " " + (item.get("extra") or "")).lower() + " "
                        eur = to_eur(item["price"], item["currency"], fx) if item["price"] is not None else None
                        already = item["id"] in seen
                        drop = False
                        if already:  # baisse de prix d'une annonce déjà signalée ?
                            old = pricelog.get(item["id"])
                            if old and eur and eur <= old * float(xcfg.get("drop_ratio", 0.85)) and matches(item, search, fx, default_exclude):
                                drop = True
                            else:
                                continue
                        if extras.ignored(state, item):
                            seen[item["id"]] = int(time.time())
                            continue
                        if first_run and not notify_existing and not already:  # on mémorise sans notifier
                            if not dry_run:
                                seen[item["id"]] = int(time.time())
                            continue
                        if not matches(item, search, fx, default_exclude):
                            continue
                        if not extras.size_ok(low, wanted_sizes):
                            continue
                        mkey = extras.model_key(item["title"])
                        med = extras.median_for(state, mkey)
                        if not dry_run and not drop:
                            extras.record_price(state, mkey, eur)
                        if first_run and not already:
                            if backlog_sent >= backlog_max:
                                continue  # trop d'annonces existantes d'un coup : le reste viendra à la passe suivante
                            backlog_sent += 1
                        labels = []
                        if any(h in low for h in flat_highlight):
                            labels.append("⭐ Pépite possible")
                        labels += extras.deal_labels(eur, med, xcfg)
                        if any(w.lower() in low for w in config.get("rare_words", [])):
                            labels.append("🏷️ Mot rare / vintage dans l'annonce")
                        if any(w.lower() in low for w in config.get("urgent_words", [])):
                            labels.append("⏰ Vendeur pressé : marge de négociation")
                        if extras.is_lot(low):
                            labels.append("📦 Lot / bundle : vérifie le contenu")
                        fp = extras.fingerprint(item, eur)
                        if fp in fprints and fprints[fp] != item["id"] and not drop:
                            labels.append("♻️ Remise en ligne probable (même titre et prix déjà vus)")
                        if drop:
                            labels.insert(0, f"📉 Baisse de prix (avant ≈ {pricelog[item['id']]:.0f} €)")
                        eff_tier = tier
                        if tier != "digest" and any(l.startswith(("⭐", "💎", "📉")) for l in labels):
                            eff_tier = "top"  # une pépite ou une vraie affaire remonte toujours en priorité
                        pepite = any(l.startswith(("⭐ Pépite", "💎")) for l in labels)
                        if first_run:
                            labels.append("📌 Déjà en ligne")
                        brand = extras.detect_brand(item["title"], config.get("brands", {}))
                        msg = extras.caption(item, brand, eur, med, pepite, labels, price_text(item, fx))
                        if dry_run:
                            print(f"[{eff_tier}{' 🔔' if pepite else ''}]", msg, "\n")
                            alerts += 1
                            continue
                        if sent_by_site.get(sid, 0) >= per_site and not pepite:
                            continue  # un seul site ne doit pas monopoliser la passe (le reste partira après)
                        if time.time() > send_deadline or alerts >= max_alerts:
                            continue  # trop d'alertes ou plus de temps : elles partiront à la passe suivante
                        if eff_tier == "digest":
                            digest.append({"title": item["title"], "price": eur or 0, "url": item["url"],
                                           "score": (eur / med) if (eur and med) else 1})
                            del digest[:-200]
                            sent = True
                        else:
                            if time.time() < send_deadline - 30:
                                enrich_photo(item)
                            extras.remember(state, item, brand)
                            sent = telegram_send(msg, markup=extras.keyboard(item), silent=not pepite,
                                                 photo=item.get("photo"), html=True,
                                                 thread=config.get("topics", {}).get(brand))
                            if sent and pepite:
                                extras.ntfy_send("Pépite", f"{item['title']} — {price_text(item, fx)}", item.get("url"))
                        if sent:
                            seen[item["id"]] = int(time.time())  # marqué vu seulement si bien envoyé
                            if eur:
                                pricelog[item["id"]] = eur
                            fprints[fp] = item["id"]
                            extras.bump(state, q)
                            extras.journal_add([datetime.now().strftime("%Y-%m-%d %H:%M"), search["name"], sid,
                                                item["title"], round(eur, 2) if eur else "", eff_tier,
                                                " | ".join(labels), item["url"]])
                            alerts += 1
                            sent_by_site[sid] = sent_by_site.get(sid, 0) + 1
                    if not dry_run:
                        initialized.add(key)

        # alertes reçues par e-mail pour les sites bloqués (Leboncoin, Vestiaire, Depop, Wallapop...)
        if not dry_run and config.get("email", {}).get("enabled"):
            try:
                for item in extras.mail_items(config["email"]):
                    if item["id"] not in seen and telegram_send("✉️ Alerte e-mail\n" + item["title"] + "\n" + item["url"],
                                                                 markup=extras.keyboard(item)):
                        seen[item["id"]] = int(time.time())
                        alerts += 1
            except Exception as ex:
                log(f"Lecture des e-mails impossible : {type(ex).__name__}: {ex}")

    except Exception as ex:  # une erreur imprévue ne doit pas faire perdre l'état
        log(f"Erreur pendant le traitement : {type(ex).__name__}: {ex}")

    if not dry_run:
        for sid in plan:
            extras.update_health(state, sid, sid not in errors, xcfg, notify_plain)
        if extras.digest_due(state, xcfg.get("digest_hour", 8)):
            if digest or xcfg.get("digest_always", False):
                telegram_send(extras.build_digest(state), silent=True)
            state["digest"] = []
            state["digest_day"] = datetime.now().strftime("%Y-%m-%d")
        for k in list(pricelog)[:-5000]:
            del pricelog[k]
        for k in list(fprints)[:-5000]:
            del fprints[k]

    for sid, err in sorted(errors.items()):
        log(f"[{sid}] KO : {err}")
    log(f"Terminé : {alerts} alerte(s), {len(errors)} site(s) en erreur")

    if not dry_run:
        save_state()
    watchdog.cancel()


def check(config, only=None):
    """Teste chaque site configuré avec une recherche simple."""
    groups = config.get("groups", {})
    sids = []
    for s in config.get("searches", []):
        for sid in expand(s.get("sources", []), groups):
            if sid not in sids and (not only or any(sid.startswith(p) for p in only)):
                sids.append(sid)

    def one(sid):
        try:
            items = list(make_source(sid).search("nike"))
        except Exception as e:
            return sid, "KO", f"{type(e).__name__}: {str(e)[:110]}"
        if not items:
            return sid, "??", "0 résultat (bloqué ou format changé ?)"
        return sid, "OK", f"{len(items)} annonces, ex. « {items[0]['title'][:45]} »"

    with ThreadPoolExecutor(max_workers=int(config.get("workers", 8))) as ex:
        results = list(ex.map(one, sids))
    for sid, status, detail in sorted(results):
        print(f"{status:2}  {sid:18} {detail}")
    ok = sum(1 for r in results if r[1] == "OK")
    print(f"\n{ok}/{len(results)} sites fonctionnent.")


def probe(sid):
    """Diagnostic : affiche ce que le site répond à plusieurs adresses."""
    s = make_source(sid)
    base = getattr(s, "base", "")
    tests = [("accueil", base, None)]
    if sid.startswith("vinted"):
        tests += [
            ("api json", f"{base}/api/v2/catalog/items", {"search_text": "nike", "order": "newest_first"}),
            ("page catalogue", f"{base}/catalog", {"search_text": "nike"}),
        ]
    for name, url, params in tests:
        try:
            r = s.session.get(url, params=params, timeout=20, headers=Vinted.API_HEADERS if "api" in name else None)
            body = r.text
            print(f"{name:15} HTTP {r.status_code}  {r.headers.get('content-type','?')[:30]}  {len(body)} car.  "
                  f"liens /items/ : {body.count('/items/')}")
            print("   début :", re.sub(r"\s+", " ", body[:160]))
        except Exception as e:
            print(f"{name:15} erreur : {e}")
    print("cookies :", [c if isinstance(c, str) else getattr(c, "name", c) for c in s.session.cookies][:12])


def main():
    parser = argparse.ArgumentParser(description="Bot d'alertes multi-sites")
    parser.add_argument("--loop", type=float, metavar="MIN", help="repasse toutes les MIN minutes")
    parser.add_argument("--dry-run", action="store_true", help="n'envoie rien, n'enregistre rien")
    parser.add_argument("--check", action="store_true", help="teste chaque site (OK / KO)")
    parser.add_argument("--probe", metavar="SITE", help="diagnostic d'un site, ex. vinted:fr")
    parser.add_argument("--only", metavar="PREFIXES", help="ex. vinted,olx:pl,ebay")
    parser.add_argument("--test-telegram", action="store_true", help="envoie un message de test")
    args = parser.parse_args()
    only = [p.strip() for p in args.only.split(",") if p.strip()] if args.only else None

    if args.test_telegram:
        sys.exit(0 if telegram_send("✅ Test du bot d'alertes : ça marche.") else 1)

    if args.probe:
        probe(args.probe)
        return

    config = load_json(CONFIG_FILE, None)
    if not config:
        sys.exit("config.json introuvable ou vide.")

    if args.check:
        check(config, only)
    elif args.loop:
        while True:
            try:
                run_once(config, args.dry_run, only)
            except Exception as ex:
                log(f"Passe interrompue ({type(ex).__name__}: {ex}), nouvel essai à la prochaine")
            time.sleep(max(args.loop, 1) * 60)
    else:
        run_once(config, args.dry_run, only)


if __name__ == "__main__":
    main()
