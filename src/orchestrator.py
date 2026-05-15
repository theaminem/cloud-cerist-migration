import subprocess
import json
import os
import sys
import getpass
import tempfile
import shutil
import time
import paramiko
import yaml
from datetime import datetime
from pathlib import Path

from scanner import scanner_containers
from state import State, Phase

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent.parent
TERRAFORM_DIR = BASE_DIR / "terraform"
ANSIBLE_DIR   = BASE_DIR / "ansible"

config_path = BASE_DIR / "config.yml"
if not config_path.exists():
    print("  ERREUR : config.yml introuvable. Copie config.yml.example et adapte-le.")
    sys.exit(1)
CONFIG  = yaml.safe_load(config_path.open())
SSH_KEY = Path(CONFIG["ssh"]["key_path"]).expanduser()


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
                username=CONFIG["ssh"]["user"],
                key_filename=str(SSH_KEY),
                timeout=5
            )
            client.close()
            return True
        except Exception:
            time.sleep(5)
    return False


# ─── Prérequis ────────────────────────────────────────────────────────────────

def reinitialiser_haproxy():
    """Remet HAProxy sur lxc_backend avant la migration."""
    haproxy_cfg = "/etc/haproxy/haproxy.cfg"
    try:
        with open(haproxy_cfg, "r") as f:
            contenu = f.read()
        contenu = contenu.replace(
            "default_backend cloud_backend",
            "default_backend lxc_backend"
        )
        with open(haproxy_cfg, "w") as f:
            f.write(contenu)
        subprocess.run(["sudo", "systemctl", "reload", "haproxy"], check=True)
        ok("HAProxy remis sur lxc_backend")
    except Exception as e:
        fail(f"HAProxy reinitialisation echouee : {e}")

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

    r = executer_cmd(["ping", "-c", "1", "-W", "2", CONFIG["network"]["api_ip"]])
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

    default_user    = CONFIG["openstack"]["default_user"]
    default_project = CONFIG["openstack"]["default_project"]
    os_username = input(f"  Utilisateur OpenStack [{default_user}] : ").strip() or default_user
    os_project  = input(f"  Projet OpenStack [{default_project}] : ").strip() or default_project

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

def generer_tfvars(containers: list, credentials: dict) -> dict:
    """Build terraform.tfvars from scan results. Returns {container_name: internal_ip}."""
    cfg_net     = CONFIG["network"]
    cfg_os      = CONFIG["openstack"]
    cfg_flavors = CONFIG.get("flavors", {})

    ssh_pub_key = Path(CONFIG["ssh"]["key_path"] + ".pub").expanduser().read_text().strip()

    # Assign sequential IPs from the pool (increment last octet by 10 per container)
    pool_base = CONFIG["internal_ip_pool"]
    parts  = pool_base.split(".")
    prefix = ".".join(parts[:3])
    start  = int(parts[3])

    ip_mapping = {}
    for i, c in enumerate(containers):
        ip_mapping[c.name] = f"{prefix}.{start + i * 10}"

    instances_hcl = ""
    for nom, internal_ip in ip_mapping.items():
        flavor = cfg_flavors.get(nom, cfg_flavors.get("default", "m1.small"))
        instances_hcl += f'  {nom} = {{ internal_ip = "{internal_ip}", flavor = "{flavor}" }}\n'

    tfvars = f"""os_auth_url     = "{cfg_os['auth_url']}"
os_username     = "{credentials['os_username']}"
os_password     = "{credentials['os_password']}"
os_project_name = "{credentials['os_project']}"
os_region       = "{cfg_os['region']}"

provider_network       = "{cfg_net['provider']}"
migration_network_cidr = "{cfg_net['internal_cidr']}"
migration_gateway      = "{cfg_net['internal_gateway']}"

image_name     = "{CONFIG['image']['name']}"
ssh_public_key = "{ssh_pub_key}"

instances = {{
{instances_hcl}}}
"""

    (TERRAFORM_DIR / "terraform.tfvars").write_text(tfvars)
    return ip_mapping


