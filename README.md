# Surveillance de disponibilité ARO et ARO-CEP

Suivi des RR1h à H+1, H+24 et H+51 pour les réseaux 00, 03, 06, 09, 12 et 18 UTC.
Le domaine de test de 0,1° × 0,1° est centré près de Foix.

ARO = AROME 0,01° ; ARO-CEP = AROME-IFS diffusé à 0,025°.
Les chemins WCS sont issus du Swagger de l'API AROME du portail Météo-France.
Le suivi mesure cette API ciblée, et non la disponibilité des outils internes ou de Meteociel.

## Installation sur GitHub (sans terminal)

1. Créer un nouveau dépôt **public**, par exemple `disponibilite-modeles`, initialisé avec un README.
   Un dépôt public évite de consommer le quota de minutes des dépôts privés pour ces exécutions fréquentes.
2. Décompresser l'archive. Ajouter à la racine du dépôt `surveiller_modeles.py`, `config.json`, `requirements.txt`, `.gitignore` et ce README, via **Add file → Upload files**.
   Ne pas déposer le dossier englobant : ces fichiers doivent être directement à la racine.
3. Créer le workflow avec **Add file → Create new file** :
   saisir exactement `.github/workflows/surveillance.yml`, puis copier le contenu du fichier homonyme fourni dans l'archive et valider avec **Commit changes**.
   Le dossier `.github` est caché sur Mac (Cmd+Maj+point permet de l'afficher).
4. Dans **Settings → Secrets and variables → Actions → New repository secret**, créer :
   - nom : `METEOFRANCE_API_KEY`
   - valeur : la clé de ton `api_key.txt`, sur une ligne, sans préfixe Bearer.
   La clé doit donner accès à l'API ciblée AROME, incluant AROME-IFS.
   Ne jamais ajouter `api_key.txt` au dépôt ni coller la clé dans le YAML.
5. Dans **Settings → Actions → General → Workflow permissions**, autoriser **Read and write permissions**, puis **Save** si l'écriture est bloquée par les réglages du dépôt.
6. Ouvrir **Actions → Disponibilite ARO et ARO-CEP → Run workflow** et lancer un premier test manuel.
7. Vérifier les logs de **Sonder les RR1h**, puis le dossier `data/` apparu dans le dépôt.
   Une réponse HTTP 401/403 demande de vérifier la clé et son abonnement à l'API ; 429 indique un quota atteint.
   Une réponse de catalogue non valide est marquée INDETERMINE, jamais assimilée à une absence du modèle.

Le workflow s'exécute ensuite automatiquement aux minutes 02, 07, 12…57 de chaque heure UTC.
Les fichiers doivent être présents sur la branche par défaut (en général `main`).
Un ordinateur allumé n'est pas nécessaire.

## Résultats

- `data/etat.json` : état persistant des premiers succès et derniers constats d'absence.
- `data/tests_AAAA-MM-JJ.csv` : détail de chaque test, heure réelle, HTTP, résultat.
- `data/disponibilites.csv` : une ligne par modèle, réseau et jalon.
- `data/disponibilites.html` : rapport autonome, UTC et heure de Paris, avec synthèse par réseau/jalon.

Pour consulter le rapport : ouvrir une exécution dans Actions, télécharger l'artifact
`disponibilite-modeles`, le décompresser, puis ouvrir `disponibilites.html` dans un navigateur.
On peut également télécharger directement le HTML depuis le dépôt avec le bouton de téléchargement du fichier.
Le rapport ne nécessite aucun serveur ni connexion Internet après téléchargement.

Les premières heures seront surtout des observations censurées : des réseaux antérieurs sont déjà disponibles.
Les observations réellement encadrées apparaissent lorsque les nouveaux réseaux arrivent après le démarrage.

## Interprétation et précision

Une disponibilité est comprise entre le début du dernier test d'absence valide et la fin du premier téléchargement validé.
L'intervalle est volontairement conservateur : il inclut le temps de réponse et les interruptions de sondage.
Un 200 n'est pas suffisant : le contenu GRIB doit confirmer les RR au sol, le réseau, H+1/24/51,
l'intervalle de cumul d'une heure, les unités et au moins un point valide dans le domaine.
Les zéros de RR constituent des données valides.
Un timeout, un échec serveur, un 401/403/429, une réponse inconnue ou un GRIB incorrect sont INDETERMINE.

GitHub Actions est un planificateur « best effort », pas un service temps réel : certains départs peuvent être retardés.
L'intervalle affiché reflète ces retards ; les mesures de précision supérieure à 15 minutes sont exclues de la synthèse
mais restent présentes dans le détail. Il n'y a aucune garantie permanente de précision à 10–15 minutes.

Tous les réseaux demandés sont recherchés pour les deux modèles, sans supposer qu'ils sont tous produits.
Un réseau jamais annoncé est « non observé sur la fenêtre de suivi » après 36 h, et ne donne pas d'horaire inventé.
Les heures de réseaux effectivement rencontrées dans les catalogues sont indiquées en haut du rapport.

Le « catalogue RR1h » signifie que le coverage est annoncé, pas que toutes les échéances sont déjà récupérables.
Un succès H+51 ne prouve pas que H+2 à H+50 sont tous accessibles : le suivi mesure uniquement les trois jalons choisis.
Une fois un jalon validé, son téléchargement n'est plus demandé ; seuls les jalons manquants restent testés.

Pour modifier le domaine, les réseaux ou les jalons, éditer `config.json`.
Conserver la même configuration pendant la campagne ; si elle change, démarrer un nouvel historique pour éviter de mélanger les mesures.

## Durée et arrêt

Laisser tourner au moins deux à trois semaines.
Pour arrêter : Actions → workflow → menu « … » → Disable workflow.
Les workflows planifiés des dépôts publics peuvent être automatiquement désactivés après 60 jours d'inactivité.
Les valeurs et dates sont conservées dans git à chaque cycle, indépendamment de la durée de rétention des artifacts.

## Test local facultatif

```bash
python3 -m pip install -r requirements.txt
export METEOFRANCE_API_KEY='votre-cle'
python3 surveiller_modeles.py
```
Ne pas partager un terminal ou une capture contenant la clé.
Pour régénérer seulement le rapport depuis l'état déjà enregistré :

```bash
python3 surveiller_modeles.py --report-only
```
