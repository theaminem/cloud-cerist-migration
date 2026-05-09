import subprocess
import json
import os
import sys
import getpass
import tempfile
import shutil
import time
import paramiko
from datetime import datetime
from pathlib import Path

from scanner import scanner_containers
from state import State, Phase

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent.parent
TERRAFORM_DIR = BASE_DIR / "terraform"
ANSIBLE_DIR   = BASE_DIR / "ansible"
SSH_KEY       = Path.home() / ".ssh" / "migration_key"


# ─── Affichage ────────────────────────────────────────────────────────────────

def titre(etape: str, total: int, texte: str):
    print(f"\n[{etape}/{total}] {texte}")
    print("-" * 50)


def ok(texte: str):
    print(f"  {texte:.<40} OK")


def fail(texte: str):
    print(f"  {texte:.<40} ECHEC")


def info(texte: str):
    print(f"  {texte}")


# ─── Utilitaires ──────────────────────────────────────────────────────────────

def executer_cmd(commande: list, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        commande,
        shell=False,
        cwd=cwd,
        capture_output=True,
        text=True
    )


def attendre_ssh(ip: str, timeout: int = 120):
    debut = time.time()
    while time.time() - debut < timeout:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=ip,
                username="ubuntu",
                key_filename=str(SSH_KEY),
                timeout=5
            )
            client.close()
            return True
        except Exception:
            time.sleep(5)
    return False


# ─── Prérequis ────────────────────────────────────────────────────────────────

def verifier_prerequis():
    titre("1", "9", "Verification des prerequis")
    erreurs = []

    outils = {
        "terraform": ["terraform", "version"],
        "ansible":   ["ansible", "--version"],
    }

    for nom, cmd in outils.items():
        r = executer_cmd(cmd)
        if r.returncode == 0:
            ok(nom)
        else:
            fail(nom)
            erreurs.append(nom)

    if SSH_KEY.exists():
        ok("cle SSH")
    else:
        fail("cle SSH")
        erreurs.append("ssh_key")

    r = executer_cmd(["ping", "-c", "1", "-W", "2", "10.0.0.10"])
    if r.returncode == 0:
        ok("vm-cible (OpenStack)")
    else:
        fail("vm-cible (OpenStack)")
        erreurs.append("openstack")

    if erreurs:
        print(f"\n  Prerequis manquants : {erreurs}")
        sys.exit(1)


# ─── Collecte des credentials ─────────────────────────────────────────────────

def collecter_credentials() -> dict:
    titre("2", "9", "Collecte des credentials")
    print("  Les credentials ne seront jamais affiches ni logges.\n")

    os_username = input("  Utilisateur OpenStack [migration-user] : ").strip() or "migration-user"
    os_project  = input("  Projet OpenStack [migration] : ").strip() or "migration"

    return {
        "os_username":           os_username,
        "os_project":            os_project,
        "os_password":           getpass.getpass("  Mot de passe OpenStack : "),
        "mariadb_root_password": getpass.getpass("  Mot de passe MariaDB root : "),
        "mariadb_app_password":  getpass.getpass("  Mot de passe MariaDB appuser : "),
    }


# ─── Phase Scan ───────────────────────────────────────────────────────────────

def phase_scan(state: State) -> list:
    titre("3", "9", "Scan des containers LXC")

    containers = scanner_containers()

    for c in containers:
        services_str = ", ".join(c.services) if c.services else "aucun"
        info(f"{c.name} ({c.ip}) ......... services : {services_str}")
        if c.databases:
            info(f"  bases : {', '.join(d.name for d in c.databases)}")

    state.phase_terminee(Phase.SCAN)
    return containers


# ─── Phase Terraform ──────────────────────────────────────────────────────────

def generer_tfvars(containers: list, credentials: dict):
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
os_username     = "{credentials['os_username']}"
os_password     = "{credentials['os_password']}"
os_project_name = "{credentials['os_project']}"
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


