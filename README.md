# Veille scientifique DT1 — automate hebdomadaire

Automate qui détecte les nouveautés scientifiques internationales sur le
diabète de type 1 (PubMed, ClinicalTrials.gov, flux RSS d'organismes),
les résume en français de façon strictement extractive, puis met à jour
[diabete-type1-veille.md](diabete-type1-veille.md) selon **deux logiques
distinctes** :

1. **Mise à jour d'une fiche existante** (sections 2-4) : si la nouveauté
   correspond, par identifiant exact tiré du titre `###`, à une
   technologie déjà suivie (ex. "VX-880" pour la fiche "Zimislecel
   (VX-880)"), les champs **Maturité**, **Pays**, **Bénéfices**,
   **Inconvénients & risques** et **Source principale** de cette fiche
   sont mis à jour — jamais son titre, jamais son "Niveau de confiance"
   (jugement curé manuellement), et jamais une nouvelle fiche créée.
2. **Nouvelle entrée** en section 5 : toute nouveauté qui ne correspond à
   aucune fiche existante (nouvelle technologie, conférence, annonce
   réglementaire) est ajoutée en tête de « 5. Nouveautés et fil
   d'actualité », sans jamais écraser les entrées précédentes.

Les deux ne se produisent jamais pour la même nouveauté. Les sections 1,
6 et 7 ne sont jamais touchées.

## Installation

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Utilisation

```powershell
# Aperçu (aucune écriture), fenêtre glissante par défaut (15 jours) :
.\.venv\Scripts\python.exe main.py --dry-run

# Exécution réelle (écrit dans le .md et met à jour dedup_store.json) :
.\.venv\Scripts\python.exe main.py

# Test sur une fenêtre passée fixe, en ignorant la déduplication :
.\.venv\Scripts\python.exe main.py --dry-run --window-start 2026-09-01 --window-end 2026-09-18 --ignore-dedup
```

Le mode `--dry-run` affiche un diff unifié du Markdown proposé sur la
sortie standard et n'écrit ni le `.md` ni `dedup_store.json`. **Toujours
valider en `--dry-run` avant une première exécution réelle.**

Ajoutez `--debug` à n'importe quelle commande ci-dessus pour une
journalisation détaillée (décisions de classement, tentatives de
correspondance fiche/nouveauté item par item) — voir la section
Débogage plus bas.

## Notification Windows (run_veille.ps1)

`run_veille.ps1` encapsule `main.py` et affiche une notification Windows
(toast) à la fin de l'exécution : nombre de fiches mises à jour, de
nouvelles entrées, d'avertissements, ou message d'erreur en cas
d'échec. C'est ce script qu'il faut utiliser au quotidien (et que la
tâche planifiée hebdomadaire utilise, voir plus bas) :

```powershell
.\run_veille.ps1              # exécution réelle, avec notification
.\run_veille.ps1 -DryRun      # test, avec notification (rien n'est écrit)
```

La notification ne s'affiche que si une session Windows est ouverte au
moment de l'exécution (comportement voulu : elle sert à être vue). Si le
PC est verrouillé ou personne n'est connecté, le résultat reste
consultable dans `logs\veille.log` et `logs\last_run_summary.json`.

## Notification par email (pour quand vous n'êtes pas devant le PC)

En complément de la notification Windows (éphémère), un email peut être
envoyé — utile si le PC tourne la tâche planifiée alors que vous êtes
absent. Géré entièrement par `main.py` (pas par `run_veille.ps1`), via
Gmail SMTP.

**Mise en place (à faire une seule fois) :**

1. Sur le compte Google **fuz2308@gmail.com**, activez la
   validation en deux étapes si ce n'est pas déjà fait :
   https://myaccount.google.com/signinoptions/two-step-verification
2. Générez un mot de passe d'application sur
   https://myaccount.google.com/apppasswords (choisir "Autre", nommer par
   exemple "Veille DT1"). Un code de 16 caractères s'affiche.
3. Copiez `email_credentials.example.yaml` sous le nom
   `email_credentials.yaml` (dans le même dossier), et remplacez la valeur
   d'exemple par ce code de 16 caractères. Ce fichier n'est jamais
   envoyé/partagé nulle part par le script — il ne fait que servir à s'authentifier
   auprès de Gmail — et il est exclu du suivi de version (`.gitignore`).
   (Peu importe que vous colliez le code avec ou sans les espaces que Google
   affiche pour la lisibilité — le script les retire automatiquement.)

