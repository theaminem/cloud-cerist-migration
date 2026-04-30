import subprocess
import re
from pydantic import BaseModel, field_validator
from typing import List, Optional


# ─── Modèles de données ───────────────────────────────────────────────────────

class Database(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def nom_valide(cls, v):
        if not re.match(r'^[a-zA-Z0-9_]+$', v):
            raise ValueError(f"Nom de base invalide : {v}")
        return v


class DBUser(BaseModel):
    user: str
    host: str


class FTPUser(BaseModel):
    username: str
    home: str


class NFSExport(BaseModel):
    path: str
    subnet: str
    options: str


class VSFTPDConfig(BaseModel):
    chroot_local_user: bool
    pasv_min_port: int
    pasv_max_port: int
    write_enable: bool
    local_enable: bool


class BackupConfig(BaseModel):
    host: str
    database: str
    destination: str


class ApacheConfig(BaseModel):
    config_php_path: str
    ips_trouvees: List[str]


class Container(BaseModel):
    name: str
    ip: str
    state: str
    services: List[str]
    packages: List[str] = []
    databases: List[Database] = []
    db_users: List[DBUser] = []
    ftp_users: List[FTPUser] = []
    nfs_exports: List[NFSExport] = []
    vsftpd_config: Optional[VSFTPDConfig] = None
    backup_config: Optional[BackupConfig] = None
    apache_config: Optional[ApacheConfig] = None
    ram_used_mb: int = 0
    disk_used_gb: int = 0

    @field_validator("name")
    @classmethod
    def nom_valide(cls, v):
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError(f"Nom de container invalide : {v}")
        return v

    @field_validator("ip")
    @classmethod
    def ip_valide(cls, v):
        if not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', v):
            raise ValueError(f"IP invalide : {v}")
        return v


# ─── Mapping service systemd → paquet apt ─────────────────────────────────────

SERVICE_TO_PACKAGE = {
    "apache2"    : "apache2",
    "mariadb"    : "mariadb-server",
    "vsftpd"     : "vsftpd",
    "nfs-server" : "nfs-kernel-server",
    "cron"       : "cron",
}


# ─── Exécution locale ─────────────────────────────────────────────────────────

def executer(commande: list) -> str:
    resultat = subprocess.run(
        commande,
        shell=False,
        capture_output=True,
        text=True
    )
    return resultat.stdout.strip()


# ─── Détection NFS ────────────────────────────────────────────────────────────

def detecter_service_nfs(nom: str) -> bool:
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "systemctl", "is-active", "nfs-server"
    ])
    return sortie.strip() == "active"


# ─── Métriques RAM et disque ──────────────────────────────────────────────────

def lire_ram(nom: str) -> int:
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "free", "-m"
    ])
    for ligne in sortie.splitlines():
        if ligne.startswith("Mem:"):
            parties = ligne.split()
            return int(parties[2])
    return 0


def lire_disk(nom: str) -> int:
    sortie = executer([
        "sudo", "du", "-sb",
        f"/var/lib/lxc/{nom}/rootfs/"
    ])
    if not sortie:
        return 0
    parties = sortie.split()
    octets = int(parties[0])
    return max(1, octets // (1024 ** 3))

# ─── Users MariaDB ────────────────────────────────────────────────────────────

def lire_users_mariadb(nom: str) -> List[DBUser]:
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "mysql", "-u", "root", "-sN",
        "-e", "SELECT user, host FROM mysql.user;"
    ])
    exclus = {"root", "mariadb.sys", "mysql"}
    users = []
    for ligne in sortie.splitlines():
        parties = ligne.strip().split()
        if len(parties) == 2 and parties[0] not in exclus:
            users.append(DBUser(user=parties[0], host=parties[1]))
    return users


# ─── Users FTP ───────────────────────────────────────────────────────────────