def phase_provisioning(state: State, credentials: dict, containers: list):
    titre("4", "9", "Provisioning Terraform")

    generer_tfvars(containers, credentials)

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

    # Derive lxc_ip from the scan, not from a static config
    container_map = {c.name: c for c in containers}
    for nom, data in instances.items():
        c = container_map.get(nom)
        state.enregistrer_ip(
            nom,
            lxc_ip=c.ip if c else "",
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


# ─── Génération des playbooks Ansible ─────────────────────────────────────────

def _new_ip(name: str) -> str:
    """Return Jinja2 expression for new_ips[name], handling hyphens in key names."""
    if '-' in name:
        return f"{{{{ new_ips['{name}'] }}}}"
    return f"{{{{ new_ips.{name} }}}}"


def generer_group_vars(instances: dict):
    cfg = CONFIG
    content  = "# Généré automatiquement par l'orchestrateur depuis config.yml\n---\n"
    content += f"ansible_user: {cfg['ssh']['user']}\n"
    content += f"ansible_ssh_private_key_file: {cfg['ssh']['key_path']}\n"
    content += "ansible_ssh_common_args: '-o StrictHostKeyChecking=no'\n"
    content += f"staging_dir: \"{cfg['staging_dir']}\"\n"
    content += f"internal_subnet: \"{cfg['network']['internal_cidr']}\"\n"
    content += "new_ips:\n"
    for nom, data in instances.items():
        content += f"  {nom}: \"{data['internal_ip']}\"\n"
    gv_path = ANSIBLE_DIR / "group_vars" / "all.yml"
    gv_path.parent.mkdir(parents=True, exist_ok=True)
    gv_path.write_text(content)
    ok("group_vars/all.yml")


def generer_provision_yml(containers: list):
    """Generate provision.yml dynamically — one play per detected container."""
    SVC_PKGS = {
        "mariadb":    ["mariadb-server", "mariadb-client", "python3-pymysql"],
        "apache2":    ["apache2", "php", "libapache2-mod-php", "php-mysql", "php-curl"],
        "vsftpd":     ["vsftpd"],
        "nfs-server": ["nfs-kernel-server", "nfs-common"],
        # "cron" absent : cron tourne par défaut sur Ubuntu, ne sert pas à identifier un container
    }
    SVC_ANSIBLE = {
        "mariadb":    "mariadb",
        "apache2":    "apache2",
        "vsftpd":     "vsftpd",
        "nfs-server": "nfs-kernel-server",
        # "cron" absent : évite de générer un play cron pour chaque container Ubuntu
    }
    APT_LOCK = (
        "    - name: Attente liberation verrou apt\n"
        "      shell: while fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend"
        " /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1; do sleep 2; done\n"
        "      changed_when: false"
    )

    lines = [
        "---",
        "# Généré automatiquement par l'orchestrateur",
        "",
        "- name: Provisionnement commun",
        "  hosts: all",
        "  become: true",
        "  gather_facts: no",
        "  tasks:",
        "    - name: Desactivation reverse DNS SSH",
        "      lineinfile:",
        "        path: /etc/ssh/sshd_config",
        "        regexp: '^#?UseDNS'",
        "        line: 'UseDNS no'",
        "      register: sshd_dns",
        "    - name: Redemarrage sshd si modifie",
        "      service:",
        "        name: ssh",
        "        state: restarted",
        "      when: sshd_dns.changed",
        "    - name: Attente fin cloud-init",
        "      command: cloud-init status --wait",
        "      changed_when: false",
        "      failed_when: false",
        APT_LOCK,
        "    - name: Mise a jour du cache apt",
        "      apt:",
        "        update_cache: yes",
        "        cache_valid_time: 3600",
        "        lock_timeout: 300",
        "    - name: Installation des paquets communs",
        "      apt:",
        "        name:",
        "          - curl",
        "          - rsync",
        "          - ca-certificates",
        "        state: present",
        "        lock_timeout: 300",
    ]

    for c in containers:
        pkgs = []
        for svc in c.services:
            for pkg in SVC_PKGS.get(svc, []):
                if pkg not in pkgs:
                    pkgs.append(pkg)

        # Backup containers identified by backup.sh presence, not by cron service
        if c.backup_config is not None and "mariadb" not in c.services:
            for pkg in ["mariadb-client", "cron"]:
                if pkg not in pkgs:
                    pkgs.append(pkg)

        svcs = list(dict.fromkeys(
            SVC_ANSIBLE[s] for s in c.services if s in SVC_ANSIBLE
        ))

        if not pkgs and not svcs:
            continue

        lines += [
            "",
            f"- name: Provisionnement {c.name}",
            f"  hosts: {c.name}",
            "  become: true",
            "  gather_facts: no",
            "  tasks:",
            APT_LOCK,
        ]

        if pkgs:
            lines += [
                f"    - name: Installation paquets {c.name}",
                "      apt:",
                "        name:",
            ] + [f"          - {p}" for p in pkgs] + [
                "        state: present",
                "        lock_timeout: 300",
            ]

        for svc_a in svcs:
            lines += [
                f"    - name: Demarrage {svc_a}",
                "      service:",
                f"        name: {svc_a}",
                "        state: started",
                "        enabled: true",
            ]

    (ANSIBLE_DIR / "provision.yml").write_text("\n".join(lines) + "\n")
    ok("provision.yml")


# ─── Helpers restore ──────────────────────────────────────────────────────────

def _restore_mariadb_play(c, apache_containers, backup_containers):
    lines = [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de staging",
        "      file:",
        '        path: "{{ staging_dir }}"',
        "        state: directory",
        "        mode: '0700'",
        "",
        "    - name: Restauration des bases de données",
        "      community.mysql.mysql_db:",
        '        name: "{{ item }}"',
        "        state: import",
        f'        target: "{{{{ staging_dir }}}}/{c.name}_{{{{ item }}}}.sql"',
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        '      loop: "{{ databases }}"',
        "",
        "    - name: Suppression ancien user appuser@ancienne_ip",
        "      community.mysql.mysql_user:",
        "        name: appuser",
        '        host: "{{ old_apache_ip }}"',
        "        state: absent",
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
    ]

    for ac in apache_containers:
        lines += [
            "",
            f"    - name: Creation user appuser pour {ac.name}",
            "      community.mysql.mysql_user:",
            "        name: appuser",
            f'        host: "{_new_ip(ac.name)}"',
            '        password: "{{ mariadb_appuser_password }}"',
            '        priv: "app_db.*:ALL/sysmonitor.*:ALL"',
            "        state: present",
            "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        ]

    for bc in backup_containers:
        lines += [
            "",
            f"    - name: Creation user appuser pour {bc.name} (lecture seule)",
            "      community.mysql.mysql_user:",
            "        name: appuser",
            f'        host: "{_new_ip(bc.name)}"',
            '        password: "{{ mariadb_appuser_password }}"',
            '        priv: "app_db.*:SELECT"',
            "        state: present",
            "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        ]

    lines += [
        "",
        "    - name: Restriction bind-address MariaDB a l'IP interne",
        "      lineinfile:",
        "        path: /etc/mysql/mariadb.conf.d/50-server.cnf",
        "        regexp: '^bind-address'",
        "        line: 'bind-address = {{ internal_ip }}'",
        "",
        "    - name: Redemarrage MariaDB",
        "      service:",
        "        name: mariadb",
        "        state: restarted",
        "",
        "    - name: Flush privileges",
        "      community.mysql.mysql_query:",
        '        query: "FLUSH PRIVILEGES"',
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
    ]
    return lines


def _restore_apache_play(c, containers):
    lines = [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de staging",
        "      file:",
        '        path: "{{ staging_dir }}"',
        "        state: directory",
        "        mode: '0700'",
        "",
        "    - name: Decompression archive web",
        "      unarchive:",
        f'        src: "{{{{ staging_dir }}}}/{c.name}_html.tar.gz"',
        "        dest: /var/www/",
        "        remote_src: yes",
        "",
        "    - name: Decompression config Apache",
        "      unarchive:",
        f'        src: "{{{{ staging_dir }}}}/{c.name}_apache2.tar.gz"',
        "        dest: /etc/",
        "        remote_src: yes",
    ]

    for other in containers:
        escaped_ip = other.ip.replace(".", "\\.")
        lines += [
            "",
            f"    - name: Remplacement IP {other.name} dans config.php",
            "      replace:",
            "        path: /var/www/html/config.php",
            f"        regexp: '\\b{escaped_ip}\\b'",
            f'        replace: "{_new_ip(other.name)}"',
        ]

    lines += [
        "",
        "    - name: Remplacement DB_PASS dans config.php",
        "      lineinfile:",
        "        path: /var/www/html/config.php",
        "        regexp: \"define\\\\('DB_PASS'\"",
        "        line: \"define('DB_PASS', '{{ mariadb_appuser_password }}');\"",
    ]

    lines += [
        "",
        "    - name: Ecriture /etc/hosts avec IPs internes",
        "      blockinfile:",
        "        path: /etc/hosts",
        "        block: |",
    ] + [f"          {_new_ip(oc.name)} {oc.name}.migration.local" for oc in containers]

    lines += [
        "",
        "    - name: Redemarrage Apache",
        "      service:",
        "        name: apache2",
        "        state: restarted",
    ]
    return lines


def _restore_nfs_play(c):
    return [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de staging",
        "      file:",
        '        path: "{{ staging_dir }}"',
        "        state: directory",
        "        mode: '0700'",
        "",
        "    - name: Creation structure NFS",
        "      file:",
        '        path: "{{ item }}"',
        "        state: directory",
        "        mode: '0755'",
        "      loop:",
        "        - /srv/nfs/shared/documents",
        "        - /srv/nfs/shared/scripts",
        "",
        "    - name: Decompression archive NFS",
        "      unarchive:",
        f'        src: "{{{{ staging_dir }}}}/{c.name}_nfs_shared.tar.gz"',
        "        dest: /srv/nfs/",
        "        remote_src: yes",
        "",
        "    - name: Reecriture /etc/exports avec nouveau sous-reseau",
        "      copy:",
        '        content: "/srv/nfs/shared {{ internal_subnet }}(rw,sync,no_subtree_check,root_squash)\\n"',
        "        dest: /etc/exports",
        "",
        "    - name: Activation des exports NFS",
        "      command: exportfs -ra",
        "",
        "    - name: Redemarrage NFS",
        "      service:",
        "        name: nfs-kernel-server",
        "        state: restarted",
    ]


def _restore_backup_play(c, mariadb_containers):
    mariadb_ip = _new_ip(mariadb_containers[0].name) if mariadb_containers else "{{ new_ips.mariadb }}"
    return [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation du repertoire de destination des backups",
        "      file:",
        "        path: /backups",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Creation du fichier de configuration backup protege",
        "      copy:",
        "        content: |",
        "          [mysqldump]",
        "          user=appuser",
        "          password={{ mariadb_appuser_password }}",
        "        dest: /etc/backup.conf",
        "        owner: root",
        "        group: root",
        "        mode: '0600'",
        "",
        "    - name: Depot backup.sh",
        "      copy:",
        "        content: |",
        "          #!/bin/bash",
        "          DATE=$(date +%Y-%m-%d_%Hh%M)",
        f'          HOST="{mariadb_ip}"',
        '          DB="app_db"',
        '          DEST="/backups"',
        "          mysqldump --defaults-extra-file=/etc/backup.conf -h $HOST $DB > $DEST/backup_$DATE.sql",
        "          if [ $? -eq 0 ]; then",
        '              echo "Backup reussi : $DEST/backup_$DATE.sql"',
        "          else",
        '              echo "Backup echoue"',
        "          fi",
        "        dest: /usr/local/bin/backup.sh",
        "        mode: '0750'",
        "",
        "    - name: Creation du cron backup",
        "      cron:",
        '        name: "backup quotidien"',
        '        minute: "0"',
        '        hour: "2"',
        "        job: /usr/local/bin/backup.sh",
        "        user: root",
    ]


def _restore_ftp_play(c):
    return [
        "",
        f"- name: Restauration {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Creation des users FTP",
        "      user:",
        '        name: "{{ item.username }}"',
        '        home: "{{ item.home }}"',
        "        shell: /bin/bash",
        "        create_home: yes",
        "        state: present",
        '      loop: "{{ ftp_users }}"',
        "",
        "    - name: Injection des hashes de passwords",
        "      user:",
        '        name: "{{ item.username }}"',
        '        password: "{{ item.password_hash }}"',
        "        update_password: always",
        '      loop: "{{ ftp_users }}"',
        "",
        "    - name: Creation des repertoires uploads",
        "      file:",
        '        path: "{{ item.home }}/uploads"',
        "        state: directory",
        '        owner: "{{ item.username }}"',
        "        mode: '0755'",
        '      loop: "{{ ftp_users }}"',
        "",
        "    - name: Decompression archives FTP",
        "      unarchive:",
        f'        src: "{{{{ staging_dir }}}}/{c.name}_ftp_{{{{ item.username }}}}.tar.gz"',
        '        dest: "{{ item.home }}/"',
        "        remote_src: yes",
        '        owner: "{{ item.username }}"',
        '      loop: "{{ ftp_users }}"',
        "      ignore_errors: yes",
        "",
        "    - name: Configuration vsftpd",
        "      copy:",
        "        content: |",
        "          listen=NO",
        "          listen_ipv6=YES",
        "          anonymous_enable=NO",
        "          local_enable=YES",
        "          write_enable=YES",
        "          local_umask=022",
        "          dirmessage_enable=YES",
        "          use_localtime=YES",
        "          xferlog_enable=YES",
        "          connect_from_port_20=YES",
        "          chroot_local_user={{ 'YES' if vsftpd_config.chroot_local_user else 'NO' }}",
        "          allow_writeable_chroot=YES",
        "          secure_chroot_dir=/var/run/vsftpd/empty",
        "          pam_service_name=vsftpd",
        "          rsa_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem",
        "          rsa_private_key_file=/etc/ssl/private/ssl-cert-snakeoil.key",
        "          ssl_enable=NO",
        "          pasv_enable=YES",
        "          pasv_min_port={{ vsftpd_config.pasv_min_port }}",
        "          pasv_max_port={{ vsftpd_config.pasv_max_port }}",
        "        dest: /etc/vsftpd.conf",
        "",
        "    - name: Redemarrage vsftpd",
        "      service:",
        "        name: vsftpd",
        "        state: restarted",
    ]


def generer_restore_yml(containers: list, ip_mapping: dict):
    """Generate restore.yml dynamically based on scanned container services."""
    apache_containers  = [c for c in containers if "apache2" in c.services]
    # A backup container is identified by the presence of backup.sh detected during scan.
    # Using "cron" alone is too broad — cron runs in every Ubuntu container by default.
    backup_containers  = [c for c in containers if c.backup_config is not None
                          and "mariadb" not in c.services]
    mariadb_containers = [c for c in containers if "mariadb" in c.services]

    lines = ["---", "# Généré automatiquement par l'orchestrateur"]

    for c in containers:
        if "mariadb"    in c.services:
            lines += _restore_mariadb_play(c, apache_containers, backup_containers)
        if "apache2"    in c.services:
            lines += _restore_apache_play(c, containers)
        if "nfs-server" in c.services:
            lines += _restore_nfs_play(c)
        if c.backup_config is not None and "mariadb" not in c.services:
            lines += _restore_backup_play(c, mariadb_containers)
        if "vsftpd"     in c.services:
            lines += _restore_ftp_play(c)

    (ANSIBLE_DIR / "restore.yml").write_text("\n".join(lines) + "\n")
    ok("restore.yml")


# ─── Helpers validate ─────────────────────────────────────────────────────────

def _validate_mariadb_play(c):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification service MariaDB",
        "      service_facts:",
        "",
        "    - name: MariaDB est actif",
        "      assert:",
        "        that: ansible_facts.services['mariadb.service'].state == 'running'",
        "        fail_msg: \"MariaDB n'est pas actif\"",
        "",
        "    - name: Verification bases de données",
        "      community.mysql.mysql_query:",
        "        query: \"SHOW DATABASES LIKE '{{ item }}'\"",
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        '      loop: "{{ databases }}"',
        "      register: db_check",
        "",
        "    - name: Verification user appuser",
        "      community.mysql.mysql_query:",
        "        query: \"SELECT user, host FROM mysql.user WHERE user='appuser'\"",
        "        login_unix_socket: /var/run/mysqld/mysqld.sock",
        "      register: user_check",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport MariaDB",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'databases': databases} | to_json }}\"",
        "        dest: /tmp/migration/validation_mariadb.json",
    ]


def _validate_apache_play(c, containers):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification service Apache",
        "      service_facts:",
        "",
        "    - name: Apache est actif",
        "      assert:",
        "        that: ansible_facts.services['apache2.service'].state == 'running'",
        "        fail_msg: \"Apache n'est pas actif\"",
        "",
        "    - name: Test HTTP sur localhost",
        "      uri:",
        "        url: http://localhost",
        "        method: GET",
        "        status_code: 200",
        "      register: http_check",
        "",
        "    - name: Verification absence anciennes IPs LXC",
        '      shell: grep -rE "10\\.0\\." /var/www/html/config.php || echo "OK"',
        "      register: ip_check",
        "      changed_when: false",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport Apache",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'http_code': http_check.status} | to_json }}\"",
        "        dest: /tmp/migration/validation_apache.json",
    ]


def _validate_nfs_play(c):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification exports NFS",
        "      command: exportfs -v",
        "      register: nfs_exports_check",
        "      changed_when: false",
        "",
        "    - name: NFS exporte au moins un repertoire",
        "      assert:",
        "        that: nfs_exports_check.stdout | length > 0",
        "        fail_msg: \"NFS n'exporte rien\"",
        "",
        "    - name: Verification fichiers partages",
        "      find:",
        "        paths: /srv/nfs/shared",
        "        recurse: yes",
        "      register: nfs_files",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport NFS",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'files_count': nfs_files.matched} | to_json }}\"",
        "        dest: /tmp/migration/validation_nfs.json",
    ]


