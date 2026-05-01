import subprocess
import json
import os
import sys
import getpass
import tempfile
import shutil
import paramiko
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.panel import Panel
from rich.table import Table

from scanner import scanner_containers
from state import State, Phase

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent.parent
TERRAFORM_DIR = BASE_DIR / "terraform"
ANSIBLE_DIR   = BASE_DIR / "ansible"
SSH_KEY       = Path.home() / ".ssh" / "migration_key"

console = Console()


# ─── Utilitaires ──────────────────────────────────────────────────────────────

def executer_cmd(commande: list, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        commande,
        shell=False,
        cwd=cwd,
        capture_output=True,
        text=True
    )


def afficher_titre(texte: str):
    console.print(Panel(f"[bold cyan]{texte}[/bold cyan]", expand=False))


def afficher_succes(texte: str):
    console.print(f"  [bold green]✓[/bold green] {texte}")


def afficher_erreur(texte: str):
    console.print(f"  [bold red]✗[/bold red] {texte}")


def afficher_info(texte: str):
    console.print(f"  [yellow]→[/yellow] {texte}")


# ─── Prérequis ────────────────────────────────────────────────────────────────

def verifier_prerequis():
    afficher_titre("Vérification des prérequis")
    erreurs = []

    outils = {
        "terraform": ["terraform", "version"],
        "ansible":   ["ansible", "--version"],
    }

    for nom, cmd in outils.items():
        r = executer_cmd(cmd)
        if r.returncode == 0:
            afficher_succes(f"{nom} disponible")
        else:
            afficher_erreur(f"{nom} introuvable")
            erreurs.append(nom)

    if not SSH_KEY.exists():
        afficher_erreur(f"Clé SSH introuvable : {SSH_KEY}")
        erreurs.append("ssh_key")
    else:
        afficher_succes("Clé SSH présente")

    r = executer_cmd(["ping", "-c", "1", "-W", "2", "10.0.0.10"])
    if r.returncode == 0:
        afficher_succes("vm-cible (OpenStack) joignable")
    else:
        afficher_erreur("vm-cible (OpenStack) non joignable")
        erreurs.append("openstack")

    if erreurs:
        console.print(f"\n[red]Prérequis manquants : {erreurs}[/red]")
        sys.exit(1)


# ─── Collecte des credentials ─────────────────────────────────────────────────

def collecter_credentials() -> dict:
    afficher_titre("Collecte des credentials")
    console.print("  Les credentials ne seront jamais affichés ni loggés.\n")

    return {
        "os_password":           getpass.getpass("  Mot de passe OpenStack (admin) : "),
        "mariadb_root_password": getpass.getpass("  Mot de passe MariaDB (root)    : "),
        "mariadb_app_password":  getpass.getpass("  Mot de passe MariaDB (appuser) : "),
    }


# ─── Phase 1 : Scan ───────────────────────────────────────────────────────────

def phase_scan(state: State) -> list:
    afficher_titre("Phase 1 : Scan des containers LXC")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Scan en cours...", total=5)

        containers = scanner_containers()

        for c in containers:
            progress.advance(task)
            afficher_info(f"{c.name} ({c.ip}) → services : {c.services}")

    state.phase_terminee(Phase.SCAN)
    return containers


# ─── Phase 2 : Génération terraform.tfvars ────────────────────────────────────

def generer_tfvars(containers: list, credentials: dict):
    afficher_titre("Phase 2 : Génération terraform.tfvars")

    ip_map = {
        "mariadb": "10.10.10.10",
        "apache":  "10.10.10.20",
        "backup":  "10.10.10.30",
        "ftp":     "10.10.10.40",
        "nfs":     "10.10.10.50",
    }

    flavor_map = {
        "mariadb": "m1.mariadb",
        "apache":  "m1.apache",
        "backup":  "m1.backup",
        "ftp":     "m1.ftp",
        "nfs":     "m1.nfs",
    }

    ssh_pub_key = (Path.home() / ".ssh" / "migration_key.pub").read_text().strip()

    instances_hcl = ""
    for nom, ip in ip_map.items():
        instances_hcl += f'  {nom} = {{ internal_ip = "{ip}", flavor = "{flavor_map[nom]}" }}\n'

    tfvars = f"""os_auth_url     = "http://10.0.0.10:5000/v3"
os_username     = "admin"
os_password     = "{credentials['os_password']}"
os_project_name = "admin"
os_region       = "RegionOne"

provider_network       = "provider"
migration_network_cidr = "10.10.10.0/24"
migration_gateway      = "10.10.10.1"

image_name     = "ubuntu-22.04"
ssh_public_key = "{ssh_pub_key}"

instances = {{
{instances_hcl}}}
"""

    tfvars_path = TERRAFORM_DIR / "terraform.tfvars"
    tfvars_path.write_text(tfvars)
    afficher_succes(f"terraform.tfvars généré : {tfvars_path}")


