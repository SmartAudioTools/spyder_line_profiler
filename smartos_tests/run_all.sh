#!/bin/bash
# Lance les tests des marqueurs de profilage, sans serveur d'affichage.
#
# Qt tourne en QT_QPA_PLATFORM=offscreen : la suite passe donc aussi bien dans une session
# graphique qu'en ligne de commande sur une machine sans ecran.
#
# Le module teste est celui INSTALLE dans site-packages (spyder_line_profiler.spyder
# .profile_targets), pas la copie du depot : c'est ce que Spyder charge reellement. Rejouer
# Commun/scripts_installation/spyder_patch/patch_spyder_line_profiler_targets.py avant de tester si le depot a bouge.
#
# ⚠ Les tests remplacent CONF par un dictionnaire en memoire : ils ne lisent ni n'ecrivent
# JAMAIS la configuration reelle de Spyder, et peuvent donc tourner pendant que Spyder est
# ouvert sans lui effacer ses marqueurs.
set -e
cd "$(dirname "$0")/.."

PYTHON="${SPYDER_LINE_PROFILER_PYTHON:-/DATA/Python/SmartPython/CachyOS/versions/SmartPythonEditor/bin/python}"

export QT_QPA_PLATFORM=offscreen

echo "=== Marqueurs de profilage : clic, survol, persistance, config du lanceur ==="
# Les avertissements "Mouse event MouseRelease not accepted by receiving widget" viennent de
# QTest en offscreen (la marge ne consomme que le press) et sont sans consequence.
"$PYTHON" tests/test_profile_targets.py 2>&1 | grep -v "Mouse event"

echo
echo "=== Resultats dans l'editeur : lecture du pickle, fonds colores, marge de droite ==="
"$PYTHON" tests/test_profile_results.py

echo
echo "=== Historique : remappage sur code modifie, racine de depot, ecriture/relecture ==="
# Module pur (ni Qt ni Spyder) : tourne avec n'importe quel interpreteur, sans serveur d'affichage.
"$PYTHON" tests/test_profile_history.py

echo
echo "=== Coloration cProfile : formule log/plancher, etalement a plat, lecture .prof ==="
# Module pur (pstats, ast, math ; ni Qt ni Spyder).
"$PYTHON" tests/test_profile_cprofile.py

echo
echo "=== Lanceur lp_launcher : cas durs, execution unique, chirurgie, plantage, all-user ==="
# Vrais runs en sous-processus du lanceur INSTALLE (celui que widgets.py execute).
"$PYTHON" tests/test_lp_launcher.py
