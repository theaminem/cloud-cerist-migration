# PFE Migration LXC → OpenStack

Automatisation complète de migration de services réseau depuis des containers LXC vers un cloud privé OpenStack Caracal.

## Contexte

Projet de fin d'études Master — Migration d'infrastructure.
Encadrant : KHIAT Abd Elhamid

## Infrastructure source

| Container | IP LXC | Service |
|---|---|---|
| mariadb | 10.0.3.10 | MariaDB 10.11 |
| apache | 10.0.3.20 | Apache2 + PHP |
| backup | 10.0.3.30 | mysqldump + cron |
| ftp | 10.0.3.40 | vsftpd |
| nfs | 10.0.3.50 | NFS kernel server |

## Infrastructure cible

OpenStack Caracal (2024.1) — installation manuelle sur Ubuntu Server 24.04
- Keystone, Glance, Nova, Neutron, Cinder, Placement

## Outils

- Python 3 : orchestration des phases de migration
- Terraform : provisioning des instances OpenStack
- Ansible : configuration et restauration des services
- HAProxy : bascule du trafic après migration
- Bind9 : résolution DNS locale (SysMonitor.migration.local)

## Lancement

```bash
python3 migrate.py
```

## Phases de migration

1. Vérification des prérequis
2. Scan des containers LXC
3. Provisioning OpenStack via Terraform
4. Backup des données depuis les containers
5. Provisionnement logiciel via Ansible
6. Transfert des archives
7. Restauration des services
8. Validation interne
9. Tests externes depuis vm-source
10. Rapport final

## Structure du projet

```
PFE-migration/
├── migrate.py
├── python/
│   ├── scanner.py
│   ├── state.py
│   └── test_runner.py
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tfvars
└── ansible/
    ├── group_vars/
    ├── host_vars/
    ├── backup.yml
    ├── provision.yml
    ├── transfer.yml
    ├── restore.yml
    └── validate.yml
```
