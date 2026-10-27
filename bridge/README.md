# Pont MetaTrader 5 (optionnel)

`mt5_bridge.py` lit le terminal **FTMO Global Markets MT5** ouvert sur votre PC Windows et envoie à l'API :
les spécifications des symboles (taille de contrat, pas de lot...), les barres H1 clôturées et les ticks (1 par seconde).
Le site affiche alors le prix temps réel FTMO ; avec `PRICE_SOURCE=mt5` les barres H1 FTMO remplacent aussi celles de Dukascopy.

Le script ne manipule **aucun identifiant** : il s'attache à un terminal déjà lancé et connecté.

## Installation

Python 3.8 à 3.14 (Windows 64 bits, wheel officielle vérifiée pour 3.13) :

    pip install -r bridge/requirements.txt

## Utilisation

1. Ouvrir le terminal FTMO MT5 et se connecter au compte. Dans *Outils > Options > Graphiques*, mettre « Maximum de barres
   dans le graphique » à une grande valeur (ex. 100000) pour un long backfill.
2. Lancer l'API (docker, port 8080 via nginx) puis :

       python bridge/mt5_bridge.py --api http://127.0.0.1:8080

Options : `--terminal "C:/Program Files/FTMO Global Markets MT5 Terminal/terminal64.exe"` (si plusieurs terminaux),
`--since 2024-01-01` (début du backfill quand l'API n'a encore aucune barre MT5, défaut 2 ans),
`--symbols XAUUSD,EURUSD`, `--interval 1`, `--token ...` (ou variable `INGEST_TOKEN`, doit égaler celle de l'API),
`--server-offset 2` (décalage fixe), `--dry-run` (n'envoie rien), `--fake --max-seconds 20` (test de la chaîne sans MT5).

Au démarrage : envoi des specs, backfill depuis la dernière barre connue de l'API (`GET /api/ingest/status`), puis boucle :
tick toutes les secondes, nouvelle barre H1 poussée dès qu'elle est clôturée. Relancer le script ne duplique rien.

## Heure du serveur

MT5 donne toutes ses heures en **heure du serveur** du courtier (FTMO : UTC+2 en hiver, UTC+3 en été, bascule calée sur les
États-Unis), encodée comme si c'était de l'UTC. Le pont convertit en UTC avant l'envoi :

- règle FTMO (UTC+2 / UTC+3 avec DST américain), appliquée barre par barre, donc correcte sur tout l'historique ;
- au démarrage (puis toutes les 6 h) le décalage est **mesuré** : heure d'un tick frais moins l'horloge du PC, arrondi à l'heure,
  validé si au moins 2 symboles concordent. Si la mesure diffère de la règle, la différence est conservée ;
- marché fermé (week-end) la mesure est impossible : la règle s'applique (message dans la console) ;
- `--server-offset N` impose un décalage fixe de N heures, sans règle DST.

Vérification rapide : l'horloge du PC doit être à l'heure (NTP). Les barres H1 doivent s'ouvrir à des heures rondes UTC
(XAUUSD en hiver : première barre de la semaine à 23h UTC).

## Si ça ne marche pas

- « Impossible de se connecter au terminal MT5 » : le terminal n'est pas ouvert/connecté, ou plusieurs terminaux existent
  (utiliser `--terminal`). Le terminal et Python doivent être de même niveau de droits (pas l'un en administrateur).
- « symbole introuvable » : le nom diffère chez le courtier ; corriger le champ `mt5` dans `api/app/config.py`.