# ─── Phase 3 : Provisioning Terraform ────────────────────────────────────────

def phase_provisioning(state: State):
    afficher_titre("Phase 3 : Provisioning Terraform")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:

        task = progress.add_task("terraform init...", total=None)
        r = executer_cmd(["terraform", "init", "-no-color"], cwd=TERRAFORM_DIR)
        if r.returncode != 0:
            afficher_erreur(r.stderr)
            raise Exception("terraform init échoué")
        progress.update(task, description="terraform apply...")

        r = executer_cmd(
            ["terraform", "apply", "-auto-approve", "-no-color"],
            cwd=TERRAFORM_DIR
        )
        if r.returncode != 0:
            afficher_erreur(r.stderr)
            raise Exception("terraform apply échoué")

    afficher_succes("Infrastructure créée")

    r = executer_cmd(
        ["terraform", "output", "-json"],
        cwd=TERRAFORM_DIR
    )
    outputs = json.loads(r.stdout)
    instances = outputs["instances"]["value"]

    for nom, data in instances.items():
        state.enregistrer_ip(
            nom,
            lxc_ip=f"10.0.3.{['mariadb','apache','backup','ftp','nfs'].index(nom)*10+10}",
            internal_ip=data["internal_ip"],
            floating_ip=data["floating_ip"]
        )
        afficher_info(f"{nom} → internal: {data['internal_ip']} floating: {data['floating_ip']}")

    state.phase_terminee(Phase.PROVISIONING)
    return instances


# ─── Phase 4 : Génération inventaire Ansible ──────────────────────────────────

def generer_inventaire(instances: dict, containers: list):
    afficher_titre("Phase 4 : Génération inventaire Ansible")

    inventory = ""
    for nom, data in instances.items():
        inventory += f"[{nom}]\n"
        inventory += f"{data['floating_ip']} ansible_user=ubuntu "
        inventory += f"ansible_ssh_private_key_file={SSH_KEY}\n\n"

    inv_path = ANSIBLE_DIR / "inventory.ini"
    inv_path.write_text(inventory)
    afficher_succes(f"inventory.ini généré : {inv_path}")

    container_map = {c.name: c for c in containers}
    for nom, data in instances.items():
        c = container_map.get(nom)
        if not c:
            continue

        host_vars = {
            "old_lxc_ip":    c.ip,
            "internal_ip":   data["internal_ip"],
            "floating_ip":   data["floating_ip"],
        }

        if c.databases:
            host_vars["databases"] = [d.name for d in c.databases]
        if c.db_users:
            host_vars["old_apache_ip"] = next(
                (u.host for u in c.db_users if u.host != "%"), ""
            )
        if c.ftp_users:
            host_vars["ftp_users"] = [
                {"username": u.username, "home": u.home, "password_hash": ""}
                for u in c.ftp_users
            ]
        if c.vsftpd_config:
            host_vars["vsftpd_config"] = c.vsftpd_config.model_dump()
        if c.nfs_exports:
            host_vars["nfs_exports"] = [
                {"path": e.path, "subnet": e.subnet, "options": e.options}
                for e in c.nfs_exports
            ]

        hv_path = ANSIBLE_DIR / "host_vars" / f"{data['floating_ip']}.yml"
        hv_path.write_text(
            "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in host_vars.items())
        )
        afficher_succes(f"host_vars/{data['floating_ip']}.yml généré")


# ─── Phase 5 : Backup ─────────────────────────────────────────────────────────