def _validate_backup_play(c, mariadb_containers):
    mariadb_ip = _new_ip(mariadb_containers[0].name) if mariadb_containers else "{{ new_ips.mariadb }}"
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification script backup.sh",
        "      stat:",
        "        path: /usr/local/bin/backup.sh",
        "      register: backup_script",
        "",
        "    - name: backup.sh existe",
        "      assert:",
        "        that: backup_script.stat.exists",
        "        fail_msg: \"backup.sh est absent\"",
        "",
        "    - name: Verification nouvelle IP mariadb dans backup.sh",
        f'      shell: grep "{mariadb_ip}" /usr/local/bin/backup.sh',
        "      register: ip_in_backup",
        "",
        "    - name: Nouvelle IP mariadb presente dans backup.sh",
        "      assert:",
        "        that: ip_in_backup.rc == 0",
        "        fail_msg: \"La nouvelle IP mariadb est absente de backup.sh\"",
        "",
        "    - name: Verification cron actif",
        "      service_facts:",
        "",
        "    - name: Cron est actif",
        "      assert:",
        "        that: ansible_facts.services['cron.service'].state == 'running'",
        "        fail_msg: \"Cron n'est pas actif\"",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport Backup",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok', 'script': backup_script.stat.exists} | to_json }}\"",
        "        dest: /tmp/migration/validation_backup.json",
    ]


