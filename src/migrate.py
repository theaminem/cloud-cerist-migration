#!/usr/bin/env python3
"""
Point d'entrée unique de la migration LXC → OpenStack.
Usage : python3 migrate.py
"""

import sys
import os

# Ajoute le répertoire src au path Python
sys.path.insert(0, os.path.dirname(__file__))

from orchestrator import main

if __name__ == "__main__":
    main()
