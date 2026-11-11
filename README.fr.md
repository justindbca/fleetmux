# fleetmux

[![tests](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml/badge.svg)](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml)

[English](README.md) · [中文](README.zh-CN.md) · **Français** · [Español](README.es.md)

**Pilotez des équipes d'agents IA comme vous gérez des conteneurs.** Une équipe, pas un simple assistant : fleetmux fait tourner un agent superviseur et N agents
workers sous forme de sessions de terminal visibles. Le superviseur ne fait que juger et répartir le travail ; les workers
ne font qu'exécuter ; c'est un daemon, et non les modèles, qui décide quand le travail est terminé. Une session proche de sa
limite de contexte passe le relais à une nouvelle session au moyen d'une note de passation écrite, si bien qu'une même tâche
peut tourner pendant des heures à travers de nombreuses sessions de modèle. **Claude Code** officiel, **Codex CLI** officiel,
**Pi** (modèles Codex et modèles locaux) et **opencode** cohabitent dans une même flotte, dans toute combinaison
superviseur × worker validée par un vrai test de fumée (`fleetmux/config/fleet.yaml#cli_pairs`).

**En production au quotidien.** Depuis octobre 2026, fleetmux fait tourner des flottes de service résidentes et des flottes
de tâches planifiées sur trois Mac pour une activité de trading réelle : une équipe d'exploitation qui démarre et arrête les
autres équipes, un desk de trading, une équipe de revue de stratégie qui répond aux déclenchements sur les prix et les
indicateurs, et une équipe de recherche planifiée. Chaque équipe a un chef IA et des workers IA ; l'équipe d'exploitation
pilote le desk de trading et l'équipe de revue comme ses sous-flottes.

## Un chef de projet, pas un secrétaire

Les agents de type assistant personnel savent déjà lancer des sous-agents, exécuter des tâches planifiées et basculer d'un
modèle à l'autre en cas de panne. Mais le résultat d'un sous-agent repart encore vers un modèle pour être jugé, les longues
tâches reposent sur la compaction du contexte, et le modèle utilisé est celui que nomment l'appelant ou la configuration.
fleetmux ajoute ce que fait un chef de projet :

1. **Livraison validée par le programme.** L'existence des livrables, la présence de ce qu'ils doivent contenir et le
   passage des tests sont établis par le daemon, pas repris du compte rendu du modèle.
2. **Des sessions qui se prolongent sans fin.** Près de la limite de contexte, une session rédige une note de passation et
   une nouvelle session prend sa place ; une session figée redémarre sur place ; le daemon et le superviseur se surveillent
   mutuellement.
3. **Répartition selon une matrice de capacités.** Avant chaque envoi, le conseiller choisit le modèle et le niveau d'effort
   dans une matrice de capacités (domaine × type de tâche × modèle, chaque case étayée par des preuves), et ce choix doit être
   validé par cette même matrice ; un plafond d'usage fait passer l'emplacement au modèle utilisable suivant de la chaîne de
   son rôle, ou met la tâche en pause.
4. **Visible de bout en bout.** Chaque agent est une session de terminal visible, que vous pouvez observer et reprendre en
   main à tout moment.
5. **CLI officiels, votre propre connexion.** Chaque agent tourne dans le CLI de son éditeur, avec votre propre connexion ;
   aucun jeton n'est extrait, l'identité d'aucun client n'est usurpée et aucun appel d'API ne passe par un proxy.
6. **Un budget par tâche.** Une carte de budget plafonne les appels, les tours, les heures et le quota par compte ; une table
   des plafonds d'usage fait descendre d'un cran de modèle ou met l'exécution en pause pour la reprendre plus tard.
7. **Des équipes d'équipes.** Un worker peut devenir le chef de sa propre sous-flotte : une seule flotte résidente peut ainsi
   mener plusieurs activités, chacune démarrée et arrêtée selon son propre calendrier.

Nous avons compris pourquoi c'est important avec notre propre installation : une passerelle d'assistant que nous utilisions a
perdu ses canaux de discussion pendant des mois sans que rien à l'extérieur ne s'en aperçoive, parce que rien à l'extérieur ne
vérifiait. Dans une flotte, le daemon vérifie chaque livraison.