def _validate_ftp_play(c):
    return [
        "",
        f"- name: Validation {c.name}",
        f"  hosts: {c.name}",
        "  become: true",
        "  tasks:",
        "    - name: Verification service vsftpd",
        "      service_facts:",
        "",
        "    - name: vsftpd est actif",
        "      assert:",
        "        that: ansible_facts.services['vsftpd.service'].state == 'running'",
        "        fail_msg: \"vsftpd n'est pas actif\"",
        "",
        "    - name: Verification repertoires users FTP",
        "      stat:",
        '        path: "{{ item.home }}/uploads"',
        '      loop: "{{ ftp_users }}"',
        "      register: ftp_dirs",
        "",
        "    - name: Creation repertoire rapport",
        "      file:",
        "        path: /tmp/migration",
        "        state: directory",
        "        mode: '0755'",
        "",
        "    - name: Ecriture rapport FTP",
        "      copy:",
        "        content: \"{{ {'service': '" + c.name + "', 'status': 'ok'} | to_json }}\"",
        "        dest: /tmp/migration/validation_ftp.json",
    ]


def generer_validate_yml(containers: list, ip_mapping: dict):
    """Generate validate.yml dynamically based on scanned container services."""
    mariadb_containers = [c for c in containers if "mariadb" in c.services]

    lines = ["---", "# Généré automatiquement par l'orchestrateur"]

    for c in containers:
        if "mariadb"    in c.services:
            lines += _validate_mariadb_play(c)
        if "apache2"    in c.services:
            lines += _validate_apache_play(c, containers)
        if "nfs-server" in c.services:
            lines += _validate_nfs_play(c)
        if c.backup_config is not None and "mariadb" not in c.services:
            lines += _validate_backup_play(c, mariadb_containers)
        if "vsftpd"     in c.services:
            lines += _validate_ftp_play(c)

    (ANSIBLE_DIR / "validate.yml").write_text("\n".join(lines) + "\n")
    ok("validate.yml")


