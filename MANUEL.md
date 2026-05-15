# Manuel technique — Migration automatique LXC → OpenStack

**Projet de fin d'études — Master Informatique**
**Auteurs :** Amine et Safa Imad Mouhamed
**Encadrant :** KHIAT Abd Elhamid

---

## Table des matières

1. [Contexte et objectif](#1-contexte-et-objectif)
2. [Architecture de l'infrastructure](#2-architecture-de-linfrastructure)
3. [Outils utilisés et pourquoi](#3-outils-utilisés-et-pourquoi)
4. [Les 9 phases de migration](#4-les-9-phases-de-migration)
5. [Fonctionnement du scanner LXC](#5-fonctionnement-du-scanner-lxc)
6. [Génération dynamique des playbooks Ansible](#6-génération-dynamique-des-playbooks-ansible)
7. [Provisioning Terraform dynamique](#7-provisioning-terraform-dynamique)
8. [Sécurité](#8-sécurité)
9. [Comment reproduire la migration de zéro](#9-comment-reproduire-la-migration-de-zéro)
10. [Structure du projet](#10-structure-du-projet)
11. [Erreurs de conception corrigées](#11-erreurs-de-conception-corrigées)

---

## 1. Contexte et objectif

### Contexte

Ce projet est réalisé dans le cadre d'un Projet de Fin d'Études (PFE) de Master en Informatique. Il répond à un besoin réel de modernisation d'infrastructure : faire passer des services réseau hébergés dans des **containers LXC** (technologie de virtualisation légère, non cloud-native) vers un **cloud privé OpenStack**, sans intervention manuelle service par service.

L'infrastructure source est une machine virtuelle (`vm-source`) qui fait tourner cinq containers LXC représentant des services réseau classiques : base de données, serveur web, serveur FTP, partage NFS et sauvegarde automatique. La cible est un cloud privé OpenStack Caracal déployé sur une deuxième machine virtuelle (`vm-cible`).

### Problème résolu

La migration manuelle d'une telle infrastructure nécessiterait, pour chaque service :
- d'identifier ce qui tourne et comment c'est configuré
- de créer une instance OpenStack avec les bons paramètres
- de sauvegarder les données
- de les transférer et de les restaurer
- de mettre à jour toutes les références aux anciennes IPs
- de valider que tout fonctionne

Ce projet automatise **l'intégralité de ce processus** en une seule commande :

```bash
python3 src/migrate.py
```

Le script détecte automatiquement ce qui tourne dans les containers LXC, génère les fichiers Terraform et Ansible adaptés à ce qu'il a trouvé, crée les instances OpenStack, transfère et restaure les données, met à jour les IPs, puis valide. Si la migration est interrompue, elle peut **reprendre** là où elle s'est arrêtée.

### Objectif pédagogique

Démontrer l'utilisation combinée de Python, Terraform et Ansible pour concevoir un pipeline de migration Infrastructure-as-Code (IaC) dynamique, 100% automatique, sans configuration manuelle par service.

---

## 2. Architecture de l'infrastructure

### Vue d'ensemble

```
┌─────────────────────────────────┐         ┌──────────────────────────────────────┐
│           vm-source             │         │              vm-cible                │
│        Ubuntu Server            │         │         Ubuntu Server 24.04          │
│     (hôte LXC — source)         │◄───────►│      OpenStack Caracal (2024.1)      │
│                                 │  réseau │                                      │
│  ┌─────────┐  ┌──────────────┐  │ provider│  Keystone · Glance · Nova           │
│  │  LXC    │  │  orchestrateur│ │         │  Neutron · Placement · Horizon       │
│  │  daemon │  │  Python       │ │         │                                      │
│  └─────────┘  └──────────────┘  │         └──────────────────────────────────────┘
│                                 │
│  containers LXC :               │
│  ┌──────────┐ ┌──────────────┐  │
│  │ mariadb  │ │    apache    │  │
│  │10.0.3.10 │ │  10.0.3.20  │  │
│  └──────────┘ └──────────────┘  │
│  ┌──────────┐ ┌──────────────┐  │
│  │  backup  │ │     ftp      │  │
│  │10.0.3.30 │ │  10.0.3.40  │  │
│  └──────────┘ └──────────────┘  │
│  ┌──────────┐                   │
│  │   nfs    │                   │
│  │10.0.3.50 │                   │
│  └──────────┘                   │
└─────────────────────────────────┘
```

### vm-source — Machine source (hôte LXC)

**Rôle :** machine qui héberge les containers LXC à migrer. L'orchestrateur Python tourne sur cette machine et a accès direct aux containers via `lxc-attach`.

**Système d'exploitation :** Ubuntu Server (compatible LXC)

**Ce qu'elle contient :**
- Le daemon LXC qui gère les containers
- Les 5 containers LXC (voir tableau ci-dessous)
- Le code source du projet (`/home/user/PFE-migration/`)
- La clé SSH privée `~/.ssh/migration_key` pour accéder aux instances OpenStack
- Les outils `terraform`, `ansible-playbook`, `python3`

**Réseau interne LXC :** sous-réseau `10.0.3.0/24`, chaque container a une IP fixe.

### Les 5 containers LXC et leurs services

| Container | IP LXC | Service principal | Description |
|-----------|--------|-------------------|-------------|
| `mariadb` | 10.0.3.10 | MariaDB 10.11 | Serveur de base de données. Héberge les bases `app_db` et `sysmonitor`. Utilisateur `appuser` avec droits limités aux hôtes autorisés. |
| `apache` | 10.0.3.20 | Apache2 + PHP | Serveur web hébergeant l'application **SysMonitor** — dashboard de monitoring en PHP. Se connecte à MariaDB via `appuser`. |
| `backup` | 10.0.3.30 | mysqldump + cron | Pas de service réseau propre. Exécute un script `backup.sh` via cron à 2h du matin pour sauvegarder `app_db` par `mysqldump`. |
| `ftp` | 10.0.3.40 | vsftpd | Serveur FTP avec mode passif (ports 40000–40100). Plusieurs comptes FTP (`ftpuser`, `ftpuser1`, `ftpuser2`) avec leurs répertoires `home`. |
| `nfs` | 10.0.3.50 | nfs-kernel-server | Partage NFS du répertoire `/srv/nfs/shared` vers le sous-réseau interne. Sous-dossiers `documents` et `scripts`. |

### vm-cible — Cloud privé OpenStack

**Rôle :** infrastructure cible dans laquelle les instances migrées seront créées. Gérée entièrement par Terraform depuis vm-source.

**Système d'exploitation :** Ubuntu Server 24.04

**Composants OpenStack installés :**

| Composant | Rôle |
|-----------|------|
| **Keystone** | Service d'authentification et d'autorisation (Identity). Émet les tokens pour les autres services. URL : `http://10.0.0.10:5000/v3` |
| **Glance** | Registre d'images. Stocke l'image Ubuntu 22.04 utilisée comme base pour toutes les instances |
| **Nova** | Calcul. Crée et gère les instances virtuelles (VMs) |
| **Neutron** | Réseau. Gère le réseau interne `10.10.10.0/24`, le router, les ports, les security groups et les floating IPs |
| **Placement** | Service de placement des ressources (CPU, RAM) requis par Nova |
| **Horizon** | Interface web d'administration (optionnel, non utilisé par le script) |

### Réseau entre les deux VMs

**Réseau provider (externe) :** réseau physique partagé entre vm-source et vm-cible. Les floating IPs attribuées aux instances OpenStack sont routables depuis vm-source.

**Réseau interne migration (`10.10.10.0/24`) :** réseau privé créé par Terraform, uniquement accessible à l'intérieur d'OpenStack. Chaque instance reçoit une IP interne fixe sur ce réseau. Les services communiquent entre eux via ces IPs internes (ex. apache → mariadb via `10.10.10.40`).

**Floating IPs :** IPs publiques du réseau provider associées aux instances pour permettre l'accès SSH depuis vm-source pendant la migration.

**Security Groups** : Terraform crée un security group par type de service avec des règles strictes :

| Security Group | Ports autorisés | Source |
|----------------|-----------------|--------|
| `sg-mariadb` | 22 (SSH), 3306 (MySQL) | 0.0.0.0/0 pour SSH, réseau interne pour 3306 |
| `sg-apache` | 22, 80, 443 | 0.0.0.0/0 |
| `sg-backup` | 22 | 0.0.0.0/0 |
| `sg-ftp` | 22, 21, 40000–40100 | 0.0.0.0/0 |
| `sg-nfs` | 22, 2049, 111 | 0.0.0.0/0 pour SSH, réseau interne pour NFS |

---

## 3. Outils utilisés et pourquoi

### Python 3.10+

**Ce que c'est :** langage de programmation généraliste, interprété, avec une riche bibliothèque standard et un écosystème de packages.

**Pourquoi on l'a choisi :** Python est le langage de facto pour l'automatisation d'infrastructure. Il permet d'écrire en quelques lignes ce qui prendrait des pages en Bash, avec une gestion d'erreurs propre, des types, et des bibliothèques comme `paramiko` et `pydantic`.

**Ce qu'il fait dans ce projet :**
- `migrate.py` : point d'entrée unique, importe et appelle `main()` de l'orchestrateur
- `orchestrator.py` : chef d'orchestre — coordonne les 9 phases, génère les fichiers Terraform et Ansible à la volée, gère la reprise sur erreur
- `scanner.py` : lit l'état réel des containers LXC (services, bases de données, users, configs)
- `state.py` : machine à états — persiste la progression dans `migration_state.json` pour permettre la reprise

### Terraform 1.5+

**Ce que c'est :** outil d'Infrastructure-as-Code (IaC) de HashiCorp. Déclare l'infrastructure souhaitée dans des fichiers `.tf` et l'applique sur un cloud provider.

**Pourquoi on l'a choisi :** Terraform est le standard industriel pour provisionner de l'infrastructure cloud de manière déclarative et idempotente. Il gère l'état (`tfstate`), détecte les différences, et peut détruire proprement toutes les ressources en rollback.

**Ce qu'il fait dans ce projet :**
- Crée le réseau interne `10.10.10.0/24`, le sous-réseau, le router, et l'interface router
- Crée les security groups et leurs règles
- Crée la keypair SSH dans OpenStack
- Crée dynamiquement les ports réseau avec IPs fixes pour chaque instance
- Crée les instances Nova (VMs) avec le bon flavor et l'image Ubuntu 22.04
- Associe une floating IP à chaque instance
- Expose via `outputs.tf` les IPs internes et floating IPs pour que Python les lise

Le fichier `terraform.tfvars` est **généré dynamiquement** par `generer_tfvars()` depuis le résultat du scan LXC, puis **supprimé immédiatement** après `terraform apply` pour des raisons de sécurité (il contient le mot de passe OpenStack).

### Ansible 2.15+

**Ce que c'est :** outil d'automatisation de configuration. Exécute des "playbooks" (fichiers YAML) sur des machines distantes via SSH sans agent.

**Pourquoi on l'a choisi :** Ansible est idéal pour configurer des systèmes de manière idempotente. Il dispose de modules spécialisés pour MariaDB (`community.mysql`), les services systemd, apt, les fichiers, les archives — tout ce dont on a besoin pour restaurer les services.

**Ce qu'il fait dans ce projet :**
- `provision.yml` : installe les paquets logiciels sur chaque instance (mariadb-server, apache2, vsftpd, etc.)
- `restore.yml` : restaure les données — extrait les archives, importe les bases SQL, recrée les users MariaDB et FTP, met à jour les IPs dans les fichiers de config, redémarre les services
- `validate.yml` : vérifie que chaque service est opérationnel — service actif, HTTP 200, bases présentes, cron actif, exports NFS, etc.

Les trois playbooks sont **générés dynamiquement** par l'orchestrateur Python depuis le résultat du scan. Ils ne sont jamais écrits à la main.

### Paramiko

**Ce que c'est :** bibliothèque Python implémentant SSH et SFTP. Permet d'ouvrir des connexions SSH, d'exécuter des commandes et de transférer des fichiers sans passer par la commande `scp` ou `rsync`.

**Pourquoi on l'a choisi :** Paramiko permet de contrôler précisément les connexions SSH depuis Python — gestion des timeouts, des tentatives de reconnexion, du transfert fichier par fichier. Il est utilisé à deux endroits :
1. `attendre_ssh()` — vérifie qu'une instance est prête à recevoir des connexions SSH avant de continuer
2. `phase_transfert()` — ouvre une session SFTP vers chaque instance et envoie les archives dans `/tmp/migration/`

### Pydantic 2.0+

**Ce que c'est :** bibliothèque Python de validation de données par schéma. Permet de définir des modèles de données avec types et validateurs.

**Pourquoi on l'a choisi :** le scanner LXC collecte des données hétérogènes (IPs, noms de bases, configs vsftpd, exports NFS). Pydantic garantit que ces données sont valides avant d'être utilisées pour générer des fichiers Terraform et Ansible. Une IP malformée ou un nom de base avec des caractères spéciaux est rejeté avant de causer des problèmes en aval.

**Ce qu'il fait :** valide et structure les modèles `Container`, `Database`, `DBUser`, `FTPUser`, `NFSExport`, `VSFTPDConfig`, `BackupConfig`, `ApacheConfig` dans `scanner.py`.

### PyYAML

**Ce que c'est :** bibliothèque Python pour lire et écrire le format YAML.

**Ce qu'il fait :** charge `config.yml` au démarrage de l'orchestrateur. Toute la configuration non-dynamique (auth OpenStack, réseau, image, SSH, flavors, pool d'IPs) est lue depuis ce fichier.

### LXC (Linux Containers)

**Ce que c'est :** technologie de virtualisation légère au niveau du noyau Linux. Les containers LXC partagent le noyau de l'hôte mais ont leur propre système de fichiers, réseau et processus.

**Rôle dans ce projet :** LXC est la source, pas un outil du pipeline. Le scanner utilise `lxc-attach` pour exécuter des commandes à l'intérieur des containers depuis l'hôte, et accède directement au rootfs des containers via `/var/lib/lxc/{nom}/rootfs/` pour créer les archives `tar`.

---

## 4. Les 9 phases de migration

La migration est orchestrée par `orchestrator.py` via la fonction `main()`. Chaque phase est une étape atomique : si elle réussit, son nom est ajouté à `phases_ok` dans `migration_state.json`. En cas d'interruption, la reprise repart à la phase non terminée.

```
Phase 1 : Vérification prérequis
Phase 2 : Collecte credentials
Phase 3 : Scan LXC                     ← produit : liste de Container
Phase 4 : Provisioning Terraform       ← produit : instances OpenStack + IP mapping
Phase 5 : Génération inventaire        ← produit : inventory.ini, host_vars/, group_vars/, provision.yml, restore.yml, validate.yml
Phase 6 : Backup LXC                   ← produit : archives dans /tmp/tmpXXXXX/
Phase 7 : Transfert SFTP               ← produit : archives dans /tmp/migration/ sur chaque instance
Phase 8a : Ansible provision.yml       ← produit : logiciels installés sur les instances
Phase 8b : Ansible restore.yml         ← produit : données restaurées, IPs mises à jour, services reconfigurés
Phase 8c : Ansible validate.yml        ← produit : rapport de validation par service
Phase 9 : Rapport final                ← produit : migration_report.json
```

---

### Phase 1 — Vérification des prérequis

**Fonction :** `verifier_prerequis()`
**Fichiers impliqués :** `config.yml`, `~/.ssh/migration_key`

**Ce qu'elle fait :**
Avant de démarrer quoi que ce soit, le script vérifie que l'environnement est prêt :

1. **Terraform disponible** — exécute `terraform version`, vérifie code retour 0
2. **Ansible disponible** — exécute `ansible --version`
3. **Clé SSH présente** — vérifie l'existence du fichier `~/.ssh/migration_key` (chemin configurable dans `config.yml`)
4. **OpenStack accessible** — ping sur `config.network.api_ip` (10.0.0.10 par défaut)

Si un prérequis manque, la liste des manquants est affichée et le script s'arrête avec `sys.exit(1)`.

**Sortie :** affichage console avec OK/ECHEC par prérequis. Aucun fichier produit.

---

### Phase 2 — Collecte des credentials

**Fonction :** `collecter_credentials()`
**Fichiers impliqués :** aucun (saisie interactive)

**Ce qu'elle fait :**
Collecte interactivement les secrets nécessaires à la migration. Aucun secret n'est jamais écrit dans un fichier persistant ni affiché à l'écran.

Les questions posées :
- Utilisateur OpenStack (valeur par défaut depuis `config.yml`)
- Projet OpenStack (valeur par défaut depuis `config.yml`)
- Mot de passe OpenStack (via `getpass.getpass`, invisible)
- Mot de passe MariaDB root (pour les dumps via `mysqldump`)
- Mot de passe MariaDB appuser (pour créer l'utilisateur sur OpenStack)

**Sortie :** dict Python `credentials` conservé en mémoire pour toute la durée de la migration.

---

### Phase 3 — Scan des containers LXC

**Fonction :** `phase_scan(state)` → appelle `scanner_containers()`
**Fichiers impliqués :** `src/scanner.py`

**Ce qu'elle fait :**
Détecte automatiquement l'état complet de chaque container LXC en cours d'exécution. Pour chaque container, le scanner collecte : services actifs, bases de données, users MariaDB, users FTP, exports NFS, configuration vsftpd, script de backup, configuration Apache, RAM utilisée, espace disque.

Le mécanisme de détection est détaillé au chapitre 5.

**Sortie :** liste d'objets `Container` (modèles Pydantic validés). Affichage console du résumé par container.

```
  apache (10.0.3.20) ......... services : apache2
  mariadb (10.0.3.10) ........ services : mariadb
    bases : app_db, sysmonitor
  backup (10.0.3.30) ......... services : cron
  ftp (10.0.3.40) ............ services : vsftpd
  nfs (10.0.3.50) ............ services : nfs-server
```

---

### Phase 4 — Provisioning Terraform

**Fonctions :** `phase_provisioning(state, credentials, containers)` → `generer_tfvars(containers, credentials)`
**Fichiers impliqués :** `terraform/main.tf`, `terraform/variables.tf`, `terraform/outputs.tf`, `terraform/terraform.tfvars` (généré puis supprimé)

**Ce qu'elle fait :**

**Étape 4a — Génération de `terraform.tfvars` :**
`generer_tfvars()` construit dynamiquement le fichier de variables Terraform depuis :
- Les credentials OpenStack saisis en phase 2
- Le résultat du scan (liste des containers, leurs noms)
- `config.yml` (réseau, image, flavors)

Le fichier assigne une IP interne à chaque container (pool séquentiel, voir chapitre 7) et choisit le flavor approprié.

**Étape 4b — `terraform init` :** initialise le provider OpenStack si ce n'est pas déjà fait.

**Étape 4c — `terraform apply` :** crée toutes les ressources OpenStack : réseau, sous-réseau, router, security groups, keypair SSH, ports avec IPs fixes, instances Nova, floating IPs.

**Étape 4d — Lecture des outputs :** `terraform output -json` retourne les IPs internes et floating IPs de chaque instance. Ces données sont stockées dans `migration_state.json`.

**Étape 4e — Attente SSH :** pour chaque instance, `attendre_ssh()` tente une connexion SSH avec Paramiko jusqu'à réussite (timeout 120s). Cela garantit que les instances sont prêtes avant de continuer.

**Étape 4f — Suppression de `terraform.tfvars` :** le fichier contenant le mot de passe OpenStack est supprimé immédiatement après `apply`.

**Sortie :**
- Instances OpenStack créées et joignables
- `migration_state.json` mis à jour avec le mapping IP complet
- `terraform.tfvars` supprimé

---

### Phase 5 — Génération de l'inventaire Ansible

**Fonction :** `generer_inventaire(instances, containers)`
**Fichiers produits :** `ansible/inventory.ini`, `ansible/group_vars/all.yml`, `ansible/host_vars/{floating_ip}.yml` (×5), `ansible/provision.yml`, `ansible/restore.yml`, `ansible/validate.yml`

**Ce qu'elle fait :**
Génère tous les fichiers de configuration Ansible nécessaires aux phases suivantes, en se basant sur le résultat du scan (phase 3) et les IPs réelles (phase 4).

**`inventory.ini` :** liste les instances par groupe (un groupe = un container). Chaque entrée associe la floating IP, l'utilisateur SSH et la clé.

```ini
[apache]
10.0.0.197 ansible_user=ubuntu ansible_ssh_private_key_file=/home/user/.ssh/migration_key

[mariadb]
10.0.0.113 ansible_user=ubuntu ansible_ssh_private_key_file=/home/user/.ssh/migration_key
```

**`group_vars/all.yml` :** variables communes à toutes les instances — user SSH, clé, répertoire de staging, sous-réseau interne, et le mapping `new_ips` (nom → IP interne).

```yaml
new_ips:
  apache: "10.10.10.10"
  backup: "10.10.10.20"
  mariadb: "10.10.10.40"
```

**`host_vars/{floating_ip}.yml` :** variables spécifiques à chaque instance — ancienne IP LXC, IP interne, floating IP. Pour MariaDB : liste des bases et ancienne IP apache. Pour FTP : liste des users et leurs homes. Pour NFS : liste des exports.

**`provision.yml`, `restore.yml`, `validate.yml` :** générés dynamiquement. Voir chapitres 6 et 7.

---

### Phase 6 — Backup des containers LXC

**Fonction :** `phase_backup(containers, credentials)`
**Fichiers produits :** archives dans un répertoire temporaire `chmod 700`

**Ce qu'elle fait :**
Sauvegarde les données de chaque container selon son type de service. Les archives sont préfixées par le nom du container pour éviter les collisions.

| Service détecté | Action | Archive produite |
|----------------|--------|-----------------|
| `mariadb` | `mysqldump` via `lxc-attach` avec credentials root | `mariadb_app_db.sql`, `mariadb_sysmonitor.sql` |
| `apache2` | `tar` du rootfs `/var/lib/lxc/apache/rootfs/var/www` | `apache_html.tar.gz` |
| `apache2` | `tar` du rootfs `/var/lib/lxc/apache/rootfs/etc/apache2` | `apache_apache2.tar.gz` |
| `vsftpd` | `tar` du home de chaque user FTP | `ftp_ftp_ftpuser.tar.gz`, etc. |
| `nfs-server` | `tar` de `/var/lib/lxc/nfs/rootfs/srv/nfs` | `nfs_nfs_shared.tar.gz` |

Pour MariaDB, les credentials root sont écrits dans `/tmp/.my.cnf` à l'intérieur du container (protégé en `600`), utilisés pour `mysqldump`, puis supprimés immédiatement dans un bloc `finally`.

**Sortie :** répertoire temporaire contenant toutes les archives, chemin retourné pour la phase suivante.

---

### Phase 7 — Transfert des archives

**Fonction :** `phase_transfert(instances, tmp_dir, containers, state)`
**Fichiers impliqués :** archives de la phase 6, connexions SFTP Paramiko

**Ce qu'elle fait :**
Pour chaque instance OpenStack, ouvre une connexion SFTP via Paramiko et transfère les archives qui lui appartiennent dans le répertoire de staging `/tmp/migration/` (créé si absent).

La liste des fichiers à transférer est construite **dynamiquement depuis le scan** : si le container a des bases de données, leurs dumps sont transférés ; si des users FTP, leurs archives ; etc. Aucune liste hardcodée.

Mécanisme de reconnexion : jusqu'à 5 tentatives avec 10 secondes d'intervalle en cas d'échec SSH.

En fin de phase, le répertoire temporaire local est supprimé (`shutil.rmtree`).

**Sortie :** archives disponibles dans `/tmp/migration/` sur chaque instance OpenStack.

---

### Phase 8a — Provisionnement logiciel (Ansible provision.yml)

**Fonction :** `phase_ansible(state, Phase.PROVISION, ...)`
**Fichiers impliqués :** `ansible/provision.yml` (généré phase 5), `ansible/inventory.ini`, `ansible/ansible.cfg`

**Ce qu'elle fait :**
Exécute `ansible-playbook provision.yml` sur toutes les instances. Ce playbook :

1. **Play commun (toutes les instances) :** désactive le reverse DNS SSH, attend la fin de cloud-init, déverrouille apt, met à jour le cache, installe `curl`, `rsync`, `ca-certificates`

2. **Play par instance :** installe les paquets spécifiques au service détecté :
   - `mariadb` → `mariadb-server`, `mariadb-client`, `python3-pymysql`
   - `apache` → `apache2`, `php`, `libapache2-mod-php`, `php-mysql`, `php-curl`
   - `ftp` → `vsftpd`
   - `nfs` → `nfs-kernel-server`, `nfs-common`
   - `backup` → `mariadb-client`, `cron`

   Puis démarre et active chaque service via systemd.

**Sortie :** instances OpenStack avec les logiciels installés et services démarrés.

---

### Phase 8b — Restauration des services (Ansible restore.yml)

**Fonction :** `phase_ansible(state, Phase.RESTORE, ..., extra_vars={"mariadb_appuser_password": ...})`
**Fichiers impliqués :** `ansible/restore.yml`, `ansible/host_vars/`, `ansible/group_vars/all.yml`

**Ce qu'elle fait :**
C'est la phase la plus complexe. Elle restaure les données et reconfigure chaque service pour fonctionner dans le nouvel environnement OpenStack avec les nouvelles IPs internes.

Le mot de passe `mariadb_appuser_password` est transmis à Ansible via `--extra-vars` en JSON.

**Par service :**

**MariaDB :**
- Importe les bases SQL depuis le staging
- Supprime l'ancien `appuser@ancienne_ip_lxc`
- Crée `appuser@{nouvelle_ip_apache}` avec tous les droits sur les bases applicatives
- Crée `appuser@{nouvelle_ip_backup}` avec droits SELECT uniquement
- Configure `bind-address` sur l'IP interne de l'instance MariaDB
- Redémarre MariaDB, flush privileges

**Apache :**
- Extrait les archives `/var/www/` et `/etc/apache2/`
- Remplace chaque ancienne IP LXC par la nouvelle IP interne dans `config.php`
- Remplace le mot de passe `DB_PASS` dans `config.php` avec `mariadb_appuser_password`
- Écrit `/etc/hosts` avec les correspondances nom → IP interne pour tous les services
- Redémarre Apache

**Backup :**
- Crée `/backups/`
- Écrit `/etc/backup.conf` avec les credentials appuser (protégé `600`)
- Dépose `backup.sh` avec la nouvelle IP MariaDB interne
- Configure le cron à 2h

**FTP :**
- Crée les comptes système FTP
- Extrait les archives des homes FTP
- Écrit `/etc/vsftpd.conf` avec la configuration scannée (chroot, ports passifs)
- Redémarre vsftpd

**NFS :**
- Extrait l'archive NFS dans `/srv/nfs/`
- Réécrit `/etc/exports` avec le nouveau sous-réseau interne
- Réexporte (`exportfs -ra`), redémarre nfs-kernel-server

**Sortie :** services opérationnels avec les données de production.

---

### Phase 8c — Validation interne (Ansible validate.yml)

**Fonction :** `phase_ansible(state, Phase.VALIDATE, ...)`
**Fichiers impliqués :** `ansible/validate.yml`

**Ce qu'elle fait :**
Vérifie sur chaque instance que le service fonctionne correctement après restauration.

| Instance | Vérifications |
|----------|--------------|
| **mariadb** | service actif, bases présentes (`SHOW DATABASES`), user appuser présent |
| **apache** | service actif, HTTP 200 sur localhost, absence d'anciennes IPs LXC dans config.php |
| **backup** | backup.sh présent, nouvelle IP MariaDB dans backup.sh, cron actif |
| **ftp** | service actif, répertoires uploads des users présents |
| **nfs** | exports non vides (`exportfs -v`), fichiers présents dans `/srv/nfs/shared` |

Chaque play écrit un rapport JSON dans `/tmp/migration/validation_{service}.json` sur l'instance.

**Sortie :** toutes les assertions passent → migration validée.

---

### Phase 9 — Rapport final

**Fonction :** `generer_rapport(state, instances)`
**Fichiers produits :** `migration_report.json`

**Ce qu'elle fait :**
Génère le rapport de migration en JSON avec :
- Date et heure de migration
- Durée totale en secondes
- Pour chaque instance : ancienne IP LXC, IP interne, floating IP, statut de validation

Affiche un tableau récapitulatif dans le terminal.

```json
{
  "migration_date": "2026-05-10T01:20:31",
  "status": "success",
  "duree_secondes": 667.0,
  "instances": {
    "mariadb": {
      "old_lxc_ip": "10.0.3.10",
      "internal_ip": "10.10.10.40",
      "floating_ip": "10.0.0.132",
      "validation": "passed"
    }
  }
}
```

**Rollback automatique :** si une exception est levée pendant n'importe quelle phase, `rollback()` est appelé. Il exécute `terraform destroy -auto-approve` pour supprimer toutes les ressources OpenStack créées, puis marque la migration en `ECHEC` dans `migration_state.json`.

---

## 5. Fonctionnement du scanner LXC

Le scanner (`src/scanner.py`) est le composant le plus important du projet. C'est lui qui rend la migration 100% dynamique : il interroge chaque container LXC en cours d'exécution et en extrait tous les paramètres nécessaires à la migration.

### Principe général

Le scanner utilise `lxc-attach -n {nom} -- {commande}` pour exécuter des commandes à l'intérieur des containers depuis l'hôte, sans avoir besoin d'une connexion SSH ni d'un agent. Chaque appel retourne la sortie standard sous forme de chaîne Python.

```python
def executer(commande: list) -> str:
    resultat = subprocess.run(commande, shell=False, capture_output=True, text=True)
    return resultat.stdout.strip()
```

### Détection des containers actifs

```python
sortie = executer(["sudo", "lxc-ls", "--running"])
noms = sortie.split()  # ["apache", "backup", "ftp", "mariadb", "nfs"]
```

Les containers arrêtés sont listés séparément et signalés par un avertissement — ils ne sont pas migrés.

### Détection des services actifs

Pour chaque container, `systemctl list-units --type=service --state=running` liste les services en cours d'exécution. Une liste d'exclusion filtre les services système non pertinents (dbus, rsyslog, systemd-*, ssh...).

```python
services_bruts = executer([
    "sudo", "lxc-attach", "-n", nom, "--",
    "systemctl", "list-units", "--type=service",
    "--state=running", "--no-legend", "--no-pager"
])
```

**Cas particulier NFS :** `nfs-server.service` n'apparaît pas toujours dans `list-units`. Une détection complémentaire via `systemctl is-active nfs-server` est effectuée.

### Détection des bases de données MariaDB

Si `mariadb` est dans les services :

```python
bases_brutes = executer([
    "sudo", "lxc-attach", "-n", nom, "--",
    "mysql", "-u", "root", "-e", "SHOW DATABASES;"
])
```

Les bases système (`information_schema`, `mysql`, `performance_schema`, `sys`) sont filtrées. Les bases applicatives restantes sont validées par le modèle Pydantic `Database` (regex `^[a-zA-Z0-9_]+$`).

Les users MariaDB sont collectés séparément :

```python
executer([..., "mysql", "-u", "root", "-sN",
          "-e", "SELECT user, host FROM mysql.user;"])
```

Les comptes système (`root`, `mariadb.sys`, `mysql`) sont exclus. Les users restants (ex. `appuser@10.0.3.20`) servent à identifier l'ancienne IP apache dans `host_vars`.

### Détection des users FTP

Lecture de `/etc/passwd` via `lxc-attach` :

```python
executer(["sudo", "lxc-attach", "-n", nom, "--", "cat", "/etc/passwd"])
```

Filtrage : les users avec `/usr/sbin/nologin` ou `/bin/false` comme shell sont exclus. Une liste d'exclusion hard-codée élimine les comptes système Ubuntu (`root`, `www-data`, `sshd`, `ubuntu`, etc.).

Les users restants sont des comptes réels avec un shell valide — ce sont les users FTP. Leurs `home` sont extraits du 6ème champ de passwd.

### Détection des exports NFS

```python
executer(["sudo", "lxc-attach", "-n", nom, "--", "cat", "/etc/exports"])
```

Chaque ligne non commentée est parsée : chemin, sous-réseau, options extraits par split sur `(` et `)`. Produit des objets `NFSExport(path, subnet, options)`.

### Détection du script de backup

```python
executer(["sudo", "lxc-attach", "-n", nom, "--",
          "cat", "/usr/local/bin/backup.sh"])
```

Si le fichier existe, les variables `HOST`, `DB` et `DEST` sont extraites ligne par ligne. Produit un objet `BackupConfig`. Si le fichier est absent, retourne `None`. C'est ce `None`/non-`None` qui distingue un container backup d'un container ordinaire — et non la présence du service `cron` (qui tourne par défaut sur Ubuntu).

### Détection de la configuration Apache

```python
executer(["sudo", "lxc-attach", "-n", nom, "--",
          "cat", "/var/www/html/config.php"])
```

Si le fichier existe, les IPs LXC (`10.0.3.x`) présentes sont extraites par regex et stockées dans `ApacheConfig.ips_trouvees`. Sert à documenter ce qui sera remplacé lors de la restauration.

### Métriques RAM et disque

- **RAM :** `free -m` dans le container, parsing de la ligne `Mem:`, champ `used`
- **Disque :** `du -sb /var/lib/lxc/{nom}/rootfs/` depuis l'hôte, conversion en Go

Ces métriques sont disponibles dans l'objet `Container` mais ne sont pas encore utilisées pour le dimensionnement des flavors.

### Modèles de données Pydantic

Chaque container est représenté par un objet `Container` validé :

```python
class Container(BaseModel):
    name: str          # validé: ^[a-zA-Z0-9_-]+$
    ip: str            # validé: format IPv4 strict
    state: str         # "RUNNING"
    services: List[str]
    packages: List[str]
    databases: List[Database]
    db_users: List[DBUser]
    ftp_users: List[FTPUser]
    nfs_exports: List[NFSExport]
    vsftpd_config: Optional[VSFTPDConfig]
    backup_config: Optional[BackupConfig]
    apache_config: Optional[ApacheConfig]
    ram_used_mb: int
    disk_used_gb: int
```

---

## 6. Génération dynamique des playbooks Ansible

L'un des aspects les plus importants du projet est que les trois playbooks Ansible (`provision.yml`, `restore.yml`, `validate.yml`) **ne sont jamais écrits à la main**. Ils sont générés à chaque migration par l'orchestrateur Python depuis le résultat du scan.

### Principe

Chaque fonction génératrice construit une liste de lignes YAML sous forme de strings Python, puis les joint avec `"\n"` et écrit le fichier.

```python
lines = ["---", "# Généré automatiquement par l'orchestrateur"]
lines += [
    "",
    f"- name: Restauration {c.name}",
    f"  hosts: {c.name}",
    ...
]
(ANSIBLE_DIR / "restore.yml").write_text("\n".join(lines) + "\n")
```

### Gestion des noms avec tirets

Ansible utilise la notation pointée pour les variables (`new_ips.mariadb`). Si un container s'appelle `mariadb-replica` (avec un tiret), la notation pointée est invalide en Jinja2. Le helper `_new_ip(name)` détecte automatiquement ce cas :

```python
def _new_ip(name: str) -> str:
    if '-' in name:
        return f"{{{{ new_ips['{name}'] }}}}"   # {{ new_ips['mariadb-replica'] }}
    return f"{{{{ new_ips.{name} }}}}"            # {{ new_ips.mariadb }}
```

### Exemple concret — container mariadb

**Entrée (résultat du scan) :**
```
Container(
    name="mariadb", ip="10.0.3.10", services=["mariadb"],
    databases=[Database(name="app_db"), Database(name="sysmonitor")],
    db_users=[DBUser(user="appuser", host="10.0.3.20")]
)
```

**`generer_provision_yml` produit pour mariadb :**
```yaml
- name: Provisionnement mariadb
  hosts: mariadb
  become: true
  gather_facts: no
  tasks:
    - name: Installation paquets mariadb
      apt:
        name:
          - mariadb-server
          - mariadb-client
          - python3-pymysql
        state: present
    - name: Demarrage mariadb
      service:
        name: mariadb
        state: started
        enabled: true
```

**`generer_restore_yml` produit pour mariadb :**
```yaml
- name: Restauration mariadb
  hosts: mariadb
  become: true
  tasks:
    - name: Restauration des bases de données
      community.mysql.mysql_db:
        name: "{{ item }}"
        state: import
        target: "{{ staging_dir }}/mariadb_{{ item }}.sql"
        login_unix_socket: /var/run/mysqld/mysqld.sock
      loop: "{{ databases }}"    # ["app_db", "sysmonitor"] depuis host_vars

    - name: Suppression ancien user appuser@ancienne_ip
      community.mysql.mysql_user:
        name: appuser
        host: "{{ old_apache_ip }}"    # "10.0.3.20" depuis host_vars
        state: absent

    - name: Creation user appuser pour apache
      community.mysql.mysql_user:
        name: appuser
        host: "{{ new_ips.apache }}"
        password: "{{ mariadb_appuser_password }}"
        priv: "app_db.*:ALL/sysmonitor.*:ALL"
        state: present

    - name: Restriction bind-address MariaDB
      lineinfile:
        path: /etc/mysql/mariadb.conf.d/50-server.cnf
        regexp: '^bind-address'
        line: 'bind-address = {{ internal_ip }}'   # "10.10.10.40" depuis host_vars
```

**`generer_validate_yml` produit pour mariadb :**
```yaml
- name: Validation mariadb
  hosts: mariadb
  tasks:
    - name: MariaDB est actif
      assert:
        that: ansible_facts.services['mariadb.service'].state == 'running'
    - name: Verification bases de données
      community.mysql.mysql_query:
        query: "SHOW DATABASES LIKE '{{ item }}'"
      loop: "{{ databases }}"
    - name: Verification user appuser
      community.mysql.mysql_query:
        query: "SELECT user, host FROM mysql.user WHERE user='appuser'"
```

**Les variables Jinja2 (`{{ databases }}`, `{{ old_apache_ip }}`, `{{ internal_ip }}`) sont résolues à l'exécution par Ansible depuis les `host_vars` générés en phase 5.**

### Détection des types de containers

La génération de restore.yml classe les containers en quatre catégories :

```python
apache_containers  = [c for c in containers if "apache2" in c.services]
mariadb_containers = [c for c in containers if "mariadb" in c.services]
backup_containers  = [c for c in containers if c.backup_config is not None
                     and "mariadb" not in c.services]
# ftp et nfs : détectés par "vsftpd" et "nfs-server" dans c.services
```

La détection des containers backup par `c.backup_config is not None` (présence de `backup.sh`) plutôt que par `"cron" in c.services` est délibérée : `cron` tourne par défaut dans tous les containers Ubuntu — cela causerait des faux positifs.

---

## 7. Provisioning Terraform dynamique

### Génération de `terraform.tfvars`

La fonction `generer_tfvars(containers, credentials)` construit le fichier de variables Terraform depuis le scan :

```python
# Tri alphabétique par ordre de scan (lxc-ls --running)
for i, c in enumerate(containers):
    ip_mapping[c.name] = f"{prefix}.{start + i * 10}"
    # apache → 10.10.10.10, backup → 10.10.10.20, ftp → 10.10.10.30...
```

### Attribution des IPs internes

Les IPs internes sont attribuées **séquentiellement** selon l'ordre alphabétique des containers (ordre retourné par `lxc-ls --running`). Le point de départ est `internal_ip_pool` dans `config.yml` (`10.10.10.10`). Chaque container suivant reçoit +10 sur le dernier octet.

**Exemple avec les 5 containers :**

| Ordre alpha | Container | IP interne attribuée |
|-------------|-----------|----------------------|
| 1 | apache | 10.10.10.10 |
| 2 | backup | 10.10.10.20 |
| 3 | ftp | 10.10.10.30 |
| 4 | mariadb | 10.10.10.40 |
| 5 | nfs | 10.10.10.50 |

**Ajout d'un nouveau container :** si un container `wordpress` est ajouté dans LXC, il sera détecté par le scanner, recevra l'IP `10.10.10.60` (6ème en ordre alphabétique après `nfs`), et un play Ansible sera généré pour lui automatiquement.

### Choix des flavors

La table des flavors est dans `config.yml` :

```yaml
flavors:
  default: "m1.small"
  mariadb: "m1.mariadb"
  apache:  "m1.apache"
  backup:  "m1.backup"
  ftp:     "m1.ftp"
  nfs:     "m1.nfs"
```

L'orchestrateur cherche le flavor par **nom de container** :

```python
flavor = cfg_flavors.get(nom, cfg_flavors.get("default", "m1.small"))
```

Un container dont le nom ne correspond à aucune clé reçoit le flavor `default`. Pour ajouter un flavor personnalisé pour `wordpress`, il suffit d'ajouter `wordpress: "m1.wordpress"` dans `config.yml`.

### Structure du `terraform.tfvars` généré

```hcl
os_auth_url     = "http://10.0.0.10:5000/v3"
os_username     = "migration-user"
os_password     = "..."
os_project_name = "migration"
os_region       = "RegionOne"

provider_network       = "provider"
migration_network_cidr = "10.10.10.0/24"
migration_gateway      = "10.10.10.1"

image_name     = "ubuntu-22.04"
ssh_public_key = "ssh-rsa AAAA..."

instances = {
  apache  = { internal_ip = "10.10.10.10", flavor = "m1.apache"  }
  backup  = { internal_ip = "10.10.10.20", flavor = "m1.backup"  }
  ftp     = { internal_ip = "10.10.10.30", flavor = "m1.ftp"     }
  mariadb = { internal_ip = "10.10.10.40", flavor = "m1.mariadb" }
  nfs     = { internal_ip = "10.10.10.50", flavor = "m1.nfs"     }
}
```

Ce fichier est passé à `terraform apply` puis **supprimé immédiatement** — il ne persiste jamais sur le disque au-delà du provisioning.

### Outputs Terraform

`outputs.tf` expose pour chaque instance son IP interne, sa floating IP et son ID Nova :

```hcl
output "instances" {
  value = {
    for nom, instance in openstack_compute_instance_v2.instances : nom => {
      internal_ip = var.instances[nom].internal_ip
      floating_ip = openstack_networking_floatingip_v2.floating_ips[nom].address
      instance_id = instance.id
    }
  }
}
```

Python lit cet output via `terraform output -json` et stocke les données dans `migration_state.json`.

---

## 8. Sécurité

### Mesures implémentées

**Credentials jamais écrits de manière persistante**
Les mots de passe OpenStack, MariaDB root et MariaDB appuser sont collectés interactivement via `getpass.getpass()` (sans echo) et stockés uniquement en mémoire Python. Le fichier `terraform.tfvars` qui contient le mot de passe OpenStack est supprimé immédiatement après `terraform apply`.

**Fichier de credentials MariaDB temporaire dans le container**
Pour `mysqldump`, les credentials root sont écrits dans `/tmp/.my.cnf` à l'intérieur du container (`chmod 600`), utilisés pour le dump, puis supprimés dans un bloc `finally` qui garantit la suppression même en cas d'erreur.

**Répertoire de staging local protégé**
Le répertoire temporaire créé par `tempfile.mkdtemp()` pour les archives est immédiatement passé en `chmod 700` — accessible uniquement à l'utilisateur courant.

**Suppression des archives locales après transfert**
`shutil.rmtree(tmp_dir)` supprime le répertoire temporaire local avec toutes les archives après le transfert SFTP. Les données sensibles (dumps SQL) ne restent pas sur le disque de vm-source.

**Séparation des droits appuser MariaDB**
L'utilisateur `appuser` est créé avec deux niveaux de droits distincts :
- `appuser@{ip_apache}` → `ALL` sur les bases applicatives (lecture/écriture pour l'application)
- `appuser@{ip_backup}` → `SELECT` uniquement sur `app_db` (lecture seule pour les sauvegardes)

**`bind-address` MariaDB limité à l'IP interne**
MariaDB n'écoute que sur `10.10.10.40` (IP interne), pas sur `0.0.0.0`. Combiné avec le security group qui limite le port 3306 au sous-réseau `10.10.10.0/24`, MariaDB n'est pas accessible depuis l'extérieur.

**Security groups restrictifs par service**
Chaque instance a son propre security group avec uniquement les ports nécessaires. MariaDB n'ouvre pas le port 80. Apache n'ouvre pas le port 3306. NFS n'ouvre ses ports que depuis le réseau interne.

**`os_password` marqué `sensitive` dans Terraform**
`variables.tf` déclare `os_password` avec `sensitive = true`, ce qui empêche Terraform d'afficher sa valeur dans les logs et outputs.

**Fichier de configuration backup protégé**
`/etc/backup.conf` (qui contient le mot de passe appuser pour mysqldump) est créé avec `owner: root`, `group: root`, `mode: '0600'` — inaccessible aux autres utilisateurs.

**Reprise sur erreur sans ré-exécuter les phases dangereuses**
`migration_state.json` permet de reprendre la migration depuis la phase où elle s'est arrêtée. Si le backup a déjà été fait, il ne sera pas refait — évitant de dupliquer des dumps SQL sur le réseau.

---

## 9. Comment reproduire la migration de zéro

### Prérequis système (sur vm-source)

| Outil | Version minimale | Installation |
|-------|-----------------|--------------|
| Python | 3.10+ | `sudo apt install python3 python3-pip` |
| Terraform | 1.5+ | Voir [developer.hashicorp.com/terraform/install](https://developer.hashicorp.com/terraform/install) |
| Ansible | 2.15+ | `pip install ansible` ou `sudo apt install ansible` |
| LXC | tout | `sudo apt install lxc` (déjà présent sur vm-source) |

Vérification :
```bash
python3 --version     # Python 3.10+
terraform version     # Terraform v1.5+
ansible --version     # ansible [core 2.15+]
```

### Étape 1 — Cloner le projet

```bash
git clone https://github.com/theaminem/PFE-migration.git
cd PFE-migration
```

### Étape 2 — Installer les dépendances Python

```bash
pip install -r requirements.txt
```

Contenu de `requirements.txt` :
```
paramiko>=3.0.0
pydantic>=2.0.0
pyyaml>=6.0
```

### Étape 3 — Installer la collection Ansible

```bash
ansible-galaxy collection install -r ansible/requirements.yml
```

Installe `community.mysql` (version ≥ 3.0.0), nécessaire pour les modules `mysql_db`, `mysql_user`, `mysql_query`.

### Étape 4 — Générer la clé SSH

```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/migration_key -N ""
```

Cette clé sera injectée dans les instances OpenStack par Terraform et utilisée par Ansible et Paramiko pour y accéder.

### Étape 5 — Configurer OpenStack

Avant de lancer la migration, l'infrastructure OpenStack doit être préparée :

1. **Créer un projet** dans Keystone (ex. `migration`)
2. **Créer un utilisateur** dans ce projet (ex. `migration-user`) avec le rôle `member`
3. **Uploader l'image** Ubuntu 22.04 dans Glance :
   ```bash
   # Télécharger l'image cloud Ubuntu 22.04
   wget https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img
   
   # L'uploader dans Glance (depuis vm-cible ou avec openstack CLI configuré)
   openstack image create "ubuntu-22.04" \
     --file jammy-server-cloudimg-amd64.img \
     --disk-format qcow2 \
     --container-format bare \
     --public
   ```
4. **Créer les flavors** utilisés dans `config.yml` :
   ```bash
   openstack flavor create m1.small   --ram 512  --disk 10 --vcpus 1
   openstack flavor create m1.apache  --ram 1024 --disk 10 --vcpus 1
   openstack flavor create m1.mariadb --ram 1024 --disk 20 --vcpus 1
   openstack flavor create m1.backup  --ram 512  --disk 20 --vcpus 1
   openstack flavor create m1.ftp     --ram 512  --disk 10 --vcpus 1
   openstack flavor create m1.nfs     --ram 512  --disk 20 --vcpus 1
   ```
5. **Vérifier que le réseau provider** existe (ex. `provider`) et est accessible depuis vm-source

### Étape 6 — Configurer `config.yml`

```bash
cp config.yml.example config.yml
nano config.yml
```

Adapter les valeurs suivantes :

```yaml
openstack:
  auth_url:        "http://{IP_VM_CIBLE}:5000/v3"   # URL Keystone
  region:          "RegionOne"
  default_user:    "migration-user"                  # Utilisateur OpenStack
  default_project: "migration"                       # Projet OpenStack

network:
  provider:          "provider"                      # Nom du réseau provider OpenStack
  internal_cidr:     "10.10.10.0/24"                # Réseau interne pour les instances
  internal_gateway:  "10.10.10.1"                   # Gateway du réseau interne
  api_ip:            "{IP_VM_CIBLE}"                 # IP de l'API OpenStack (pour le ping prérequis)

image:
  name: "ubuntu-22.04"                              # Nom exact de l'image dans Glance

ssh:
  key_path: "~/.ssh/migration_key"                  # Chemin de la clé SSH générée à l'étape 4
  user:     "ubuntu"                                # User par défaut de l'image Ubuntu cloud

staging_dir: "/tmp/migration"                       # Répertoire de staging sur les instances

flavors:
  default: "m1.small"
  mariadb: "m1.mariadb"
  apache:  "m1.apache"
  backup:  "m1.backup"
  ftp:     "m1.ftp"
  nfs:     "m1.nfs"

internal_ip_pool: "10.10.10.10"                    # Première IP du pool (ordre alphabétique des containers)
```

### Étape 7 — Vérifier les containers LXC

S'assurer que les containers LXC à migrer sont en cours d'exécution :

```bash
sudo lxc-ls --running
# Doit afficher : apache backup ftp mariadb nfs
```

### Étape 8 — Lancer la migration

```bash
python3 src/migrate.py
```

Le script guide interactivement :
1. Affiche les prérequis vérifiés
2. Demande les credentials (OpenStack password, MariaDB root password, MariaDB appuser password)
3. Lance les 9 phases automatiquement

**Durée typique :** 10 à 15 minutes selon la taille des données et la performance réseau.

### Reprise en cas d'interruption

Si la migration est interrompue (erreur réseau, Ctrl+C, etc.), relancer simplement :

```bash
python3 src/migrate.py
```

Le script détecte `migration_state.json`, affiche l'état actuel, et propose de reprendre depuis la phase interrompue. Les phases déjà terminées ne sont pas ré-exécutées.

### Vérifier le résultat

```bash
# Rapport de migration
cat migration_report.json

# État final
cat migration_state.json

# Accéder à l'application web
curl http://{FLOATING_IP_APACHE}/
```

---

## 10. Structure du projet

```
PFE-migration/
│
├── config.yml              # Configuration active (ignorée par git — contient des secrets)
├── config.yml.example      # Modèle de configuration à copier et adapter
├── requirements.txt        # Dépendances Python : paramiko, pydantic, pyyaml
├── migration_state.json    # État courant de la migration (généré, ignoré par git)
├── migration_report.json   # Rapport final de migration (généré, ignoré par git)
├── README.md               # Présentation rapide du projet
├── MANUEL.md               # Ce document — documentation technique complète
├── LICENSE                 # Licence du projet
├── .gitignore              # Exclut : config.yml, migration_state.json, tfstate, clés SSH
│
├── src/
│   ├── migrate.py          # Point d'entrée. Importe et appelle orchestrator.main()
│   ├── orchestrator.py     # Cerveau du projet. Orchestre les 9 phases, génère tous les fichiers
│   ├── scanner.py          # Scan des containers LXC via lxc-attach. Modèles Pydantic
│   └── state.py            # Machine à états. Persiste la progression dans migration_state.json
│
├── terraform/
│   ├── main.tf             # Ressources OpenStack : réseau, router, SG, instances, floating IPs
│   ├── variables.tf        # Déclaration des variables Terraform avec types et descriptions
│   ├── outputs.tf          # Output "instances" : internal_ip + floating_ip par instance
│   └── terraform.tfvars    # Généré dynamiquement par orchestrator.py, supprimé après apply
│
└── ansible/
    ├── ansible.cfg         # Configuration Ansible : forks=5, pipelining, timeouts
    ├── requirements.yml    # Collection community.mysql >= 3.0.0
    ├── inventory.ini       # Généré par orchestrator.py. Floating IPs par groupe
    ├── provision.yml       # Généré par generer_provision_yml(). Installation des paquets
    ├── restore.yml         # Généré par generer_restore_yml(). Restauration des données
    ├── validate.yml        # Généré par generer_validate_yml(). Vérification post-migration
    ├── group_vars/
    │   └── all.yml         # Généré : user SSH, clé, staging_dir, new_ips par container
    └── host_vars/
        └── {floating_ip}.yml   # Généré par instance : old_lxc_ip, internal_ip, databases, etc.
```

### Détail des fichiers clés

**`src/orchestrator.py`** (~1300 lignes)
Le fichier central. Contient :
- `main()` : boucle principale des 9 phases avec gestion de la reprise
- `verifier_prerequis()`, `collecter_credentials()` : phases 1-2
- `generer_tfvars()`, `phase_provisioning()` : phase 4
- `generer_group_vars()`, `generer_provision_yml()`, `generer_restore_yml()`, `generer_validate_yml()`, `generer_inventaire()` : phase 5
- `phase_backup()`, `phase_transfert()` : phases 6-7
- `lancer_ansible()`, `phase_ansible()` : phases 8a-8c
- `rollback()`, `generer_rapport()` : rollback et phase 9
- `_restore_mariadb_play()`, `_restore_apache_play()`, `_restore_nfs_play()`, `_restore_backup_play()`, `_restore_ftp_play()` : helpers de génération restore.yml
- `_validate_*_play()` : helpers de génération validate.yml
- `_new_ip(name)` : helper Jinja2 pour les noms avec tirets

**`src/scanner.py`** (~430 lignes)
Modèles Pydantic + fonctions de détection :
- `scanner_containers()` : fonction principale, retourne `List[Container]`
- `lire_users_mariadb()`, `lire_users_ftp()`, `lire_exports_nfs()`, `lire_vsftpd_config()`, `lire_backup_config()`, `lire_apache_config()` : détection par service
- `lire_ram()`, `lire_disk()` : métriques ressources

**`src/state.py`**
- Classe `State` avec `sauvegarder()`, `charger()`, `phase_terminee()`, `marquer_echec()`, `marquer_termine()`
- Enum `Phase` : SCAN(1) → PROVISIONING(2) → BACKUP(3) → PROVISION(4) → TRANSFER(5) → RESTORE(6) → VALIDATE(7) → TERMINE(9) → ECHEC(-1)

**`terraform/main.tf`**
Infrastructure complète déclarée en HCL. Ne jamais modifier manuellement — il est conçu pour recevoir les variables dynamiques de `terraform.tfvars`.

**`ansible/ansible.cfg`**
Configuration optimisée :
- `host_key_checking = False` : pas de vérification d'hôte (instances fraîches)
- `pipelining = True` : optimisation SSH (moins de connexions)
- `forks = 5` : 5 hôtes en parallèle
- `ControlPersist=600s` : réutilisation des connexions SSH

---

## 11. Erreurs de conception corrigées

Cette section documente les problèmes identifiés et corrigés pendant le développement du projet. Elle est utile pour comprendre les choix techniques et éviter de réintroduire les mêmes erreurs.

---

### ERR-01 — Migration statique : services codés en dur dans `config.yml`

**Problème initial :** la version originale de `config.yml` contenait un bloc `services:` avec les IPs LXC, les IPs internes, les flavors et la liste des fichiers à transférer pour chaque service. Ajouter un container LXC nécessitait de modifier `config.yml`, `provision.yml`, `restore.yml` et `validate.yml` manuellement.

```yaml
# Ancienne version — non dynamique
services:
  mariadb:
    lxc_ip:      "10.0.3.10"
    internal_ip: "10.10.10.10"
    flavor:      "m1.mariadb"
    transfer_files: ["app_db.sql", "sysmonitor.sql"]
```

**Correction :** suppression du bloc `services:`, remplacement par `flavors:` et `internal_ip_pool:`. Les IPs sont déduites du scan par attribution séquentielle. Les fichiers à transférer sont déduits du type de service détecté. Ajouter un container LXC ne requiert aucune modification.

---

### ERR-02 — Regex `\\b` ne fonctionnait pas dans les playbooks YAML

**Problème :** le remplacement des IPs dans `config.php` ne fonctionnait pas. Dans le code Python, le regexp était généré avec `'\\\\b{ip}\\\\b'`, ce qui produisait `\\b10\.0\.3\.10\\b` dans le YAML. Dans une chaîne YAML single-quoted, `\\b` est littéral (deux caractères : backslash + b), pas un word boundary.

**Correction :** utiliser `'\\b{ip}\\b'` dans le code Python, qui produit `\b10\.0\.3\.10\b` dans le YAML single-quoted, correctement interprété comme word boundary par le moteur regex Python d'Ansible.

```python
# Avant (incorrect)
f"        regexp: '\\\\b{escaped_ip}\\\\b'",

# Après (correct)
f"        regexp: '\\b{escaped_ip}\\b'",
```

---

### ERR-03 — Conteneurs backup mal identifiés (`"cron" in c.services`)

**Problème :** le container backup était identifié par la présence du service `cron` dans ses services. Or Ubuntu fait tourner `cron` par défaut dans **tous** ses containers. Résultat : apache, ftp et nfs étaient également considérés comme des containers backup, générant des plays incorrects (backup.sh déployé sur apache, grant SELECT écrasant le grant ALL, etc.)

**Correction :** utiliser `c.backup_config is not None` comme critère. Le scanner lit `/usr/local/bin/backup.sh` — seul le vrai container backup a ce fichier. Ce test est précis et ne génère pas de faux positifs.

```python
# Avant (incorrect)
backup_containers = [c for c in containers if "cron" in c.services]

# Après (correct)
backup_containers = [c for c in containers if c.backup_config is not None
                    and "mariadb" not in c.services]
```

---

### ERR-04 — `DB_PASS` dans `config.php` non mis à jour après migration

**Problème :** `config.php` contient `define('DB_PASS', 'password')` — le mot de passe original du container LXC. Lors de la restauration, les IPs étaient remplacées, mais pas le mot de passe. Or l'orchestrateur crée l'utilisateur `appuser` sur l'instance MariaDB OpenStack avec `mariadb_app_password` (saisi au prompt). Si ce mot de passe diffère de l'original, la connexion PHP→MariaDB échoue silencieusement.

**Correction :** ajout d'une tâche `lineinfile` dans `_restore_apache_play` qui remplace la ligne entière `define('DB_PASS', ...)` par la valeur de la variable Ansible `{{ mariadb_appuser_password }}`.

```python
lines += [
    "",
    "    - name: Remplacement DB_PASS dans config.php",
    "      lineinfile:",
    "        path: /var/www/html/config.php",
    "        regexp: \"define\\\\('DB_PASS'\"",
    "        line: \"define('DB_PASS', '{{ mariadb_appuser_password }}');\"",
]
```

---

### ERR-05 — Séparateur de grants MariaDB incorrect (virgule au lieu de slash)

**Problème :** le module `community.mysql.mysql_user` attend le caractère `/` comme séparateur entre plusieurs grants dans le paramètre `priv` au format string. Le code utilisait une virgule, ce qui créait un grant sur une base nommée littéralement `app_db.*:ALL,sysmonitor` — inexistante.

```python
# Avant (incorrect)
'        priv: "app_db.*:ALL,sysmonitor.*:ALL"',

# Après (correct)
'        priv: "app_db.*:ALL/sysmonitor.*:ALL"',
```

---

### ERR-06 — Archives sans préfixe de container (collision de noms)

**Problème initial :** les archives étaient nommées sans préfixe (`html.tar.gz`, `app_db.sql`). Si deux containers avaient un service apache2, leurs archives `html.tar.gz` se seraient écrasées mutuellement dans le répertoire temporaire.

**Correction :** toutes les archives sont préfixées par le nom du container :
- `apache_html.tar.gz`, `apache_apache2.tar.gz`
- `mariadb_app_db.sql`, `mariadb_sysmonitor.sql`
- `ftp_ftp_ftpuser.tar.gz`
- `nfs_nfs_shared.tar.gz`

---

### ERR-07 — `phase_transfert` avec liste de fichiers statique

**Problème initial :** la liste des fichiers à transférer vers chaque instance était lue depuis `config.yml` (champ `transfer_files:`). Si le scanner détectait une nouvelle base de données, elle était dumpée mais jamais transférée.

**Correction :** la liste est construite dynamiquement depuis l'objet `Container` :

```python
if "mariadb" in c.services:
    fichiers += [f"{c.name}_{db.name}.sql" for db in c.databases]
if "apache2" in c.services:
    fichiers += [f"{c.name}_html.tar.gz", f"{c.name}_apache2.tar.gz"]
if "vsftpd" in c.services:
    fichiers += [f"{c.name}_ftp_{u.username}.tar.gz" for u in c.ftp_users]
```

---

*Document généré le 2026-05-10. Pour toute question, contacter les auteurs via le repository GitHub.*
