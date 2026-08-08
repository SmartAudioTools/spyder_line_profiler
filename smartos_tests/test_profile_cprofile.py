# -*- coding: utf-8 -*-
"""Coloration cProfile de tout le code : formule de chaleur log/plancher, lecture .prof, defs.

CE QUE CE TEST COUVRE
  - log_heat : LA formule, le defaut invisible de ce chantier (une erreur y colore faux sans que
    rien ne le signale). Bornes (plancher, max, hors bornes), point milieu en echelle log,
    monotonie, cas degeneres sans exception. Teste EN PREMIER, avant tout branchement Spyder.
  - read_prof : aller-retour sur un VRAI .prof produit par cProfile (+ absent).
  - def_values_for_file : valeur (cumtime) a poser sur la ligne du `def` de chaque fonction du
    fichier ; la ligne du `def` vient directement de cProfile (aucun source lu) ; autre fichier
    et fonctions C ignores.

Modele : on colore la ligne du `def` avec le CUMTIME BRUT, normalise sur total_tt (contrainte de
coherence : la ligne "appeler B" d'une fonction marquee = cumtime de B = def de B). Cf. l'en-tete
de profile_cprofile.py et TODO - Spyder - line profiler.txt.

Module pur (pstats, math ; ni Qt ni Spyder) : tourne avec n'importe quel interpreteur.

Lancement :
    python tests/test_profile_cprofile.py
"""

import cProfile
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profile_cprofile as pc  # noqa: E402


# ---- log_heat : le point delicat
# -----------------------------------------------------------------------------
def test_log_heat_bornes():
    assert pc.log_heat(0, 100, 1) == 0.0        # valeur nulle -> froid
    assert pc.log_heat(0.5, 100, 1) == 0.0      # sous le plancher -> froid
    assert pc.log_heat(1, 100, 1) == 0.0        # egal au plancher -> froid
    assert pc.log_heat(100, 100, 1) == 1.0      # egal au max (total_tt) -> chaud
    assert pc.log_heat(200, 100, 1) == 1.0      # au-dela du max -> clampe a 1


def test_log_heat_milieu_log():
    # En log, le milieu de [1, 100] est la moyenne GEOMETRIQUE = 10, pas 50.
    assert abs(pc.log_heat(10, 100, 1) - 0.5) < 1e-12, pc.log_heat(10, 100, 1)
    assert pc.log_heat(50, 100, 1) > 0.8        # 50 (milieu lineaire) est deja tres chaud en log


def test_log_heat_monotone():
    a, b, c = pc.log_heat(2, 100, 1), pc.log_heat(20, 100, 1), pc.log_heat(90, 100, 1)
    assert 0 < a < b < c < 1, (a, b, c)


def test_log_heat_degenere_sans_exception():
    assert pc.log_heat(50, 1, 1) == 0.0         # max == plancher
    assert pc.log_heat(50, 100, 0) == 0.0       # plancher nul
    assert pc.log_heat(50, 100, -3) == 0.0      # plancher negatif


# ---- def_values_for_file
# -----------------------------------------------------------------------------
def test_def_values_du_fichier():
    fichier = "/proj/maths.py"
    stats = {
        (fichier, 1, 'travaille'): (8.0, 5.0, 3),          # def ligne 1
        (fichier, 9, 'methode'): (6.0, 6.0, 1),            # def ligne 9
        (fichier, 1, '<module>'): (50.0, 1.0, 1),          # code de premier niveau -> IGNORE
        (fichier, 4, '<lambda>'): (7.0, 7.0, 1),           # lambda -> IGNORE (commence par '<')
        (fichier, 5, '<listcomp>'): (7.0, 7.0, 1),         # comprehension -> IGNORE
        ("/proj/AUTRE.py", 1, 'travaille'): (99.0, 99.0, 1),   # autre fichier -> ignore
        ("", 0, 'built-in'): (3.0, 3.0, 1),                # fonction C, pas de fichier -> ignore
    }
    vals = pc.def_values_for_file(fichier, stats)
    # ligne 1 = travaille (le <module> a la meme ligne est ecarte, pas de collision).
    assert set(vals) == {1, 9}, set(vals)
    assert vals[1]['function'] == 'travaille', vals[1]
    assert vals[1] == {'cumtime': 8.0, 'tottime': 5.0, 'ncalls': 3, 'function': 'travaille'}
    assert vals[9]['cumtime'] == 6.0 and vals[9]['function'] == 'methode'


def test_def_values_fichier_sans_fonction():
    stats = {("/proj/maths.py", 1, 'f'): (8.0, 8.0, 1)}
    assert pc.def_values_for_file("/proj/vide.py", stats) == {}


# ---- user_reference : haut de l'echelle = code utilisateur, PAS la machinerie de profilage
# -----------------------------------------------------------------------------
def test_user_reference_ignore_machinerie():
    stats = {
        ("/proj/main.py", 1, '<module>'): (0.010, 0.001, 1),         # <module> (programme) -> EXCLU
        ("/proj/main.py", 5, 'travaille'): (0.008, 0.008, 1),        # FONCTION : 8 ms
        ("/proj/main.py", 9, 'legere'): (0.003, 0.003, 1),           # FONCTION : 3 ms
        ("~", 0, '<built-in method builtins.exec>'): (9.0, 0.3, 1),   # NATIF (fichier "~") -> ignore
        ("/tmp/x/kernprof-eager-preimports-abc.py", 1, '<module>'): (5.0, 5.0, 1),  # kernprof
        ("/usr/lib/python3.12/importlib/_bootstrap.py", 1, '_f'): (3.0, 3.0, 1),    # stdlib
        ("/venv/lib/python3.12/site-packages/foo.py", 1, 'g'): (2.0, 2.0, 1),       # site-packages
        ("<frozen importlib._bootstrap>", 1, '_f'): (4.0, 4.0, 1),   # pseudo-fichier -> ignore
    }
    # Le sommet = la FONCTION la plus chaude (travaille, 0.008), PAS le <module> (0.010, exclu) ni
    # les 9/5/4/3/2 de la machinerie/librairies.
    assert abs(pc.user_reference(stats) - 0.008) < 1e-9, pc.user_reference(stats)
    assert pc.user_reference({}) == 0.0
    # rien que du <module>/machinerie -> 0 (repli lineaire du line profiler)
    assert pc.user_reference({("/proj/x.py", 1, '<module>'): (9.0, 9.0, 1)}) == 0.0
    assert pc.user_reference({("/tmp/kernprof-eager.py", 1, 'x'): (9.0, 9.0, 1)}) == 0.0


# ---- read_prof : vrai .prof
# -----------------------------------------------------------------------------
def _charge():
    total = 0
    for i in range(20000):
        total += i * i
    return total


def test_read_prof_vrai_fichier():
    fd, path = tempfile.mkstemp(suffix='.prof')
    os.close(fd)
    try:
        prof = cProfile.Profile()
        prof.enable()
        _charge()
        prof.disable()
        prof.dump_stats(path)

        stats, total = pc.read_prof(path)
        assert total > 0, total
        found = [v for k, v in stats.items() if k[2] == '_charge']
        assert found, list(stats)[:5]
        cumtime, tottime, ncalls = found[0]
        assert cumtime > 0 and ncalls >= 1, found[0]
    finally:
        os.remove(path)


def test_read_prof_absent():
    stats, total = pc.read_prof("/inexistant/pas.prof")
    assert stats == {} and total == 0.0


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} tests passes.")


if __name__ == '__main__':
    main()