Conçu par Justin Wang (Toronto), ingénieur en systèmes d'IA : [LinkedIn](https://www.linkedin.com/in/justinwangto) · [X](https://x.com/alaboluo).
Disponible pour des missions en freelance : mise en place d'environnements d'IA de A à Z, optimisation d'environnements
d'exécution d'agents, exploitation des capacités des LLM et orchestration multi-agents. Écrivez-moi en message privé sur X.

[Référence de conception](docs/design.md) (en anglais)

**Points forts**

- **Validation littérale.** Une tâche ne se termine que lorsque les livrables déclarés existent et contiennent ce qu'ils
  doivent contenir ; les fausses déclarations `COMPLETED` sont comptées et bloquent la tâche.
- **Rotation du contexte.** À une proportion configurable de la fenêtre de contexte, la session rédige une note de passation
  et un successeur (`worker-1` → `worker-1.1`) prend la suite dans le même onglet.
- **Un Redis privé par exécution** (socket Unix uniquement, pas de TCP) qui porte un bus de messages borné, avec des
  livraisons contrôlées par numéro de génération : un rappel tardif d'une session retirée ne peut jamais agir.
- **Chien de garde et frein.** Détection de blocage à cinq états pour chaque emplacement ; une carte de budget par tâche
  (appels, tours, heures, quota par compte) et une table des plafonds d'usage qui fait descendre un emplacement bloqué le long
  de sa chaîne de modèles ou met l'exécution en pause de façon reprenable. Les comptes se trouvent dans le bloc `accounts` de
  votre copie privée du fichier de configuration, jamais dans le code.
- **Un seul fichier de configuration.** Chaque table (paires de CLI, modes de cycle de vie, plafonds, dialogues du chien de
  garde, fiches de modèles, profils, comptes) est un bloc de `fleetmux/config/fleet.yaml` ; un `~/.fleetmux/fleet.yaml`
  privé au même format remplace les blocs qu'un opérateur a le droit de modifier.
- **CLI officiels, votre propre connexion.** Claude Code tourne toujours sous la forme du TUI officiel `claude`, avec votre
  propre connexion, et Pi ne reçoit jamais d'identifiants Claude ; les modèles Codex tournent dans le Codex CLI officiel ou via
  la connexion OpenAI propre à Pi. fleetmux n'extrait ni ne transmet de jetons, ne se fait passer pour aucun client et ne fait passer aucun
  appel d'API par un proxy.
- **Sous-flottes.** Un worker peut devenir le superviseur de sa propre flotte enfant.
- **Plus de 2 000 tests** (Redis en mémoire, faux cmux, transcriptions synthétiques ; vrai Redis là où c'est important).

```
 cmux workspace "Fleet agents" (one page)
 ┌───────────────────────────────┬────────────────────────┐
 │ supervisor-1.2                │ [worker-1.1] [worker-2] │
 │ supervisor TUI (selected)     │ worker tabs            │
 │                               │                        │
 ├───────────────────────────────┤ selected worker's TUI  │
 │ [history] [daemon]            │                        │
 │ retired screens + handoffs    │                        │
 └───────────────────────────────┴────────────────────────┘
 daemon: the fleet daemon, private Redis socket, routing, verification and watchdog.
```

À gauche, le TUI du superviseur (sélectionné) ; à droite, les onglets des workers avec, en dessous, le TUI du worker
sélectionné ; en bas à gauche, les onglets `history` (écrans retirés et notes de passation) et `daemon` (le daemon de la
flotte : socket Redis privé, routage, vérification et chien de garde).

## Cas d'usage

Chaque `start` indique un `--lifecycle` ; il détermine ce que « terminé » veut dire et la façon dont l'exécution se clôt
(`fleetmux/config/fleet.yaml#lifecycle_modes`). Voici les configurations pour lesquelles fleetmux est conçu ; le rapport
planifié, le service résident et le développement de fonctionnalités tournent au quotidien.

- **Des opérations limitées aux modèles locaux.** Certains environnements n'autorisent que des modèles qui tournent sur vos
  propres machines, et les modèles locaux collaborent mal : ils perdent le fil de qui fait quoi, annoncent un travail comme
  fini alors qu'il ne l'est pas, débordent une petite fenêtre de contexte ou se figent en plein tour. Une partie du problème
  tient au modèle, une autre à l'environnement d'exécution qui l'entoure. fleetmux retire la collaboration aux modèles pour la
  confier au daemon. Le superviseur ne fait que choisir l'étape suivante et un worker ne fait que l'exécuter ; c'est le daemon
  qui décide quand un tour est terminé, si le résultat existe et dit ce qu'il doit dire (fichiers vérifiés littéralement,
  réponses de service confrontées à `reply.required` ; trois fausses déclarations `COMPLETED` bloquent la tâche), quand une
  session passe le relais à une nouvelle (fenêtre et ratio par modèle) et quand une session figée est redémarrée. Superviseur
  et workers peuvent tous être des modèles locaux via Pi (`--supervisor-cli pi --worker-cli pi --pi-model <provider>/<model>`) ;
  `--max-busy` maintient les requêtes simultanées sous la limite mémoire du serveur d'inférence ; le Redis privé de la flotte
  n'écoute que sur un socket Unix et n'ouvre aucun port.