def phase_backup(containers: list, credentials: dict):
    afficher_titre("Phase 5 : Backup des containers LXC")

    tmp_dir = tempfile.mkdtemp(mode=0o700)
    afficher_info(f"Répertoire temporaire : {tmp_dir}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Backup en cours...", total=len(containers))

        for c in containers:
            afficher_info(f"Backup {c.name}...")

            if "mariadb" in c.services:
                for db in c.databases:
                    r = executer_cmd([
                        "sudo", "lxc-attach", "-n", c.name, "--",
                        "mysqldump", "-u", "root",
                        f"-p{credentials['mariadb_root_password']}",
                        db.name
                    ])
                    if r.returncode == 0:
                        dump_path = os.path.join(tmp_dir, f"{db.name}.sql")
                        with open(dump_path, "w") as f:
                            f.write(r.stdout)
                        afficher_succes(f"Dump {db.name} OK")

            if "apache2" in c.services:
                executer_cmd([
                    "sudo", "tar", "-czf",
                    os.path.join(tmp_dir, "html.tar.gz"),
                    "-C", f"/var/lib/lxc/{c.name}/rootfs/var/www",
                    "html"
                ])
                executer_cmd([
                    "sudo", "tar", "-czf",
                    os.path.join(tmp_dir, "apache2.tar.gz"),
                    "-C", f"/var/lib/lxc/{c.name}/rootfs/etc",
                    "apache2"
                ])

            if "vsftpd" in c.services:
                for u in c.ftp_users:
                    username = u.username
                    home_rel = u.home.lstrip("/")
                    executer_cmd([
                        "sudo", "tar", "-czf",
                        os.path.join(tmp_dir, f"ftp_{username}.tar.gz"),
                        "-C", f"/var/lib/lxc/{c.name}/rootfs/{home_rel}",
                        "."
                    ])

            if "nfs-server" in c.services:
                executer_cmd([
                    "sudo", "tar", "-czf",
                    os.path.join(tmp_dir, "nfs_shared.tar.gz"),
                    "-C", f"/var/lib/lxc/{c.name}/rootfs/srv/nfs",
                    "shared"
                ])

            progress.advance(task)

    afficher_succes("Backup terminé")
    return tmp_dir


# ─── Phase 6 : Transfert ──────────────────────────────────────────────────────

def phase_transfert(instances: dict, tmp_dir: str):
    afficher_titre("Phase 6 : Transfert des archives")

    service_files = {
        "mariadb": ["app_db.sql", "sysmonitor.sql"],
        "apache":  ["html.tar.gz", "apache2.tar.gz"],
        "ftp":     ["ftp_ftpuser.tar.gz", "ftp_ftpuser1.tar.gz", "ftp_ftpuser2.tar.gz"],
        "nfs":     ["nfs_shared.tar.gz"],
        "backup":  [],
    }

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Transfert...", total=len(instances))

        for nom, data in instances.items():
            floating_ip = data["floating_ip"]
            fichiers = service_files.get(nom, [])

            if not fichiers:
                progress.advance(task)
                continue

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=floating_ip,
                username="ubuntu",
                key_filename=str(SSH_KEY)
            )

            client.exec_command("mkdir -p /tmp/migration")
            sftp = client.open_sftp()

            for fichier in fichiers:
                src = os.path.join(tmp_dir, fichier)
                if os.path.exists(src):
                    sftp.put(src, f"/tmp/migration/{fichier}")
                    afficher_succes(f"{fichier} → {nom} ({floating_ip})")

            sftp.close()
            client.close()
            progress.advance(task)

    shutil.rmtree(tmp_dir)
    afficher_succes("Répertoire temporaire supprimé")


# ─── Phase 7 : Ansible ────────────────────────────────────────────────────────

def lancer_ansible(playbook: str, inventaire: str):
    r = executer_cmd([
        "ansible-playbook",
        playbook,
        "-i", inventaire,
        "--ssh-extra-args", "-o StrictHostKeyChecking=no"
    ])
    if r.returncode != 0:
        raise Exception(f"Ansible {playbook} échoué :\n{r.stderr}")
    return r


def phase_ansible(state: State, etape: str, playbook: str):
    afficher_titre(f"Phase Ansible : {etape}")
    inventaire = str(ANSIBLE_DIR / "inventory.ini")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        progress.add_task(f"ansible-playbook {playbook}...", total=None)
        lancer_ansible(str(ANSIBLE_DIR / playbook), inventaire)

    afficher_succes(f"{etape} terminé")