**Configuration** (`config.yaml`, déjà en place) :
```yaml
email:
  enabled: true
  from_addr: "fuz2308@gmail.com"    # compte Gmail avec le mot de passe d'application
  to_addrs:                          # un ou plusieurs destinataires
    - "jmfusella@free.fr"
    - "marionfusella@gmail.com"
  only_if_changes: true              # pas d'email pour un "rien à signaler"
```
Les destinataires peuvent être sur n'importe quel fournisseur (Gmail,
Free, etc.) : seul l'expéditeur doit être le compte Gmail configuré avec
le mot de passe d'application.

**Contenu :** le mail détaille chaque nouveauté — pour une fiche mise à
jour, son pays et le texte exact ajouté à Bénéfices/Inconvénients &
risques/Source principale ; pour une nouvelle entrée, son pays, son
résumé (FR) complet, sa maturité, sa confiance et le lien vers la source.
Le pays apparaît systématiquement dans les deux cas — jamais deviné :
"non précisé dans la source" si l'information est absente de la source
consultée. Pas besoin d'ouvrir le fichier `.md` pour avoir le détail.

**Comportement :**
- Envoyé uniquement en exécution réelle (jamais en `--dry-run`), pour ne
  jamais surprendre pendant un test.
- Avec `only_if_changes: true` (recommandé) : un email seulement s'il y a
  au moins une fiche mise à jour, une nouvelle entrée, ou une erreur —
  jamais pour un « rien à signaler » (la notification Windows, elle,
  continue de s'afficher à chaque exécution).
- Un échec d'envoi (identifiants absents, réseau, quota Gmail) est
  journalisé en avertissement mais **n'interrompt jamais** l'automate :
  l'email est un confort, jamais une garantie.

**Tester la configuration sans attendre une vraie nouveauté :**
```powershell
.\.venv\Scripts\python.exe main.py --dry-run --test-email --limit 1
```
`--test-email` force un envoi (même en dry-run, même sans changement),
pour valider que les identifiants Gmail fonctionnent.

## Planification hebdomadaire (PC local, tâche planifiée Windows)

```powershell
.\register_task.ps1                                   # chaque lundi 07h00, écriture réelle
.\register_task.ps1 -DayOfWeek Wednesday -Time "08:30"
.\register_task.ps1 -DryRun                            # pour valider la planification sans jamais écrire
```

La tâche planifiée appelle `run_veille.ps1` (pas `main.py` directement) :
la notification Windows fonctionne donc aussi bien en exécution manuelle
qu'en exécution hebdomadaire automatique.

Le PC doit être allumé à l'heure prévue ; `StartWhenAvailable` rattrape
une exécution manquée au prochain démarrage. Vérifier avec :

```powershell
Get-ScheduledTask -TaskName VeilleDT1 | Get-ScheduledTaskInfo
```

Supprimer avec :

```powershell
Unregister-ScheduledTask -TaskName VeilleDT1 -Confirm:$false
```

## Fonctionnement dans le cloud (GitHub Actions) — optionnel

Alternative à la tâche planifiée Windows locale ci-dessus : l'automate peut
tourner chaque semaine sur les serveurs GitHub
(`.github/workflows/veille.yml`), indépendamment de tout PC allumé. Le PC
n'est alors nécessaire que pour consulter le résultat (email, ou
`diabete-type1-veille.md` à jour sur GitHub / après `git pull`) — pas pour
que l'automate s'exécute.

**Mise en place (à faire une seule fois) :**

1. Créer un dépôt GitHub, de préférence **privé** (`config.yaml` contient
   des adresses email, et `diabete-type1-veille.md` y sera poussé
   automatiquement à chaque nouveauté) :
   ```powershell
   git init
   git add .
   git commit -m "Automate de veille DT1"
   git branch -M main
   git remote add origin https://github.com/VOTRE-COMPTE/veille-diabete-type-1.git
   git push -u origin main
   ```
2. Sur la page du dépôt : **Settings → Secrets and variables → Actions →
   New repository secret**. Un seul secret est nécessaire :

   | Nom du secret | Valeur |
   |---|---|
   | `GMAIL_APP_PASSWORD` | le même mot de passe d'application à 16 caractères que celui déjà présent dans `email_credentials.yaml` en local |

   (`from_addr`/`to_addr`/`to_addrs` restent dans `config.yaml`, déjà versionné
   dans le dépôt — ce ne sont pas des secrets, seul le mot de passe l'est.
   `email_credentials.yaml`, lui, reste exclu du suivi de version : le
   workflow le régénère à la volée depuis le secret à chaque exécution.)
