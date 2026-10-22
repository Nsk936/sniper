# Bot d'alertes multi-sites → Telegram

Il surveille tes recherches sur ~33 sites (Vinted ×15 pays, eBay ×4, OLX ×6, Marktplaats/2dehands/2ememain,
Wallapop, Depop, Kleinanzeigen, Willhaben, Bazoš…) et t'envoie un message Telegram dès qu'une **nouvelle**
annonce correspond. Il n'achète rien automatiquement.

## 1. Créer le bot Telegram (2 min, gratuit)
1. Sur Telegram, ouvre **@BotFather** → `/newbot` → copie le **token**.
2. Envoie un message à ton bot, puis ouvre `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`
   et repère `"chat":{"id": ...}` : c'est ton **chat id**.

## 2. (Optionnel) Clés eBay gratuites
Sur https://developer.ebay.com : crée une application "Production" → **Client ID** + **Client Secret**
(variables `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`). Sans clés, les sites eBay sont simplement ignorés.

## 3. Lancer sur ton PC
```bash
pip install -r requirements.txt

# Windows (PowerShell)
$env:TELEGRAM_BOT_TOKEN="123:ABC"; $env:TELEGRAM_CHAT_ID="123456"
# Mac / Linux
export TELEGRAM_BOT_TOKEN="123:ABC" TELEGRAM_CHAT_ID="123456"

python sniper.py --test-telegram   # vérifie que le message arrive
python sniper.py --check           # teste chaque site : OK / ?? / KO   <-- À FAIRE EN PREMIER
python sniper.py --dry-run         # simule une passe sans rien envoyer
python sniper.py --loop 15         # tourne en continu, une passe toutes les 15 minutes (pas moins !)
```
`--only vinted,olx:pl` limite à certains sites. Au premier passage de chaque (site, mot-clé), le bot
**mémorise** les annonces existantes sans alerter, puis n'alerte que sur les nouvelles.

Si beaucoup de sites répondent **403**, c'est l'anti-bot : le module `curl_cffi` (dans requirements.txt) y répond en imitant Chrome ; refais `pip install -r requirements.txt`.

**`--check` est important** : je n'ai pas pu tester les sites en direct. Les sites marqués KO ou ?? sont
bloqués depuis ta connexion ou ont changé de format. Garde ceux qui sont OK ; envoie-moi le résultat
pour que je corrige les autres.

## 4. Cloud gratuit (GitHub Actions)
Dépôt **public** (minutes illimitées), secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (+ eBay),
workflow dans `.github/workflows/sniper.yml` (toutes les 15 min). Beaucoup de sites bloquent les IP
des serveurs cloud : si `--check` donne de meilleurs résultats sur ton PC, lance-le plutôt chez toi.

## Régler tes recherches : `config.json`
- `groups` : listes de sites réutilisables (`tous`, `vinted_all`, `olx_all`…). Retire un site d'un groupe pour l'ignorer.
- `queries` : mots envoyés à chaque site (un appel par mot-clé et par site).
- `must_include_any` : il faut au moins un de ces mots dans le **titre** (filtre le bruit).
- `must_include` : mots tous obligatoires · `exclude` : mots interdits · `default_exclude` : exclusions globales
  (femme, enfant, short… en plusieurs langues).
- `max_price` : prix max **en euros** ; les autres devises sont converties avec `fx` (taux approximatifs, à ajuster).
- `workers` : nombre de sites interrogés en parallèle.

## Sites non pris en charge
Facebook Marketplace (connexion obligatoire), Mercari US/JP, Goofish, Poshmark, Grailed, Tradera, Allegro,
et les petites annonces scandinaves / est-européennes / italiennes / espagnoles non listées ci-dessus.
Chacun demande une intégration spécifique (clés, signatures, anti-bot).

## À savoir
- Vinted, OLX, Marktplaats, Wallapop, Depop passent par des API internes non officielles : elles peuvent changer
  ou bloquer, et leur usage va à l'encontre des conditions des sites. Rythme raisonnable pour un usage personnel.
- Le filtre porte sur le titre : une annonce qui n'a le modèle que dans la description sera ratée.
- Vérifie toujours le code produit sur l'étiquette avant d'acheter.