def lire_users_ftp(nom: str) -> List[FTPUser]:
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "cat", "/etc/passwd"
    ])
    exclus_shells = ["/usr/sbin/nologin", "/bin/false"]
    exclus_users = {
        "root", "daemon", "bin", "sys", "sync",
        "games", "man", "lp", "mail", "news",
        "uucp", "proxy", "www-data", "backup",
        "list", "irc", "gnats", "nobody",
        "systemd-network", "systemd-resolve",
        "messagebus", "syslog", "_apt",
        "tss", "uuidd", "tcpdump", "sshd",
        "pollinate", "landscape", "fwupd-refresh",
        "ubuntu"
    }
    users = []
    for ligne in sortie.splitlines():
        parties = ligne.split(":")
        if len(parties) < 7:
            continue
        username = parties[0]
        home     = parties[5]
        shell    = parties[6]
        if username in exclus_users:
            continue
        if shell in exclus_shells:
            continue
        users.append(FTPUser(username=username, home=home))
    return users


# ─── Exports NFS ─────────────────────────────────────────────────────────────

def lire_exports_nfs(nom: str) -> List[NFSExport]:
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "cat", "/etc/exports"
    ])
    exports = []
    for ligne in sortie.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        parties = ligne.split()
        if len(parties) < 2:
            continue
        path  = parties[0]
        reste = parties[1]
        if "(" in reste:
            subnet  = reste[:reste.index("(")]
            options = reste[reste.index("(")+1:reste.index(")")]
        else:
            subnet  = reste
            options = ""
        exports.append(NFSExport(path=path, subnet=subnet, options=options))
    return exports


# ─── Config vsftpd ───────────────────────────────────────────────────────────

def lire_vsftpd_config(nom: str) -> VSFTPDConfig:
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "cat", "/etc/vsftpd.conf"
    ])
    params = {}
    for ligne in sortie.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        if "=" in ligne:
            cle, valeur = ligne.split("=", 1)
            params[cle.strip()] = valeur.strip()

    return VSFTPDConfig(
        chroot_local_user = params.get("chroot_local_user", "NO") == "YES",
        pasv_min_port     = int(params.get("pasv_min_port", 40000)),
        pasv_max_port     = int(params.get("pasv_max_port", 40100)),
        write_enable      = params.get("write_enable", "NO") == "YES",
        local_enable      = params.get("local_enable", "NO") == "YES",
    )


# ─── Config backup.sh ────────────────────────────────────────────────────────

def lire_backup_config(nom: str) -> Optional[BackupConfig]:
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "cat", "/usr/local/bin/backup.sh"
    ])
    if not sortie:
        return None
    params = {}
    for ligne in sortie.splitlines():
        ligne = ligne.strip()
        for cle in ["HOST", "DB", "DEST"]:
            if ligne.startswith(f"{cle}="):
                valeur = ligne.split("=", 1)[1].strip().strip('"')
                params[cle] = valeur
    if not params:
        return None
    return BackupConfig(
        host        = params.get("HOST", ""),
        database    = params.get("DB", ""),
        destination = params.get("DEST", ""),
    )


# ─── Config Apache / config.php ──────────────────────────────────────────────

def lire_apache_config(nom: str) -> Optional[ApacheConfig]:
    chemin = "/var/www/html/config.php"
    sortie = executer([
        "sudo", "lxc-attach", "-n", nom, "--",
        "cat", chemin
    ])
    if not sortie:
        return None
    ips = re.findall(r'\b10\.0\.3\.\d{1,3}\b', sortie)
    ips = list(dict.fromkeys(ips))
    return ApacheConfig(
        config_php_path = chemin,
        ips_trouvees    = ips
    )


# ─── Scan des containers ──────────────────────────────────────────────────────