def phase_provisioning(state: State, credentials: dict):
    titre("4", "9", "Provisioning Terraform")

    generer_tfvars([], credentials)

    info("terraform init...")
    r = executer_cmd(["terraform", "init", "-no-color"], cwd=TERRAFORM_DIR)
    if r.returncode != 0:
        fail("terraform init")
        raise Exception("terraform init echoue")
    ok("terraform init")

    info("terraform apply...")
    r = executer_cmd(
        ["terraform", "apply", "-auto-approve", "-no-color"],
        cwd=TERRAFORM_DIR
    )
    if r.returncode != 0:
        fail("terraform apply")
        raise Exception(f"terraform apply echoue:\n{r.stderr[-500:]}")
    ok("terraform apply")

    r = executer_cmd(["terraform", "output", "-json"], cwd=TERRAFORM_DIR)
    outputs = json.loads(r.stdout)
    instances = outputs["instances"]["value"]

    lxc_ips = {
        "mariadb": "10.0.3.10",
        "apache":  "10.0.3.20",
        "backup":  "10.0.3.30",
        "ftp":     "10.0.3.40",
        "nfs":     "10.0.3.50",
    }

    for nom, data in instances.items():
        state.enregistrer_ip(
            nom,
            lxc_ip=lxc_ips.get(nom, ""),
            internal_ip=data["internal_ip"],
            floating_ip=data["floating_ip"]
        )
        info(f"  {nom:.<20} {data['internal_ip']} -> {data['floating_ip']}")

    # Supprime terraform.tfvars (contient le mot de passe)
    tfvars_path = TERRAFORM_DIR / "terraform.tfvars"
    if tfvars_path.exists():
        tfvars_path.unlink()
        info("  terraform.tfvars supprime (securite)")

    # Attente SSH sur toutes les instances
    info("")
    info("Attente SSH sur les instances...")
    for nom, data in instances.items():
        ip = data["floating_ip"]
        if attendre_ssh(ip):
            ok(f"SSH {nom} ({ip})")
        else:
            fail(f"SSH {nom} ({ip})")
            raise Exception(f"SSH timeout sur {nom} ({ip})")

    state.phase_terminee(Phase.PROVISIONING)
    return instances


# ─── Phase Inventaire Ansible ─────────────────────────────────────────────────

def generer_inventaire(instances: dict, containers: list):
    titre("5", "9", "Generation inventaire Ansible")

    inventory = ""
    for nom, data in instances.items():
        inventory += f"[{nom}]\n"
        inventory += f"{data['floating_ip']} ansible_user=ubuntu "
        inventory += f"ansible_ssh_private_key_file={SSH_KEY}\n\n"

    inv_path = ANSIBLE_DIR / "inventory.ini"
    inv_path.write_text(inventory)
    ok("inventory.ini")

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
        ok(f"host_vars {nom}")


# ─── Phase Backup ─────────────────────────────────────────────────────────────

def phase_backup(containers: list, credentials: dict):
    titre("6", "9", "Backup des containers LXC")

    tmp_dir = tempfile.mkdtemp()
    os.chmod(tmp_dir, 0o700)
    info(f"Repertoire temporaire : {tmp_dir}")

    for c in containers:
        info(f"Backup {c.name}...")

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
                    ok(f"dump {db.name}")
                else:
                    fail(f"dump {db.name}")

        if "apache2" in c.services:
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, "html.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/var/www",
                "html"
            ])
            if r.returncode == 0:
                ok("archive /var/www/html")
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, "apache2.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/etc",
                "apache2"
            ])
            if r.returncode == 0:
                ok("archive /etc/apache2")

        if "vsftpd" in c.services:
            for u in c.ftp_users:
                username = u.username
                home_rel = u.home.lstrip("/")
                r = executer_cmd([
                    "sudo", "tar", "-czf",
                    os.path.join(tmp_dir, f"ftp_{username}.tar.gz"),
                    "-C", f"/var/lib/lxc/{c.name}/rootfs/{home_rel}",
                    "."
                ])
                if r.returncode == 0:
                    ok(f"archive ftp {username}")

        if "nfs-server" in c.services:
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, "nfs_shared.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/srv/nfs",
                "shared"
            ])
            if r.returncode == 0:
                ok("archive /srv/nfs/shared")

    return tmp_dir


# ─── Phase Transfert ──────────────────────────────────────────────────────────

def phase_transfert(instances: dict, tmp_dir: str, state: State):
    titre("7", "9", "Transfert des archives")

    service_files = {
        "mariadb": ["app_db.sql", "sysmonitor.sql"],
        "apache":  ["html.tar.gz", "apache2.tar.gz"],
        "ftp":     ["ftp_ftpuser.tar.gz", "ftp_ftpuser1.tar.gz", "ftp_ftpuser2.tar.gz"],
        "nfs":     ["nfs_shared.tar.gz"],
        "backup":  [],
    }

    for nom, data in instances.items():
        floating_ip = data["floating_ip"]
        fichiers = service_files.get(nom, [])

        if not fichiers:
            continue

        info(f"Transfert vers {nom} ({floating_ip})...")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Retry connexion SSH avec banner timeout
        for tentative in range(5):
            try:
                client.connect(
                    hostname=floating_ip,
                    username="ubuntu",
                    key_filename=str(SSH_KEY),
                    timeout=30,
                    banner_timeout=60
                )
                break
            except Exception as e:
                if tentative < 4:
                    time.sleep(10)
                else:
                    raise e
        
        client.exec_command("mkdir -p /tmp/migration")
        sftp = client.open_sftp()

        for fichier in fichiers:
            src = os.path.join(tmp_dir, fichier)
            if os.path.exists(src):
                sftp.put(src, f"/tmp/migration/{fichier}")
                ok(f"{fichier} -> {nom}")
            else:
                fail(f"{fichier} introuvable")

        sftp.close()
        client.close()

    shutil.rmtree(tmp_dir)
    info("Repertoire temporaire supprime")
    state.phase_terminee(Phase.BACKUP)