# ─── Rollback ─────────────────────────────────────────────────────────────────

def rollback(state: State, erreur: str):
    afficher_erreur(f"Erreur : {erreur}")
    afficher_info("Rollback en cours...")

    r = executer_cmd(
        ["terraform", "destroy", "-auto-approve", "-no-color"],
        cwd=TERRAFORM_DIR
    )
    if r.returncode == 0:
        afficher_succes("Ressources OpenStack supprimées")
    else:
        afficher_erreur("Terraform destroy échoué — vérification manuelle nécessaire")

    state.marquer_echec(erreur)


# ─── Rapport final ────────────────────────────────────────────────────────────

def generer_rapport(state: State, instances: dict):
    afficher_titre("Rapport final")

    rapport = {
        "migration_date": datetime.now().isoformat(),
        "status": "success",
        "duree_secondes": (datetime.now() - datetime.fromisoformat(state.debut)).seconds,
        "instances": {},
        "dns_records_to_update": [],
        "load_balancer_pool": [],
    }

    for nom, data in instances.items():
        rapport["instances"][nom] = {
            "old_lxc_ip":   state.ip_mapping.get(nom, {}).get("lxc_ip", ""),
            "internal_ip":  data["internal_ip"],
            "floating_ip":  data["floating_ip"],
            "validation":   "passed"
        }

    rapport["dns_records_to_update"].append({
        "name": "SysMonitor.migration.local",
        "old_ip": state.ip_mapping.get("apache", {}).get("lxc_ip", ""),
        "new_ip": instances.get("apache", {}).get("floating_ip", "")
    })

    rapport["load_balancer_pool"].append({
        "service": "apache",
        "floating_ip": instances.get("apache", {}).get("floating_ip", ""),
        "internal_ip": instances.get("apache", {}).get("internal_ip", ""),
        "port": 80
    })

    rapport_path = BASE_DIR / "migration_report.json"
    rapport_path.write_text(json.dumps(rapport, indent=2))

    table = Table(title="Résumé de la migration")
    table.add_column("Service", style="cyan")
    table.add_column("Ancienne IP LXC", style="red")
    table.add_column("IP interne", style="green")
    table.add_column("Floating IP", style="green")

    for nom, data in instances.items():
        table.add_row(
            nom,
            state.ip_mapping.get(nom, {}).get("lxc_ip", ""),
            data["internal_ip"],
            data["floating_ip"]
        )

    console.print(table)
    afficher_succes(f"Rapport écrit : {rapport_path}")


# ─── Point d'entrée ───────────────────────────────────────────────────────────

def main():
    console.print(Panel.fit(
        "[bold cyan]Migration LXC → OpenStack[/bold cyan]\n"
        "[dim]PFE Master — Automatisation complète[/dim]",
        border_style="cyan"
    ))

    state = State.charger()
    instances = {}

    try:
        verifier_prerequis()
        credentials = collecter_credentials()

        if state.phase.value <= Phase.SCAN.value:
            containers = phase_scan(state)
        else:
            containers = scanner_containers()

        if state.phase.value <= Phase.PROVISIONING.value:
            generer_tfvars(containers, credentials)
            instances = phase_provisioning(state)
            generer_inventaire(instances, containers)
        else:
            r = executer_cmd(["terraform", "output", "-json"], cwd=TERRAFORM_DIR)
            instances = json.loads(r.stdout)["instances"]["value"]

        if state.phase.value <= Phase.BACKUP.value:
            tmp_dir = phase_backup(containers, credentials)
            phase_transfert(instances, tmp_dir)
            state.phase_terminee(Phase.BACKUP)

        if state.phase.value <= Phase.PROVISION.value:
            phase_ansible(state, "Provisionnement logiciel", "provision.yml")
            state.phase_terminee(Phase.PROVISION)

        if state.phase.value <= Phase.RESTORE.value:
            phase_ansible(state, "Restauration des services", "restore.yml")
            state.phase_terminee(Phase.RESTORE)

        if state.phase.value <= Phase.VALIDATE.value:
            phase_ansible(state, "Validation interne", "validate.yml")
            state.phase_terminee(Phase.VALIDATE)

        state.marquer_termine()
        generer_rapport(state, instances)

    except Exception as e:
        rollback(state, str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
