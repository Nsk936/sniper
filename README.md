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