# ─── Phase Ansible ────────────────────────────────────────────────────────────

def lancer_ansible(playbook: str, inventaire: str, extra_vars: dict = None):
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    env["ANSIBLE_FORCE_COLOR"] = "1"

    cmd = [
        "ansible-playbook",
        playbook,
        "-i", inventaire,
    ]
    if extra_vars:
        cmd += ["--extra-vars", json.dumps(extra_vars)]

    # Pas de capture_output : Ansible écrit directement sur le terminal en temps réel
    r = subprocess.run(cmd, shell=False, text=True, timeout=1800, env=env)
    if r.returncode != 0:
        raise Exception(f"Ansible echoue (code {r.returncode}) — voir la sortie ci-dessus")
    return r

def phase_ansible(state: State, phase: Phase, etape_num: str,
                   etape_nom: str, playbook: str, extra_vars: dict = None):
    titre(etape_num, "9", etape_nom)
    inventaire = str(ANSIBLE_DIR / "inventory.ini")

    info(f"ansible-playbook {playbook}...")
    lancer_ansible(str(ANSIBLE_DIR / playbook), inventaire, extra_vars)
    ok(etape_nom)
    state.phase_terminee(phase)


# ─── Rollback ─────────────────────────────────────────────────────────────────

def rollback(state: State, erreur: str):
    print(f"\n  ERREUR : {erreur}")
    print("  Rollback en cours...")

    r = executer_cmd(
        ["terraform", "destroy", "-auto-approve", "-no-color"],
        cwd=TERRAFORM_DIR
    )
    if r.returncode == 0:
        ok("Ressources OpenStack supprimees")
    else:
        fail("terraform destroy")

    state.marquer_echec(erreur)


# ─── Rapport final ────────────────────────────────────────────────────────────

def generer_rapport(state: State, instances: dict):
    titre("9", "9", "Rapport final")

    rapport = {
        "migration_date": datetime.now().isoformat(),
        "status": "success",
        "duree_secondes": (datetime.now() - datetime.fromisoformat(state.debut)).total_seconds(),
        "instances": {},
    }

    print(f"\n  {'Service':<12} {'Ancienne IP':<16} {'IP interne':<16} {'Floating IP':<16}")
    print(f"  {'-'*12} {'-'*16} {'-'*16} {'-'*16}")

    for nom, data in instances.items():
        lxc_ip = state.ip_mapping.get(nom, {}).get("lxc_ip", "")
        rapport["instances"][nom] = {
            "old_lxc_ip":   lxc_ip,
            "internal_ip":  data["internal_ip"],
            "floating_ip":  data["floating_ip"],
            "validation":   "passed"
        }
        print(f"  {nom:<12} {lxc_ip:<16} {data['internal_ip']:<16} {data['floating_ip']:<16}")

    rapport_path = BASE_DIR / "migration_report.json"
    rapport_path.write_text(json.dumps(rapport, indent=2))
    print(f"\n  Rapport ecrit : {rapport_path}")
    print("\n=== Migration terminee avec succes ===\n")


# ─── Point d'entrée ───────────────────────────────────────────────────────────

def main():
    print("\n=== Migration LXC -> OpenStack ===")
    print("    PFE Master - Automatisation complete\n")

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
            instances = phase_provisioning(state, credentials)
            generer_inventaire(instances, containers)
        else:
            r = executer_cmd(["terraform", "output", "-json"], cwd=TERRAFORM_DIR)
            instances = json.loads(r.stdout)["instances"]["value"]

        if state.phase.value <= Phase.BACKUP.value:
            tmp_dir = phase_backup(containers, credentials)
            phase_transfert(instances, tmp_dir, state)

        if state.phase.value <= Phase.PROVISION.value:
            phase_ansible(state, Phase.PROVISION, "8a", "Provisionnement logiciel", "provision.yml")

        if state.phase.value <= Phase.RESTORE.value:
            phase_ansible(
                state, Phase.RESTORE, "8b", "Restauration des services", "restore.yml",
                extra_vars={"mariadb_appuser_password": credentials["mariadb_app_password"]}
            )

        if state.phase.value <= Phase.VALIDATE.value:
            phase_ansible(state, Phase.VALIDATE, "8c", "Validation interne", "validate.yml")

        state.marquer_termine()
        generer_rapport(state, instances)

    except Exception as e:
        rollback(state, str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
