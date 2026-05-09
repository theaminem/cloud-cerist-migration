# PFE Migration LXC → OpenStack

Automatisation complète de migration de services réseau depuis des containers LXC vers un cloud privé OpenStack Caracal.

## Contexte

Projet de fin d'études Master — Migration d'infrastructure.
Encadrant : KHIAT Abd Elhamid
Réalisé par : Amine et Safa Imad Mouhamed

## Prérequis

- Python 3.10+
- Terraform 1.5+
- Ansible 2.15+
- Accès SSH aux instances OpenStack

## Installation

```bash
git clone https://github.com/theaminem/PFE-migration.git
cd PFE-migration
pip install -r requirements.txt
ansible-galaxy collection install -r ansible/requirements.yml
cp config.yml.example config.yml
nano config.yml
```

## Configuration

Toute la configuration se trouve dans `config.yml`. Adapte les valeurs à ton infrastructure :

- `openstack` : URL, région, utilisateur, projet
- `network` : CIDR interne, gateway, IP de vm-cible
- `image` : nom de l'image Glance
- `ssh` : chemin de la clé et utilisateur
- `services` : IPs LXC, IPs internes OpenStack, flavors, fichiers à transférer

## Lancement

```bash
python3 src/migrate.py
```

## Phases de migration

| Phase | Description |
|-------|-------------|
| 1 | Vérification des prérequis |
| 2 | Collecte des credentials |
| 3 | Scan des containers LXC |
| 4 | Provisioning Terraform |
| 5 | Génération inventaire Ansible |
| 6 | Backup des données LXC |
| 7 | Transfert des archives |
| 8a | Provisionnement logiciel (Ansible) |
| 8b | Restauration des services (Ansible) |
| 8c | Validation interne (Ansible) |
| 9 | Rapport final |

## Structure du projet
PFE-migration/
├── config.yml.example      # Modèle de configuration
├── requirements.txt        # Dépendances Python
├── src/
│   ├── migrate.py          # Point d'entrée
│   ├── orchestrator.py     # Orchestration des phases
│   ├── scanner.py          # Scan des containers LXC
│   └── state.py            # Gestion de l'état
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
└── ansible/
├── requirements.yml    # Collections Ansible
├── ansible.cfg
├── group_vars/
├── provision.yml
├── restore.yml
└── validate.yml

## Infrastructure source

| Container | IP LXC | Service |
|-----------|--------|---------|
| mariadb | 10.0.3.10 | MariaDB 10.11 |
| apache | 10.0.3.20 | Apache2 + PHP |
| backup | 10.0.3.30 | mysqldump + cron |
| ftp | 10.0.3.40 | vsftpd |
| nfs | 10.0.3.50 | NFS kernel server |

## Infrastructure cible

OpenStack Caracal (2024.1) — installation manuelle sur Ubuntu Server 24.04

Composants : Keystone, Glance, Nova, Neutron, Placement, Horizon