## Annonces déjà en ligne
Par défaut (`"notify_existing": true`), au premier passage le bot t'envoie aussi les annonces **déjà en ligne** qui
correspondent à tes filtres (marquées « 📌 Déjà en ligne »), jusqu'à `backlog_max` par passe ; le reste suit à la passe
suivante. Seules les ~30 à 90 annonces les plus récentes de chaque recherche sont lues, pas tout l'historique.
Pour refaire ce rattrapage, supprime le fichier `seen.json`. Mets `"notify_existing": false` pour ne recevoir que les nouvelles.

## Nouveautés (v2)
- **Niveaux** (`tier` par recherche) : `top` (🚨 + notif ntfy), `normal`, `low` (notification silencieuse), `digest` (regroupé dans le résumé de 8 h). Une pépite, une baisse de prix ou une vraie affaire passe toujours en 🚨.
- **Score d'affaire** : prix comparé à la médiane déjà vue pour le même modèle (💎, ⚠️ prix suspect, 💰 revente estimée — estimation indicative, pas une garantie).
- **Taille** : mets `"sizes": ["M"]` dans config.json ; une taille différente écarte l'annonce, une taille non indiquée la garde.
- **Boutons Telegram** : Ouvrir / Ignorer ce modèle / Ignorer ce vendeur (le vendeur n'est connu que si le site le fournit ; les clics sont lus à la passe suivante).
- **Fréquence par site** : `interval_min` (eBay 5 min, Vinted FR 10 min…). Sur GitHub Actions le minimum reste 15 min.
- **ntfy** (2e canal gratuit) : installe l'appli ntfy, choisis un nom de canal secret et définis `NTFY_TOPIC`.
- **E-mails d'alertes** (Leboncoin, Vestiaire, Depop, Wallapop, Facebook) : crée les alertes sur ces sites vers une adresse Gmail, puis définis `IMAP_HOST=imap.gmail.com`, `IMAP_USER`, `IMAP_PASSWORD` (mot de passe d'application) et mets `"email": {"enabled": true}`. Non testé en réel.
- Enchères eBay finissant bientôt (`ebayend:*`), fautes de vendeurs, lots/bundles, remises en ligne, baisses de prix, alerte de panne (>2 h), résumé du matin, journal `journal.csv` (ouvrable dans Excel), compteurs par mot-clé, sauvegardes quotidiennes dans `backups/`.
- **Windows** : lance `demarrage_auto_windows.bat` une fois ; le bot redémarre à chaque connexion.

## Messages photo (v3)
- Chaque annonce = **un message photo** (photo en grand, légende HTML courte, mêmes lignes partout : 💶 prix, 📏 taille, ✨ état, ⭐ vendeur, 🕒 ancienneté — une ligne est omise si le site ne donne pas l'info). Si la photo ne charge pas, le message part sans photo.
- Badges : « 🔥 -X % sous le marché » (dès que le bot a assez de prix de comparaison) et « 💎 Pépite ». **Son de notification uniquement pour les 💎 pépites**, le reste est silencieux.
- Boutons : 🛒 Acheter / 🔗 Voir l'annonce (liens) · 💬 Contacter / ⭐ Favori · 📷 Photos (album) / 🚫 Ignorer. « Contacter » ouvre l'annonce (aucun site ne permet d'écrire au vendeur par un lien direct). Favori/Photos/Ignorer sont traités à la passe suivante du bot, pas instantanément.
- Un sujet par marque : crée un groupe Telegram avec « Sujets » activés, ajoute le bot comme admin, puis mets les numéros de sujets dans `"topics"` de config.json, ex. `{"Nike": 12, "Stone Island": 15}` (et TELEGRAM_CHAT_ID = l'identifiant du groupe).
- Nouvelles marques : Stone Island, Ralph Lauren, Essentials, Lacoste, Alo, Adidas, Nike archives, Under Armour pépites (pépites = alertes normales, général = silencieux). Exclusions ajoutées : taché, troué, abîmé, défaut, dupe, imitation, aliexpress…
- Les annonces sans marque (« veste coupe-vent noire ») ne sont pas cherchables par mot-clé ici ; il faut les alertes natives des sites avec filtre de marque.
