# Structure H1 (v2, en direct)

Graphique H1 façon TradingView sur 10 marchés (or, argent, EURUSD, GBPUSD, USDJPY, S&P 500, Nasdaq, DAX, WTI, Brent) :
structure de Bob recalculée à chaque nouvelle bougie, backtest sur la période choisie avec paramètres réglables,
et ordres à placer (BUY/SELL LIMIT ou au marché, SL, TP, lots selon capital et risque).

Rien n'est envoyé au broker : les ordres sont à saisir à la main dans MT5.

## Lancer

Docker Desktop doit tourner. Dans ce dossier :

```
docker compose up -d --build
```

Puis ouvrir http://localhost:8080. Arrêter : `docker compose down`.

Les prix sont stockés dans `_data/` (fichiers parquet). Au premier lancement, l'historique depuis 2010 se télécharge
en tâche de fond (quelques minutes par marché) ; le site est utilisable dès que les données récentes sont là.

## D'où viennent les prix

- **Dukascopy** (par défaut, gratuit, sans compte) : bougies H1 depuis 2010, mises à jour chaque minute.
  Une bougie fermée arrive 1 à 2 min après la fin de l'heure. Le prix « en direct » entre deux bougies a jusqu'à 1 h de retard.
  Le service a des pannes (503, ou redirection vers le https bloqué) : la mise à jour s'arrête alors aussitôt,
  la pastille affiche « Dukascopy indisponible », et elle reprend seule à la minute suivante qui marche.
  Pour voir ce que fait le serveur : `docker compose logs -f api`.
- **MT5 FTMO** (optionnel, temps réel) : avec le terminal MT5 ouvert et connecté sur ce PC, lancer la passerelle
  (voir `bridge/README.md`) :

  ```
  python bridge/mt5_bridge.py --api http://127.0.0.1:8080
  ```

  Elle envoie les prix tick par tick et les vraies tailles de contrat (calcul des lots exact). Pour baser aussi les
  bougies et les backtests sur les prix FTMO : `PRICE_SOURCE=mt5` dans `docker-compose.yml`.

Réglages (variables dans `docker-compose.yml`) : `REFRESH_SECONDS` (60), `PRICE_SOURCE` (dukascopy | mt5),
`HISTORY_START` (2010-01-01), `INGEST_TOKEN` (secret partagé avec la passerelle, optionnel), `WEB_PORT` (8080).

## Mettre en ligne (serveur avec Docker)

Le site a besoin d'un serveur qui tourne en continu (mise à jour des prix chaque minute, données sur disque) :
un petit VPS Linux suffit (1 vCPU, 2 Go de RAM, ~5 €/mois chez Hetzner, OVH, Scaleway…).

```
# sur le serveur (Ubuntu), une seule fois
curl -fsSL https://get.docker.com | sh
git clone https://github.com/antoinerame/algo-bob.git && cd algo-bob
echo "WEB_PORT=80" > .env
echo "INGEST_TOKEN=$(openssl rand -hex 24)" >> .env      # secret pour la passerelle MT5
docker compose up -d --build
```

Le site est alors sur `http://<ip-du-serveur>`. Le premier démarrage télécharge l'historique (une dizaine de minutes
pour l'or, plus pour les autres marchés). Mise à jour : `git pull && docker compose up -d --build`.

Sans `INGEST_TOKEN`, l'API refuse les prix envoyés depuis internet (seul le réseau local est accepté). Pour brancher
MT5 depuis ton PC sur le serveur : `python bridge/mt5_bridge.py --api http://<ip-du-serveur> --token <INGEST_TOKEN>`.
Le site n'a pas de mot de passe : toute personne qui a l'adresse le voit (pour un nom de domaine et le HTTPS, mettre
Caddy ou un tunnel Cloudflare devant).

## Backtest

- Période au choix (onglet Statistiques) ; la structure est toujours calculée sur tout l'historique, la période ne filtre que les entrées.
- Calcul sur bougies H1 : si SL et TP sont touchés dans la même heure, le trade compte perdant. Un peu plus pessimiste
  que la v1 (minute) : sur l'or, EPA −0,061 R contre −0,059 R ; FT valide −0,106 R contre −0,066 R.
- Optimiser les paramètres sur une période puis vérifier sur une autre : sinon on surajuste.

## Contenu

- `site/` : la page (lightweight-charts 4.1.3 inclus).
- `api/` : serveur Python (FastAPI) — flux Dukascopy, stockage, moteur (`engine/structure.py` et `continuation.py` = règles de Bob, inchangées), backtest, plan.
- `bridge/` : passerelle MT5 (Windows).
- `tests/` : `.venv/Scripts/python -m pytest` (crée le venv avec `python -m venv .venv` puis `pip install -r api/requirements.txt pytest`).
- `scripts/` : code de la v1 (calcul hors ligne sur prix minute). La v1 statique est copiée dans `../structure-h1-v1-statique/`.
- `ARCHITECTURE.md` : contrats techniques (API, paramètres).
