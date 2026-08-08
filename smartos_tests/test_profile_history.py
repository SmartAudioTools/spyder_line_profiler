# -*- coding: utf-8 -*-
"""Historique de line profiling PAR FONCTION : remappage, racine de depot, archivage, aller-retour.

CE QUE CE TEST COUVRE
  - remap_lines : LE point delicat (deuxieme question du TODO). Une erreur y reporterait une
    mesure sur la MAUVAISE ligne sans que rien ne le signale - defaut invisible, teste EN PREMIER.
    Quatre cas : insertion en tete, suppression au-dessus, modification de la ligne mesuree
    (-> perimee), modification AILLEURS (la ligne survit), plus l'invariant de conservation.
  - qualname_at_line / span_of_qualname : reconstruction du nom qualifie ("Calc.methode") et de
    l'etendue d'une fonction, a partir de la seule ligne du `def` (ce que donne line_profiler).
  - archive_lstats : UN run par fonction, cle par nom qualifie, lignes exprimees relativement a la
    tranche ; le nom nu de line_profiler ("methode") devient bien "Calc.methode" ; un fichier hors
    depot est ignore sans faire echouer.
  - remap_run_to_current : le pont Q4 de bout en bout - un run rejoue sur un source modifie donne
    les bonnes lignes ABSOLUES, et marque perime ce qui a change.
  - find_repo_root / entry_dir / save_function_run / load_function_runs : localisation et
    aller-retour d'ecriture ; hors depot -> None sans rien ecrire.

Le module teste ne depend NI de Qt NI de Spyder : il tourne avec n'importe quel interpreteur.
On importe la copie DU DEPOT (source de verite), pas celle de site-packages.

Lancement :
    python tests/test_profile_history.py
"""

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profile_history as ph  # noqa: E402


# Un fichier de reference, avec une fonction de premier niveau et une methode.
SOURCE = (
    "def lente():\n"            # 1
    "    x = 0\n"               # 2
    "    for i in range(3):\n"  # 3
    "        x += i\n"          # 4
    "    return x\n"            # 5
    "\n"                        # 6
    "\n"                        # 7
    "class Calc:\n"             # 8
    "    def methode(self):\n"  # 9
    "        y = 1\n"           # 10
    "        return y\n"        # 11
)


# ---- remap_lines
# -----------------------------------------------------------------------------
def test_remap_identique():
    src = "a\nb\nc\nd\n"
    mapped, stale = ph.remap_lines(src, src, {2: 'B', 4: 'D'})
    assert mapped == {2: 'B', 4: 'D'}, mapped
    assert stale == {}, stale


def test_remap_insertion_en_tete():
    old = "a\nb\nc\n"
    new = "nouvelle\na\nb\nc\n"
    mapped, stale = ph.remap_lines(old, new, {2: 'B'})
    assert mapped == {3: 'B'}, mapped
    assert stale == {}, stale


def test_remap_suppression_au_dessus():
    old = "a\nb\nc\nd\n"
    new = "b\nc\nd\n"
    mapped, stale = ph.remap_lines(old, new, {2: 'B', 4: 'D'})
    assert mapped == {1: 'B', 3: 'D'}, mapped
    assert stale == {}, stale


def test_remap_ligne_mesuree_modifiee():
    old = "a\nb\nc\n"
    new = "a\nb_modifiee\nc\n"
    mapped, stale = ph.remap_lines(old, new, {2: 'B'})
    assert mapped == {}, mapped
    assert stale == {2: 'B'}, stale


def test_remap_modification_ailleurs():
    old = "a\nb\nc\nd\n"
    new = "a\nb\nCHANGEE\nd\n"
    mapped, stale = ph.remap_lines(old, new, {2: 'B', 4: 'D'})
    assert mapped == {2: 'B', 4: 'D'}, mapped
    assert stale == {}, stale


def test_remap_invariant_conservation():
    old = "l1\nl2\nl3\nl4\nl5\n"
    new = "en-tete\nl1\nl3-modifiee\nl4\nl5\nqueue\n"
    valeurs = {1: 'A', 2: 'B', 3: 'C', 4: 'D', 5: 'E'}
    mapped, stale = ph.remap_lines(old, new, valeurs)
    assert len(mapped) + len(stale) == len(valeurs), (mapped, stale)
    assert len(set(mapped.values())) == len(mapped), mapped
    nb_lignes_new = len(new.splitlines())
    assert all(1 <= n <= nb_lignes_new for n in mapped), mapped