3. Onglet **Actions** du dépôt → workflow **"Veille DT1"** → **"Run
   workflow"**, laisser **"Simulation seulement"** cochée, lancer. Vérifier
   dans les logs (ou dans l'artefact `veille-logs` téléchargeable en bas de
   la page du run) que l'exécution se termine sans erreur — aucune
   écriture, aucun email, mêmes messages qu'en `--dry-run` local.
4. Relancer une fois manuellement avec la case décochée pour un premier
   test réel (écriture + email si `only_if_changes` le permet), ou
   attendre simplement la prochaine échéance : le cron hebdomadaire
   (chaque lundi ~07h heure de Paris) s'en charge ensuite tout seul.

**Ce que le workflow fait à chaque exécution réelle (cron ou manuelle,
case décochée) :** il lance `main.py` puis, s'il y a eu du nouveau,
recommit lui-même `diabete-type1-veille.md`, `dedup_store.json` et
`logs/last_run_summary.json` sur la branche `main`. **Le dépôt GitHub
devient alors la version de référence du document** — faites `git pull`
avant de l'éditer en local pour ne jamais repartir d'une version périmée,
et évitez de laisser la tâche planifiée Windows locale active *en même
temps* en écriture réelle (les deux éditeraient le même contenu depuis
deux endroits différents, avec un risque de conflit/écrasement au
prochain `git pull`/`push`).

## Fichiers

| Fichier | Rôle |
|---|---|
| `main.py` | Script principal (collecte, classification, résumé, traduction, correspondance fiche/nouveauté, insertion). |
| `config.yaml` | Sources, requêtes PubMed, flux RSS, mots-clés de classification, mots-clés de routage bénéfice/risque, fenêtre temporelle. |
| `dedup_store.json` | Clés (DOI/NCT/URL) déjà traitées — jamais retraitées — ainsi qu'un instantané des fiches connues (`known_fiches`, informatif). Mis à jour uniquement en exécution réelle. |
| `diabete-type1-veille.md` | Document cible ; seules les sections 2-5 peuvent être modifiées (fiches mises à jour ou nouvelles entrées section 5) ; jamais les sections 1, 6, 7. |
| `run_veille.ps1` | Lance `main.py` puis affiche la notification Windows de résultat. À utiliser au quotidien. |
| `register_task.ps1` | Enregistre la tâche planifiée Windows hebdomadaire (appelle `run_veille.ps1`). |
| `logs/veille.log` | Journal détaillé de chaque exécution, avec rotation automatique (2 Mo × 10 fichiers). |
| `logs/last_run_summary.json` | Résumé structuré de la dernière exécution (compteurs, fiches mises à jour, nouvelles entrées, avertissements) — alimente la notification et sert de point de départ pour le débogage. |
| `email_credentials.example.yaml` | Modèle pour `email_credentials.yaml` (à créer soi-même, jamais suivi par git) contenant le mot de passe d'application Gmail. |
| `.github/workflows/veille.yml` | Planificateur cloud optionnel (GitHub Actions) — voir section dédiée plus bas. |

## Traduction : pas de clé API