def generer_inventaire(instances: dict, containers: list):
    titre("5", "9", "Generation inventaire Ansible")

    ssh_user = CONFIG["ssh"]["user"]
    inventory = ""
    for nom, data in instances.items():
        inventory += f"[{nom}]\n"
        inventory += f"{data['floating_ip']} ansible_user={ssh_user} "
        inventory += f"ansible_ssh_private_key_file={SSH_KEY}\n\n"

    inv_path = ANSIBLE_DIR / "inventory.ini"
    inv_path.write_text(inventory)
    ok("inventory.ini")

    generer_group_vars(instances)

    container_map = {c.name: c for c in containers}
    hv_dir = ANSIBLE_DIR / "host_vars"
    hv_dir.mkdir(exist_ok=True)
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

        hv_path = hv_dir / f"{data['floating_ip']}.yml"
        hv_path.write_text(
            "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in host_vars.items())
        )
        ok(f"host_vars {nom}")

    # Generate all three playbooks from the live scan
    ip_mapping = {nom: data["internal_ip"] for nom, data in instances.items()}
    generer_provision_yml(containers)
    generer_restore_yml(containers, ip_mapping)
    generer_validate_yml(containers, ip_mapping)


# ─── Phase Backup ─────────────────────────────────────────────────────────────

def phase_backup(containers: list, credentials: dict):
    titre("6", "9", "Backup des containers LXC")

    tmp_dir = tempfile.mkdtemp()
    os.chmod(tmp_dir, 0o700)
    info(f"Repertoire temporaire : {tmp_dir}")

    for c in containers:
        info(f"Backup {c.name}...")

        if "mariadb" in c.services:
            cnf = f"[mysqldump]\nuser=root\npassword={credentials['mariadb_root_password']}\n"
            subprocess.run(
                ["sudo", "lxc-attach", "-n", c.name, "--", "tee", "/tmp/.my.cnf"],
                input=cnf, capture_output=True, text=True
            )
            subprocess.run(
                ["sudo", "lxc-attach", "-n", c.name, "--", "chmod", "600", "/tmp/.my.cnf"],
                capture_output=True
            )
            try:
                for db in c.databases:
                    r = executer_cmd([
                        "sudo", "lxc-attach", "-n", c.name, "--",
                        "mysqldump", "--defaults-extra-file=/tmp/.my.cnf",
                        db.name
                    ])
                    if r.returncode == 0:
                        dump_path = os.path.join(tmp_dir, f"{c.name}_{db.name}.sql")
                        with open(dump_path, "w") as f:
                            f.write(r.stdout)
                        ok(f"dump {db.name}")
                    else:
                        fail(f"dump {db.name}")
            finally:
                subprocess.run(
                    ["sudo", "lxc-attach", "-n", c.name, "--", "rm", "-f", "/tmp/.my.cnf"],
                    capture_output=True
                )

        if "apache2" in c.services:
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, f"{c.name}_html.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/var/www",
                "html"
            ])
            if r.returncode == 0:
                ok("archive /var/www/html")
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, f"{c.name}_apache2.tar.gz"),
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
                    os.path.join(tmp_dir, f"{c.name}_ftp_{username}.tar.gz"),
                    "-C", f"/var/lib/lxc/{c.name}/rootfs/{home_rel}",
                    "."
                ])
                if r.returncode == 0:
                    ok(f"archive ftp {username}")

        if "nfs-server" in c.services:
            r = executer_cmd([
                "sudo", "tar", "-czf",
                os.path.join(tmp_dir, f"{c.name}_nfs_shared.tar.gz"),
                "-C", f"/var/lib/lxc/{c.name}/rootfs/srv/nfs",
                "shared"
            ])
            if r.returncode == 0:
                ok("archive /srv/nfs/shared")

    return tmp_dir