# ---- qualname_at_line / span_of_qualname
# -----------------------------------------------------------------------------
def test_qualname_fonction_et_methode():
    assert ph.qualname_at_line(SOURCE, 1) == ('lente', 1, 5)      # sur le def
    assert ph.qualname_at_line(SOURCE, 4) == ('lente', 1, 5)      # dans le corps
    assert ph.qualname_at_line(SOURCE, 9) == ('Calc.methode', 9, 11)
    assert ph.qualname_at_line(SOURCE, 10) == ('Calc.methode', 9, 11)
    assert ph.qualname_at_line(SOURCE, 8) == (None, None, None)   # ligne 'class', hors methode
    assert ph.qualname_at_line(SOURCE, 6) == (None, None, None)   # ligne vide


def test_span_of_qualname():
    assert ph.span_of_qualname(SOURCE, 'lente') == (1, 5)
    assert ph.span_of_qualname(SOURCE, 'Calc.methode') == (9, 11)
    assert ph.span_of_qualname(SOURCE, 'inexistante') is None


# ---- find_repo_root / entry_dir
# -----------------------------------------------------------------------------
def test_racine_depot_et_cle_relative():
    with tempfile.TemporaryDirectory() as tmp:
        racine = os.path.join(tmp, 'projet')
        sous = os.path.join(racine, 'pkg', 'sous')
        os.makedirs(sous)
        os.mkdir(os.path.join(racine, '.hg'))
        fichier = os.path.join(sous, 'maths.py')
        with open(fichier, 'w') as flux:
            flux.write("x = 1\n")

        assert ph.find_repo_root(fichier) == racine
        attendu = os.path.join(racine, '.profiler', 'pkg', 'sous', 'maths.py')
        assert ph.entry_dir(fichier) == attendu, ph.entry_dir(fichier)


def test_hors_depot_pas_historique():
    with tempfile.TemporaryDirectory() as tmp:
        fichier = os.path.join(tmp, 'isole.py')
        with open(fichier, 'w') as flux:
            flux.write("x = 1\n")
        assert ph.find_repo_root(fichier) is None
        assert ph.entry_dir(fichier) is None


# ---- save_function_run / load_function_runs
# -----------------------------------------------------------------------------
def _depot_avec_fichier(tmp, source=SOURCE, marqueur='.hg'):
    racine = os.path.join(tmp, 'projet')
    os.makedirs(racine)
    os.mkdir(os.path.join(racine, marqueur))
    fichier = os.path.join(racine, 'maths.py')
    with open(fichier, 'w') as flux:
        flux.write(source)
    return racine, fichier


def test_ecriture_relecture_fonction():
    with tempfile.TemporaryDirectory() as tmp:
        racine, fichier = _depot_avec_fichier(tmp)
        dest = ph.save_function_run(
            fichier, 'Calc.methode', def_line=9,
            source_slice="    def methode(self):\n        y = 1\n        return y",
            lines_rel=[(2, 1, 10), (3, 1, 5)], unit=1e-09,
            when=datetime(2026, 7, 22, 14, 3, 0), machine='cachyos', revision='abc+')

        assert dest is not None and os.path.isfile(dest), dest
        attendu = os.path.join(racine, '.profiler', 'maths.py', 'Calc.methode')
        assert dest.startswith(attendu), dest

        runs = ph.load_function_runs(fichier, 'Calc.methode')
        assert len(runs) == 1, runs
        run = runs[0]
        assert run['function'] == 'Calc.methode'
        assert run['machine'] == 'cachyos'
        assert run['revision'] == 'abc+'
        assert run['def_line'] == 9
        assert run['lines'] == [[2, 1, 10], [3, 1, 5]]
        assert run['format'] == 2
        # Une autre fonction n'a rien.
        assert ph.load_function_runs(fichier, 'lente') == []


# ---- archive_lstats
# -----------------------------------------------------------------------------
class _FauxLineStats:
    """Imitation minimale de line_profiler.LineStats : juste .unit et .timings.

    line_profiler ne donne que le NOM NU ('methode') dans ses clefs ; archive_lstats doit
    reconstruire 'Calc.methode' par ast. C'est precisement ce que ce faux objet permet de
    verifier sans line_profiler ni pickle.
    """

    def __init__(self, unit, timings):
        self.unit = unit
        self.timings = timings