- **Surveillance de trading : des stratégies confiées à l'IA, revues sur déclenchement.** C'est votre propre programme qui
  surveille les prix et les indicateurs ; le modèle n'est pas sollicité toute la journée. Lorsqu'un niveau de prix ou un
  paramètre d'indicateur est franchi, le programme envoie une requête, par exemple `{"to": "exit-1", "payload": {strategy,
  position, trigger, data}, "reply": {"required": ["verdict", "param", "evidence"]}}`, et le worker titulaire de ce rôle (un
  prompt par rôle via `--roles`, ses outils et skills via `--worker-kit`) mène une revue complète : il lit la position et les
  données de marché, revérifie les hypothèses de la stratégie et répond en JSON. Une requête qui indique `to` saute le tour du
  superviseur ; les déclenchements simultanés font la queue sous `--max-busy` ; le daemon contrôle les champs de la réponse
  avant que votre programme ne la lise (`fleetmux results --drain`). Les requêtes n'ont pas d'échéance globale : une requête
  qui expire n'immobilise que son propre emplacement, et un worker Claude Code figé reprend sa propre session et reçoit de
  nouveau la même requête. La flotte ne passe aucun
  ordre et ne connaît aucun calendrier : votre programme décide quoi faire du verdict, fait passer tout ordre par ses propres
  contrôles et met la flotte en `pause` / `resume` autour des heures de marché.
- **Un modèle de puissance moyenne à la tête d'une foule de modèles gratuits plus faibles, pour le travail courant.**
  Traduction, rédaction, synthèse et tri sont surtout une affaire de volume. Un superviseur de puissance moyenne (via Pi ou
  Codex) découpe le travail en petits morceaux, un chapitre, un lot de documents, un sujet, et déclare les fichiers que la
  tâche doit produire, la longueur de chacun et ce que chacun doit contenir. Des workers sur des modèles gratuits via Pi
  traitent les morceaux (un modèle par ligne du roster, `pi_model:`). Un modèle faible ne voit jamais qu'un petit morceau :
  `"fresh": true` sur un élément de répartition lance une session vierge dans le même emplacement, pour que le morceau
  précédent ne déborde pas sur le suivant, et un long morceau passe le relais à une nouvelle session avant qu'une petite
  fenêtre ne déborde. Les erreurs d'un modèle faible sont détectées par le programme au lieu d'être prises pour argent
  comptant : la tâche ne se clôt que lorsque chaque livrable déclaré existe, est assez long et contient le texte requis, et
  trois fausses déclarations `COMPLETED` la bloquent. Les points d'accès gratuits sont instables et plafonnés : limites de
  débit et plafonds d'usage passent par la table des plafonds, qui fait descendre l'emplacement le long de sa chaîne de modèles
  ou met l'exécution en pause de façon reprenable ; `--max-busy` reste sous la limite de concurrence de l'offre gratuite ; on
  ajoute ou retire des workers en modifiant le YAML du roster pendant que la flotte tourne. Pour une passe de qualité, donnez à
  une ligne du roster un `pi_model:` plus puissant et utilisez-la comme relecteur.
- **Rapport de recherche planifié** (`scheduled`). Le planificateur de l'appelant démarre une flotte à heures fixes dans la
  journée ouvrée (avec `clock_gate` activé, la flotte refuse elle-même de se lancer un jour de fermeture du marché) : un
  superviseur et deux workers Claude Code dotés de skills de recherche et d'un MCP de recherche web via `--worker-kit`.
  L'objectif déclare le fichier du rapport et les sections qu'il doit contenir ; le daemon le valide littéralement, impose un
  budget de temps strict à l'exécution et ferme l'espace de travail à la fin.
- **Service de jugement résident** (`service`). Un autre programme envoie des requêtes JSON avec `fleetmux request` ; une
  requête qui indique `to` va directement au worker de ce rôle (`--worker-names exit-1,reentry-1,...`, un prompt par rôle via
  `--roles`). Le daemon contrôle chaque réponse au regard de `reply.required` / `reply.allowed`, et l'appelant suspend la
  flotte en dehors de ses heures de travail avec `pause` / `resume`. Voir
  [Service mode](README.md#service-mode-resident-fleet) dans le README en anglais.
- **Développement de fonctionnalités avec revue indépendante** (`dev_task`). Le superviseur découpe la fonctionnalité, des
  workers la construisent et des workers relecteurs distincts la jugent ; la revue est limitée à trois tours et les P0 / P1
  restants vous reviennent au lieu d'un quatrième tour. La tâche n'est terminée que lorsque le `test_cmd` de la porte de
  livraison est passé et que le travail est fusionné et remis à la file de publication. Les contrats d'interface
  (`"contracts": true`) figent les interfaces partagées avant le début du travail.
- **Programme en plusieurs phases** (`program`). Chaque phase tourne comme sa propre flotte enfant `dev_task` ; un registre
  consigne les phases, et le programme se termine quand la dernière phase est fusionnée et revue.
- **Revue jusqu'à validation** (`review_loop`). Revue → correction → revue, trois tours au plus, puis un rapport.
- **Une tâche qui dépasse un seul worker** (en développement). Quand un worker reste occupé par un flux de travail au-delà
  d'une durée configurable, on lui demande de proposer un découpage ; avec l'accord du superviseur (et les modèles choisis),
  le worker passe le relais et son successeur devient le superviseur d'une flotte enfant, affichée en direct dans la zone des
  workers du parent. Un worker peut aussi le demander de lui-même. Voir
  [Sub-fleets](README.md#sub-fleets-a-worker-becomes-a-sub-fleet-supervisor-development) dans le README en anglais et
  `docs/design.md` §44.
- **Une seule flotte d'exploitation résidente pour plusieurs activités** (en développement). Un superviseur principal en mode
  service dispose d'un worker par activité ; chacun de ces workers supervise une flotte enfant nommée, déclarée dans le roster,
  qu'un programme extérieur peut démarrer et arrêter selon son propre calendrier (`docs/design.md` §29).
- **Modèles de jour et de nuit.** `start --roster day.yaml --night-roster night.yaml` et `fleetmux night on|off` basculent,
  à un moment d'inactivité, chaque worker dont le profil diffère vers son profil de nuit (par exemple un modèle gratuit ou
  local), sous les mêmes noms d'emplacement, et indiquent au superviseur ce qui est fait et ce qui reste (un cycle complet
  jour → nuit → jour n'est pas encore validé de bout en bout). `start --resume` relance de la même manière une exécution
  interrompue.
- **Tourne partout, se suit de partout.** Sous Linux, le backend `fleetmux` remplace cmux ; `fleetmux attach` (y compris via
  `ssh -t`) ou `fleetmux gui` affiche la page en direct depuis un autre terminal, y compris une application de terminal sur
  téléphone.

Des canaux de discussion (Telegram, Feishu, WeChat, e-mail, Slack) servent de second clavier au superviseur, avec les
résultats et les « en attente d'un humain » renvoyés dans la même conversation : `fleetmux channel`,
[contracts §8](docs/contracts.md).

## Installation

Une seule commande : `fleetmux <subcommand>` (par exemple `fleetmux start`, `fleetmux status`, `fleetmux channel`). Le binaire
exécute lui-même ses sous-commandes de terminal (`server`, `attach`, `reap`, `web`, `gui --runtime D`) et transmet toutes les
sous-commandes de flotte au CLI Python de la même version (docs/design.md §50). Une machine installée sous l'ancien
nom du projet déplace ses données une fois avec `fleetmux home migrate` (docs/design.md §50.1).

```bash
git clone https://github.com/justindbca/fleetmux && cd fleetmux
pip install -e .
```

Ajoutez un profil Codex pour les agents. Codex ≥ 0.156 range chaque profil dans son propre fichier
(voir [`codex.config.example.toml`](codex.config.example.toml)) :

```toml
# ~/.codex/fleet.config.toml   (selected with `codex --profile fleet`)
model = "gpt-5.5"
```

Rien d'autre : la flotte crée son propre home Codex pour les agents et répond elle-même, pour chaque répertoire d'agent, à
la question de Codex « faire confiance à ce dossier ? ».

Les prérequis (cmux, Codex CLI, `redis-server`, version de Python) et l'installation par version sur une machine cible
(`scripts/install_release.sh`) sont décrits dans le README en anglais :
[Requirements](README.md#requirements) et [Release install](README.md#release-install-on-a-target-machine-macos-or-linux).

## Démarrage rapide

Ouvrez un terminal cmux, configurez votre fournisseur et votre modèle Pi, et remplacez `local/my-model` ci-dessous. Les
nouveaux démarrages exigent Redis v2 et une activation explicite du mode développement. Sans `--supervisor-cli` /
`--worker-cli`, `start` prend la paire par défaut dans `fleetmux/config/fleet.yaml#cli_pairs` : **un superviseur Pi sur
`openai-codex/gpt-6-astra` (thinking high) avec des workers Claude Code officiels**. Cette table liste chaque paire prise en
charge et son statut (voir [Redis v2 development runtime](README.md#redis-v2-development-runtime) dans le README en
anglais) ; une paire absente de la table est refusée. `start` exige aussi `--lifecycle <mode>` (`service`, `dev_task`,
`program`, `scheduled`, `review_loop`, `smoke` ; voir `fleetmux/config/fleet.yaml#lifecycle_modes`) ; une paire
`unverified` ne démarre qu'avec `--lifecycle smoke`. Les exemples de configuration et de lancement propres à Codex, dans le
README en anglais, ne sont conservés qu'à titre de référence v1 historique.

```bash
fleetmux start --workers 2 --supervisor-cli pi --worker-cli pi --lifecycle <mode> \
  --pi-model local/my-model --allow-unsandboxed-pi --allow-development-v2
echo '{"goal": "Survey the three most-cited papers on X and write a comparison",
       "hours": 4,
       "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["Conclusion"]}]}' \
  | fleetmux task -                           # hand the goal to the supervisor
fleetmux status                               # task + per-agent state, context usage, inbox depth
fleetmux stop                                 # cancel; confirm runtime retirement before Redis shutdown
```

`fleetmux task -` confie l'objectif au superviseur ; `fleetmux status` affiche la tâche, l'état de chaque agent,
l'occupation du contexte et la profondeur de la boîte de réception ; `fleetmux stop` annule, en confirmant le retrait du
runtime avant l'arrêt de Redis.

## Référence complète

Pour la référence complète, consultez le [README en anglais](README.md) : prérequis, installation par version, backends de
terminal (cmux, WezTerm, fleetmux) et options de `start`, disposition et page d'historique, mode service, roster YAML des
workers, fenêtre et ratio de passation, reprise d'une exécution, contrats d'interface, sous-flottes, agents Claude Code et Pi,
runtime de développement Redis v2, cheminement d'une tâche, clés Redis, état des tests, carte des modules, limites connues et
développement. La référence de conception se trouve dans [docs/design.md](docs/design.md).

Licence : MIT