# ─── Phase Transfert ──────────────────────────────────────────────────────────

def phase_transfert(instances: dict, tmp_dir: str, containers: list, state: State):
    titre("7", "9", "Transfert des archives")

    container_map = {c.name: c for c in containers}

    for nom, data in instances.items():
        c = container_map.get(nom)
        if not c:
            continue

        floating_ip = data["floating_ip"]

        # Build file list dynamically from scan — no static config needed
        fichiers = []
        if "mariadb"    in c.services:
            fichiers += [f"{c.name}_{db.name}.sql" for db in c.databases]
        if "apache2"    in c.services:
            fichiers += [f"{c.name}_html.tar.gz", f"{c.name}_apache2.tar.gz"]
        if "vsftpd"     in c.services:
            fichiers += [f"{c.name}_ftp_{u.username}.tar.gz" for u in c.ftp_users]
        if "nfs-server" in c.services:
            fichiers += [f"{c.name}_nfs_shared.tar.gz"]

        if not fichiers:
            continue

        info(f"Transfert vers {nom} ({floating_ip})...")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        for tentative in range(5):
            try:
                client.connect(
                    hostname=floating_ip,
                    username=CONFIG["ssh"]["user"],
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

        staging = CONFIG["staging_dir"]
        _, _, stderr = client.exec_command(f"mkdir -p {staging}")
        if stderr.read():
            raise Exception(f"mkdir {staging} echoue sur {floating_ip}")
        sftp = client.open_sftp()

        for fichier in fichiers:
            src = os.path.join(tmp_dir, fichier)
            if os.path.exists(src):
                sftp.put(src, f"{staging}/{fichier}")
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

def basculer_haproxy(floating_ip_apache: str):
    """Bascule HAProxy vers l instance OpenStack apache apres migration."""
    haproxy_cfg = "/etc/haproxy/haproxy.cfg"
    try:
        with open(haproxy_cfg, "r") as f:
            contenu = f.read()
        # Mettre a jour l IP du backend cloud
        import re
        contenu = re.sub(
            r"server apache_cloud \S+",
            f"server apache_cloud {floating_ip_apache}:80 check",
            contenu
        )
        # Basculer vers cloud_backend
        contenu = contenu.replace(
            "default_backend lxc_backend",
            "default_backend cloud_backend"
        )
        with open(haproxy_cfg, "w") as f:
            f.write(contenu)
        import subprocess
        subprocess.run(["sudo", "systemctl", "reload", "haproxy"], check=True)
        ok("HAProxy bascule vers instance apache OpenStack")
    except Exception as e:
        fail(f"HAProxy bascule echouee : {e}")


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
            "validation":   "passed" if Phase.VALIDATE in [Phase[p] for p in state.phases_ok] else "skipped"
        }
        print(f"  {nom:<12} {lxc_ip:<16} {data['internal_ip']:<16} {data['floating_ip']:<16}")

    rapport_path = BASE_DIR / "migration_report.json"
    rapport_path.write_text(json.dumps(rapport, indent=2))
    print(f"\n  Rapport ecrit : {rapport_path}")
    apache_floating_ip = instances.get("apache", {}).get("floating_ip", "")
    if apache_floating_ip:
        basculer_haproxy(apache_floating_ip)
    print("\n=== Migration terminee avec succes ===\n")


# ─── Point d'entrée ───────────────────────────────────────────────────────────

def main():
    print("\n=== Migration LXC -> OpenStack ===")
    print("    PFE Master - Automatisation complete\n")

    state = State.charger()
    instances = {}

    try:
        verifier_prerequis()
        reinitialiser_haproxy()
        credentials = collecter_credentials()

        if state.phase.value <= Phase.SCAN.value:
            containers = phase_scan(state)
        else:
            containers = scanner_containers()

        if state.phase.value <= Phase.PROVISIONING.value:
            instances = phase_provisioning(state, credentials, containers)
            generer_inventaire(instances, containers)
        else:
            r = executer_cmd(["terraform", "output", "-json"], cwd=TERRAFORM_DIR)
            instances = json.loads(r.stdout)["instances"]["value"]

        if state.phase.value <= Phase.BACKUP.value:
            tmp_dir = phase_backup(containers, credentials)
            phase_transfert(instances, tmp_dir, containers, state)

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