Vous avez choisi de ne fournir aucune clé (DeepL/OpenAI/Anthropic). Le
script utilise donc deux services de traduction gratuits sans clé (via
`deep-translator`) : Google Translate (non officiel) en premier, puis
MyMemory en repli si le premier échoue. **Conséquence importante** :
comme il n'y a pas de clé, on ne peut pas non plus faire *rédiger* un
résumé par un modèle — le "Résumé (FR)" est donc une **traduction pure
d'un extrait extractif** (les premières phrases de l'abstract/du
communiqué, sans reformulation), ce qui respecte de fait l'interdiction
d'inventer un détail absent de la source, mais donne un résumé un peu
moins fluide qu'une reformulation par IA. Si vous obtenez plus tard une
clé Anthropic/OpenAI, il suffira de remplacer `translate_fr()` par un
appel à cette API avec un prompt strict (« ne reformule qu'à partir de
CE texte, n'ajoute aucune information absente »).

En cas d'échec des deux moteurs gratuits (quota, blocage réseau), le
texte source est conservé, préfixé de `[traduction automatique
indisponible]`, plutôt que d'insérer une traduction inventée ou de faire
échouer toute l'exécution.

## Flux RSS — statut de vérification (18/09/2026)

| Source | Statut | Note |
|---|---|---|
| FDA presse | Vérifié (XML valide) | |
| EMA actualités | Vérifié (XML valide) | |
| ANSM actualités | Vérifié (XML valide) | |
| Breakthrough T1D (ex-JDRF) | Vérifié (XML valide) | |
| Vertex Pharmaceuticals | Vérifié (XML valide) | |
| Eli Lilly | Vérifié (XML valide) | |
| Dexcom | **404 confirmé** sur le schéma d'URL Q4 (identique à Vertex/Lilly) | Désactivé dans `config.yaml`. URL à corriger manuellement. |
| Insulet | **404 confirmé** sur le schéma d'URL Q4 | Désactivé, même raison. |
| Medtronic Diabetes | **Pas de RSS public** | Désactivé dans `config.yaml`. L'activer nécessiterait un scraping HTML généraliste, explicitement exclu comme source principale par le cahier des charges. |
| Fédération Française des Diabétiques | **Pas de RSS public** | Désactivé, même raison. |

Si un flux RSS tombe en panne (URL changée, 404, XML invalide), le
script journalise un avertissement et continue avec les autres sources
— une source RSS cassée n'interrompt jamais toute l'exécution.

## Logique à deux niveaux : comment fonctionne la correspondance

**Extraction des identifiants** (`extract_identifiers`) : pour une fiche
titrée `### Zimislecel (VX-880) --- îlots dérivés de cellules souches`, le
script coupe uniquement sur le séparateur de titre (`---` ou `—`) et
retient le membre de gauche : `"zimislecel (vx-880)"`, `"zimislecel"` et
`"vx-880"` (contenu des parenthèses). C'est de la manipulation de chaîne,
jamais une lecture du sens de la fiche.

**Correspondance** (`match_item_to_fiche`) : une nouveauté est associée à
une fiche si l'un de ces identifiants apparaît, mot entier, dans son titre
(et pour les identifiants de 4 caractères ou plus, aussi dans son résumé).
Les identifiants très courts (≤ 3 caractères, ex. "ATG") ne sont cherchés
que dans le titre, pour limiter les faux positifs. **Si plusieurs fiches
correspondent à la fois, l'automate refuse de choisir** : l'élément part
en section 5 comme nouveauté ordinaire plutôt que de risquer de modifier
la mauvaise fiche — vérifiez le journal (`logs/veille.log`) pour ces cas.

**Garde-fou confiance** : une nouveauté de confiance **Faible** ne met
jamais à jour une fiche, même si elle matche un identifiant — elle part
en section 5 (avec la mention « non confirmé »), pour ne jamais laisser
une rumeur non confirmée modifier une fiche curée manuellement.

**Gabarit d'une fiche technologie** (sections 2-4) — chaque fiche `### ...`
suit ce format, dans cet ordre :
```
### Nom de la technologie --- description courte

-   **Maturité** : [Préclinique / Phase 1 / Phase 2 / Phase 3 / Commercialisé / non précisé dans la source]
-   **Pays** : [pays de l'organisme/promoteur principal, ou "Multi-national"
    si l'essai est multicentrique international, ou "non précisé dans la
    source" si l'information est absente de la source consultée]
-   **Bénéfices** : [...]
-   **Inconvénients & risques** : [...]
-   **Niveau de confiance** : [jugement curé manuellement — jamais réécrit par l'automate]
-   **Source principale** : [...]
```

**Construction des champs mis à jour** (`build_fiche_updates`) — le
cahier des charges ne précisant pas comment répartir un résumé entre
"Bénéfices" et "Inconvénients & risques", ce choix a été fait :
- **Maturité** : remplacée (pas cumulée) si la nouveauté indique un statut
  concret (ex. "Phase 3") ; sinon laissée telle quelle.
- **Pays** : renseigné une seule fois, à partir du champ pays fourni par la
  source (PubMed/ClinicalTrials.gov/RSS) — jamais si la fiche a déjà un
  pays renseigné (curé manuellement ou par une exécution précédente), pour
  ne jamais écraser une information déjà validée.
- **Bénéfices / Inconvénients & risques** : le résumé traduit est coupé en
  phrases, chacune classée par présence d'un mot-clé de risque
  (`risk_keywords` dans `config.yaml` — "risque", "hypoglycémie",
  "immunosuppression", "décès"...) ; une phrase sans mot-clé va par
  défaut en "Bénéfices". Le texte est **ajouté** (préfixé `[JJ/MM/AAAA]`)
  à la suite du texte existant, jamais effacé — c'est un tri par
  mot-clé déterministe, pas une classification sémantique.
- **Source principale** : la nouvelle source est ajoutée à la suite
  (séparateur `;`), sauf si son URL est déjà présente dans le champ.
- Seules les lignes des champs effectivement modifiés sont réécrites (au
  nouveau format strict une ligne), dans le fichier : le titre `###`, le
  "Niveau de confiance" et les fiches non concernées restent
  **pixel pour pixel identiques**, y compris leur ancien formatage
  multi-lignes s'ils n'ont jamais été touchés par l'automate.

## Débogage

**Trois niveaux d'information, du plus rapide au plus détaillé :**

1. **`logs\last_run_summary.json`** — un coup d'œil suffit : fenêtre de
   dates, nombre d'éléments trouvés/filtrés par source, fiches mises à
   jour, nouvelles entrées, nombre d'avertissements, et l'erreur exacte si
   l'exécution a échoué (`error`). C'est ce fichier que lit
   `run_veille.ps1` pour construire la notification, et c'est le premier
   réflexe pour comprendre un résultat inattendu sans rouvrir tout le
   journal.
2. **`logs\veille.log`** — journal texte complet de chaque exécution
   (INFO par défaut), avec rotation automatique (2 Mo par fichier, 10
   fichiers conservés, soit plusieurs mois d'historique à raison d'une
   exécution par semaine).
3. **`--debug`** — à ajouter à n'importe quelle commande pour obtenir, en
   plus, le détail de chaque décision de correspondance fiche/nouveauté
   (`Correspondance pour '...' : <fiche> / aucune -> nouvelle entrée`),
   utile pour comprendre pourquoi un élément précis a atterri là où il a
   atterri :
   ```powershell
   .\.venv\Scripts\python.exe main.py --dry-run --debug --limit 5
   ```

**Exemple de `last_run_summary.json` :**
```json
{
  "generated_at": "2026-09-18T12:00:00+00:00",
  "dry_run": false,
  "window_start": "2026-09-01",
  "window_end": "2026-09-18",
  "sources": {
    "pubmed": {"found": 37, "filtered_out": 0, "error": null},
    "clinicaltrials": {"found": 39, "filtered_out": 33, "error": null},
    "rss": {"found": 4, "filtered_out": 13, "error": null}
  },
  "found_total": 80,
  "after_dedup": 8,
  "fiches_updated": [
    {"title": "...", "new_maturity": null, "benefit_additions": ["[18/09/2026] ..."], "risk_additions": [], "source_additions": ["PubMed ; https://..."]}
  ],
  "new_entries": [
    {"date": "18/09/2026", "title": "...", "axis": "Mécanique", "doc_type": "Article", "maturity": "...", "summary": "...", "confidence": "Élevé", "confidence_note": "", "source_name": "PubMed", "source_url": "https://..."}
  ],
  "warnings_count": 2,
  "warnings": ["... RSS Vertex ... impossible de lire le flux ...", "..."],
  "error": null
}
```
Ce fichier est réécrit à **chaque** exécution (dry-run compris) et reflète
toujours la toute dernière tentative, y compris en cas d'échec complet
(`error` renseigné, tous les compteurs à 0).

## Règles de confiance (implémentées dans `classify_confidence`)

- **Élevé** : ClinicalTrials.gov (a toujours un NCT), PubMed avec DOI, ou
  communiqué RSS citant lui-même un DOI/NCT.
- **Moyen** : PubMed sans DOI récupéré, ou communiqué officiel
  d'un domaine listé dans `official_org_domains` (config.yaml) sans
  DOI/NCT.
- **Faible** : tout le reste (association sans DOI/NCT, domaine non
  reconnu) — toujours inséré avec la mention explicite « non confirmé »,
  jamais supprimé silencieusement.

## Limites connues

- Pas de recherche web générale automatisée : le cahier des charges
  l'exige uniquement en complément et jamais comme seule source pour une
  confiance « Élevée » ; comme aucune clé d'API de recherche web n'a été
  fournie, cette source n'est pas implémentée pour l'instant (PubMed +
  ClinicalTrials.gov + RSS suffisent à couvrir les sources primaires et
  les communiqués officiels). Elle peut être ajoutée plus tard sans
  changer l'architecture (nouvelle fonction `fetch_web_search`, résultats
  marqués `category: "media"` → confiance Faible par construction).
- Le pays d'origine n'est fiable que pour ClinicalTrials.gov (lieux des
  centres). Pour PubMed, c'est le pays de la revue (pas nécessairement
  celui de l'étude) ; pour les flux RSS, c'est le pays configuré pour
  l'organisme. Quand l'information manque vraiment, le script écrit
  « non précisé dans la source » plutôt que de deviner.
- La classification par axe (Biologique/Mécanique/Immunothérapie) et par
  maturité repose sur des mots-clés (`config.yaml`, sections
  `axis_keywords` / `maturity_keywords`) : ajustez ces listes si une
  entrée est mal classée.
