import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum


# ─── Phases de migration ──────────────────────────────────────────────────────

class Phase(Enum):
    SCAN         = 1
    PROVISIONING = 2
    BACKUP       = 3
    PROVISION    = 4
    TRANSFER     = 5
    RESTORE      = 6
    VALIDATE     = 7
    TESTS        = 8
    TERMINE      = 9
    ECHEC        = -1


# ─── Gestionnaire d'état ──────────────────────────────────────────────────────

class State:

    FICHIER = str(Path(__file__).parent.parent / "migration_state.json")

    def __init__(self):
        self.phase         : Phase              = Phase.SCAN
        self.debut         : str               = datetime.now().isoformat()
        self.fin           : Optional[str]      = None
        self.erreur        : Optional[str]      = None
        self.ip_mapping    : Dict[str, Dict]    = {}
        self.ressources    : Dict[str, Any]     = {}
        self.phases_ok     : list               = []

    # ─── Lecture / écriture ───────────────────────────────────────────────────

    def sauvegarder(self):
        data = {
            "phase"      : self.phase.name,
            "debut"      : self.debut,
            "fin"        : self.fin,
            "erreur"     : self.erreur,
            "ip_mapping" : self.ip_mapping,
            "ressources" : self.ressources,
            "phases_ok"  : self.phases_ok,
        }
        with open(self.FICHIER, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def charger(cls) -> "State":
        s = cls()
        if not os.path.exists(cls.FICHIER):
            return s
        with open(cls.FICHIER, "r") as f:
            data = json.load(f)
        s.phase      = Phase[data.get("phase", "SCAN")]
        s.debut      = data.get("debut", s.debut)
        s.fin        = data.get("fin")
        s.erreur     = data.get("erreur")
        s.ip_mapping = data.get("ip_mapping", {})
        s.ressources = data.get("ressources", {})
        s.phases_ok  = data.get("phases_ok", [])
        return s

    # ─── Transitions d'état ───────────────────────────────────────────────────

    def phase_terminee(self, phase: Phase):
        self.phases_ok.append(phase.name)
        self.phase = Phase(phase.value + 1)
        self.sauvegarder()
        print(f"  [ÉTAT] Phase {phase.name} terminée.")

    def marquer_echec(self, erreur: str):
        self.phase  = Phase.ECHEC
        self.erreur = erreur
        self.fin    = datetime.now().isoformat()
        self.sauvegarder()
        print(f"  [ÉCHEC] {erreur}")

    def marquer_termine(self):
        self.phase = Phase.TERMINE
        self.fin   = datetime.now().isoformat()
        self.sauvegarder()
        print("  [ÉTAT] Migration terminée avec succès.")

    # ─── Gestion des IPs ──────────────────────────────────────────────────────

    def enregistrer_ip(self, nom: str, lxc_ip: str,
                       internal_ip: str, floating_ip: str):
        self.ip_mapping[nom] = {
            "lxc_ip"      : lxc_ip,
            "internal_ip" : internal_ip,
            "floating_ip" : floating_ip,
        }
        self.sauvegarder()

    def get_floating_ip(self, nom: str) -> Optional[str]:
        return self.ip_mapping.get(nom, {}).get("floating_ip")

    def get_internal_ip(self, nom: str) -> Optional[str]:
        return self.ip_mapping.get(nom, {}).get("internal_ip")

    # ─── Gestion des ressources Terraform ────────────────────────────────────

    def enregistrer_ressource(self, cle: str, valeur: Any):
        self.ressources[cle] = valeur
        self.sauvegarder()

    # ─── Reprise ──────────────────────────────────────────────────────────────

    def peut_reprendre(self) -> bool:
        return (
            os.path.exists(self.FICHIER)
            and self.phase not in (Phase.TERMINE, Phase.ECHEC)
            and len(self.phases_ok) > 0
        )

    def afficher_resume(self):
        print(f"\n  Phase courante : {self.phase.name}")
        print(f"  Phases OK      : {self.phases_ok}")
        print(f"  IP mapping     : {self.ip_mapping}")
        if self.erreur:
            print(f"  Dernière erreur : {self.erreur}")


# ─── Point d'entrée test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Test State ===\n")

    s = State()
    s.sauvegarder()
    print("État initial sauvegardé.")

    s.phase_terminee(Phase.SCAN)
    s.enregistrer_ip(
        "mariadb",
        lxc_ip      = "10.0.3.10",
        internal_ip = "10.10.10.10",
        floating_ip = "10.0.0.101"
    )

    s2 = State.charger()
    s2.afficher_resume()

    print("\nTest reprise :", s2.peut_reprendre())

    os.remove(State.FICHIER)
    print("Fichier supprimé. Test terminé.")