def test_archive_un_run_par_fonction_avec_nom_qualifie():
    with tempfile.TemporaryDirectory() as tmp:
        racine, fichier = _depot_avec_fichier(tmp)
        hors = os.path.join(tmp, 'hors.py')            # hors depot -> ignore
        with open(hors, 'w') as flux:
            flux.write("def f():\n    return 1\n")

        lstats = _FauxLineStats(unit=1e-09, timings={
            (fichier, 1, 'lente'): [(2, 1, 100), (3, 3, 300), (4, 3, 300), (5, 1, 50)],
            (fichier, 9, 'methode'): [(10, 1, 10), (11, 1, 5)],   # nom nu 'methode'
            (hors, 1, 'f'): [(2, 1, 10)],
        })
        ecrits = ph.archive_lstats(
            lstats, when=datetime(2026, 7, 22, 15, 0, 0), machine='m', revision='r')
        assert len(ecrits) == 2, ecrits                # le fichier hors depot n'ecrit rien

        # 'lente' : tranche lignes 1..5, offsets relatifs = lignes absolues (start=1).
        run_lente = ph.load_function_runs(fichier, 'lente')[0]
        assert run_lente['lines'] == [[2, 1, 100], [3, 3, 300], [4, 3, 300], [5, 1, 50]]
        assert run_lente['source'].startswith('def lente():')

        # 'methode' : clef reconstruite en 'Calc.methode', tranche 9..11, offsets = ligne-8.
        assert ph.load_function_runs(fichier, 'methode') == []      # PAS sous le nom nu
        run_meth = ph.load_function_runs(fichier, 'Calc.methode')[0]
        assert run_meth['function'] == 'Calc.methode'
        assert run_meth['lines'] == [[2, 1, 10], [3, 1, 5]]         # 10->2, 11->3
        assert run_meth['def_line'] == 9

        assert sorted(ph.functions_with_history(fichier)) == ['Calc.methode', 'lente']


def test_deux_runs_tries_du_plus_recent():
    with tempfile.TemporaryDirectory() as tmp:
        racine, fichier = _depot_avec_fichier(tmp)
        for when, machine in ((datetime(2026, 7, 20, 9, 0, 0), 'm1'),
                              (datetime(2026, 7, 22, 9, 0, 0), 'm2')):
            ph.save_function_run(fichier, 'lente', 1, "def lente():\n    return 0",
                                 [(2, 1, 1)], 1e-09, when=when, machine=machine, revision='r')
        runs = ph.load_function_runs(fichier, 'lente')
        assert [r['machine'] for r in runs] == ['m2', 'm1'], runs


# ---- remap_run_to_current : le pont Q4 de bout en bout
# -----------------------------------------------------------------------------
def _run_lente(tmp):
    """Archive un run de 'lente' sur SOURCE et le renvoie."""
    racine, fichier = _depot_avec_fichier(tmp)
    lstats = _FauxLineStats(unit=1e-09, timings={
        (fichier, 1, 'lente'): [(2, 1, 100), (3, 3, 300), (4, 3, 300), (5, 1, 50)]})
    ph.archive_lstats(lstats, when=datetime(2026, 7, 22, 15, 0, 0), machine='m', revision='r')
    return ph.load_function_runs(fichier, 'lente')[0]


def test_remap_run_decalage_du_fichier():
    """Une ligne ajoutee AVANT la fonction decale ses mesures d'autant, sans rien perimer."""
    with tempfile.TemporaryDirectory() as tmp:
        run = _run_lente(tmp)
        nouveau = "import os\n" + SOURCE          # 'lente' passe des lignes 1..5 a 2..6
        mapped, stale = ph.remap_run_to_current(run, nouveau)
        # Mesures d'origine sur lignes absolues 2,3,4,5 -> decalees a 3,4,5,6.
        assert sorted(mapped) == [3, 4, 5, 6], mapped
        assert stale == {}, stale


def test_remap_run_ligne_interne_modifiee():
    """Modifier une ligne DU corps la rend perimee ; les autres suivent."""
    with tempfile.TemporaryDirectory() as tmp:
        run = _run_lente(tmp)
        nouveau = SOURCE.replace("    x = 0\n", "    x = 999\n")   # ligne 2 changee
        mapped, stale = ph.remap_run_to_current(run, nouveau)
        assert 2 not in mapped                    # 'x = 0' n'est reportee nulle part
        assert sorted(mapped) == [3, 4, 5], mapped
        assert list(stale) == [2], stale          # offset 2 dans la tranche = ligne 'x = 0'


def test_remap_run_fonction_disparue():
    """La fonction n'existe plus dans le source courant : tout est perime."""
    with tempfile.TemporaryDirectory() as tmp:
        run = _run_lente(tmp)
        mapped, stale = ph.remap_run_to_current(run, "def autre():\n    return 1\n")
        assert mapped == {}, mapped
        assert len(stale) == 4, stale


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} tests passes.")


if __name__ == '__main__':
    main()