def scanner_containers() -> List[Container]:
    containers = []

    sortie = executer(["sudo", "lxc-ls", "--running"])
    noms   = sortie.split()

    for nom in noms:
        ip   = executer(["sudo", "lxc-info", "-n", nom, "-iH"]).strip()
        etat = executer(["sudo", "lxc-info", "-n", nom, "-sH"]).strip()

        services_bruts = executer([
            "sudo", "lxc-attach", "-n", nom, "--",
            "systemctl", "list-units", "--type=service",
            "--state=running", "--no-legend", "--no-pager"
        ])

        exclus = [
            "console-getty", "container-getty",
            "dbus", "rsyslog", "systemd-journald",
            "systemd-logind", "systemd-networkd",
            "systemd-resolved", "rpcbind",
            "fsidd", "nfs-blkmap", "nfs-idmapd",
            "nfs-mountd", "nfsdcld", "rpc-statd",
            "ssh", "sshd"
        ]

        services = []
        for ligne in services_bruts.splitlines():
            parties = ligne.strip().split()
            if not parties:
                continue
            nom_service = parties[0].replace(".service", "")
            if not any(nom_service.startswith(e) for e in exclus):
                services.append(nom_service)

        if detecter_service_nfs(nom):
            if "nfs-server" not in services:
                services.append("nfs-server")

        packages = [
            SERVICE_TO_PACKAGE[s]
            for s in services
            if s in SERVICE_TO_PACKAGE
        ]

        bases    = []
        db_users = []
        if "mariadb" in services:
            bases_brutes = executer([
                "sudo", "lxc-attach", "-n", nom, "--",
                "mysql", "-u", "root", "-e", "SHOW DATABASES;"
            ])
            systeme = {
                "information_schema", "mysql",
                "performance_schema", "sys"
            }
            for ligne in bases_brutes.splitlines()[1:]:
                db = ligne.strip()
                if db and db not in systeme:
                    bases.append(Database(name=db))
            db_users = lire_users_mariadb(nom)

        ftp_users     = lire_users_ftp(nom)     if "vsftpd"     in services else []
        nfs_exports   = lire_exports_nfs(nom)   if "nfs-server" in services else []
        vsftpd_config = lire_vsftpd_config(nom) if "vsftpd"     in services else None
        backup_config = lire_backup_config(nom)
        apache_config = lire_apache_config(nom) if "apache2"    in services else None

        ram  = lire_ram(nom)
        disk = lire_disk(nom)

        container = Container(
            name          = nom,
            ip            = ip,
            state         = etat,
            services      = services,
            packages      = packages,
            databases     = bases,
            db_users      = db_users,
            ftp_users     = ftp_users,
            nfs_exports   = nfs_exports,
            vsftpd_config = vsftpd_config,
            backup_config = backup_config,
            apache_config = apache_config,
            ram_used_mb   = ram,
            disk_used_gb  = disk
        )
        containers.append(container)

    return containers


# ─── Point d'entrée test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Scanner LXC ===\n")
    print("Scan des containers en cours...\n")

    try:
        containers = scanner_containers()

        print(f"\n=== Résultat : {len(containers)} containers ===")
        for c in containers:
            print(f"\n{'─'*40}")
            print(f"  Container : {c.name}")
            print(f"  IP        : {c.ip}")
            print(f"  État      : {c.state}")
            print(f"  Services  : {c.services}")
            print(f"  Paquets   : {c.packages}")
            print(f"  RAM       : {c.ram_used_mb} MB")
            print(f"  Disk      : {c.disk_used_gb} GB")
            if c.databases:
                print(f"  Bases     : {[d.name for d in c.databases]}")
            if c.db_users:
                print(f"  DB Users  : {[(u.user, u.host) for u in c.db_users]}")
            if c.ftp_users:
                print(f"  FTP Users : {[(u.username, u.home) for u in c.ftp_users]}")
            if c.nfs_exports:
                print(f"  Exports   : {[(e.path, e.subnet) for e in c.nfs_exports]}")
            if c.vsftpd_config:
                print(f"  VSFTPD    : pasv {c.vsftpd_config.pasv_min_port}"
                      f"-{c.vsftpd_config.pasv_max_port}")
            if c.backup_config:
                print(f"  Backup    : host={c.backup_config.host}"
                      f" db={c.backup_config.database}"
                      f" dest={c.backup_config.destination}")
            if c.apache_config:
                print(f"  Apache    : IPs trouvées = {c.apache_config.ips_trouvees}")
        print(f"\n{'─'*40}")

    except Exception as e:
        print(f"Erreur : {e}")
