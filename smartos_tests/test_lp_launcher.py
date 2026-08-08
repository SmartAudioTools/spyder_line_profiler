# -*- coding: utf-8 -*-
"""Tests du lanceur de profilage lp_launcher.py (celui qui remplace kernprof).

Module PUR (subprocess + stdlib) : aucune dependance Qt ni Spyder dans CE fichier ; le
lanceur teste est la copie INSTALLEE dans site-packages (comme le reste de la suite), lancee
par le meme interpreteur que celui de Spyder - exactement ce que fait widgets.py.

Ce que la suite verrouille, dans l'ordre des defauts qui ont motive la refonte (cf.
TODO - Spyder - line profiler.txt, mesures du 24/07/2026) :
  - PLUSIEURS fonctions marquees sont TOUTES mesurees, y compris les cas durs mesures :
    deux corps au bytecode strictement identique, une fonction sous @functools.lru_cache ;
  - le script ne s'execute qu'UNE fois (kernprof le rejouait deux fois : pre-import
    des cibles pointees, puis __main__) ;
  - lancer un module d'un paquet PAR SON FICHIER mesure ses fonctions marquees (la
    divergence __main__ de kernprof les perdait) ;
  - le profilage est CHIRURGICAL : evenements de line-tracing absents du GLOBAL, poses en
    LOCAL sur les seules fonctions enregistrees - vrai avec le module line_profiler recompile
    (armement local natif) COMME avec le module stock (re-scoping de repli du lanceur) ;
  - un script qui plante laisse quand meme ses mesures, et le code de sortie remonte ;
  - sys.argv, __name__ == '__main__' et le @profile nu (compat kernprof -b) fonctionnent.
"""

import json
import os
import os.path as osp
import pickle
import pstats
import subprocess
import sys
import tempfile

# Le lanceur INSTALLE (site-packages), comme le charge widgets.py.
from spyder_line_profiler.spyder import lp_launcher

LANCEUR = lp_launcher.__file__

CORPS_BOUCLE = """    total = 0
    for i in range(n):
        total += i * i
    return total
"""


def hits_boucle(n):
    """Hits attendus pour CORPS_BOUCLE appele une fois avec `n` : init + (n+1) tours de for
    + n corps + return."""
    return 2 * n + 3


def lancer(dossier, script, targets=None, all_user=False, racine=None, args=(),
           avec_prof=True):
    """Execute le lanceur sur `script` ; renvoie (code retour, stdout, timings, chemin .prof).

    `targets` : {chemin: [lignes]} ; `timings` : {nom de fonction: hits cumules}.
    """
    config = osp.join(dossier, 'config.json')
    with open(config, 'w', encoding='utf-8') as flux:
        json.dump({'targets': targets or {}, 'all_user': all_user, 'racine': racine}, flux)
    lprof = osp.join(dossier, 'sortie.lprof')
    prof = osp.join(dossier, 'sortie.prof')
    commande = [sys.executable, '-X', 'utf8', LANCEUR, '--lprof', lprof, '--config', config]
    if avec_prof:
        commande += ['--prof', prof]
    commande += [script] + list(args)
    resultat = subprocess.run(commande, capture_output=True, text=True, timeout=120)
    timings = {}
    if osp.isfile(lprof):
        with open(lprof, 'rb') as flux:
            stats = pickle.load(flux)
        for (_fichier, _ligne, nom), lignes in stats.timings.items():
            timings[nom] = timings.get(nom, 0) + sum(h for _l, h, _t in lignes)
    return resultat, timings, (prof if avec_prof and osp.isfile(prof) else None)


def test_cas_durs_tous_mesures_en_une_seule_execution():
    # Le coeur de la refonte : bytecodes identiques, @lru_cache, non-marquee absente, et le
    # temoin d'execution unique (une ligne ajoutee au fichier temoin PAR execution).
    with tempfile.TemporaryDirectory() as dossier:
        temoin = osp.join(dossier, 'temoin.txt')
        script = osp.join(dossier, 'cas_durs.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write(f"""import functools
def lente(n):                   # ligne 2, marquee
{CORPS_BOUCLE}
def jumelle(n):                 # ligne 8, marquee, bytecode IDENTIQUE a lente
{CORPS_BOUCLE}
@functools.lru_cache            # ligne 14
def memo(n):                    # ligne 15, marquee via une ligne de son corps (la 16)
    s = 0
    for i in range(n):
        s += i
    return s
def non_marquee(n):
    return sum(range(n))
lente(300); jumelle(3000); memo(1000); non_marquee(50)
with open({temoin!r}, 'a') as flux:
    flux.write('execution\\n')
""")
        resultat, timings, prof = lancer(
            dossier, script, targets={script: [2, 8, 16]})
        assert resultat.returncode == 0, resultat.stderr
        attendu = {'lente': hits_boucle(300), 'jumelle': hits_boucle(3000),
                   'memo': hits_boucle(1000)}
        assert timings == attendu, (timings, attendu)
        with open(temoin, 'r', encoding='utf-8') as flux:
            executions = flux.readlines()
        assert len(executions) == 1, f"script execute {len(executions)} fois"
        # Le .prof cProfile est lisible et ne porte que l'execution du script.
        assert prof is not None
        pstats.Stats(prof)


def test_module_de_paquet_lance_par_son_fichier():
    # Le defaut d'origine ("plusieurs fonctions marquees, une seule mesuree") : pkg/maths.py
    # marque puis LANCE directement. kernprof perdait tout ou partie (divergence __main__).
    with tempfile.TemporaryDirectory() as dossier:
        pkg = osp.join(dossier, 'pkg')
        os.makedirs(pkg)
        with open(osp.join(pkg, '__init__.py'), 'w', encoding='utf-8'):
            pass
        script = osp.join(pkg, 'maths.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write(f"""def lente(n):
{CORPS_BOUCLE}
def rapide(n):
{CORPS_BOUCLE}
print(rapide(300), lente(3000))
""")
        resultat, timings, _prof = lancer(dossier, script, targets={script: [1, 7]})
        assert resultat.returncode == 0, resultat.stderr
        attendu = {'lente': hits_boucle(3000), 'rapide': hits_boucle(300)}
        assert timings == attendu, (timings, attendu)


def test_module_importe_marque_et_script_non_marque():
    # Les marqueurs sont dans un module ; le script qui l'appelle n'en porte aucun : seules
    # les fonctions marquees du module sont mesurees, rien du script.
    with tempfile.TemporaryDirectory() as dossier:
        pkg = osp.join(dossier, 'pkg')
        os.makedirs(pkg)
        with open(osp.join(pkg, '__init__.py'), 'w', encoding='utf-8'):
            pass
        module = osp.join(pkg, 'm.py')
        with open(module, 'w', encoding='utf-8') as flux:
            flux.write(f"""def visee(n):
{CORPS_BOUCLE}
def ignoree(n):
    return n
""")
        script = osp.join(dossier, 'run.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write("from pkg.m import visee, ignoree\n"
                       "def locale(n):\n    return n\n"
                       "print(visee(400), ignoree(1), locale(1))\n")
        resultat, timings, _prof = lancer(dossier, script, targets={module: [1]})
        assert resultat.returncode == 0, resultat.stderr
        assert timings == {'visee': hits_boucle(400)}, timings


def test_profilage_chirurgical_et_environnement_du_script():
    # Le script lui-meme constate : line-tracing ABSENT du global, present en LOCAL sur la
    # marquee, absent du temoin ; __name__ == '__main__' ; sys.argv = [script] + args.
    # Vrai avec le module line_profiler recompile COMME avec le stock (re-scoping du lanceur).
    with tempfile.TemporaryDirectory() as dossier:
        sortie = osp.join(dossier, 'etat.json')
        script = osp.join(dossier, 'etat.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write(f"""import json, sys
def marquee(n):
{CORPS_BOUCLE}
def temoin(n):
    return n
marquee(100); temoin(1)
mon = sys.monitoring
line_tracing = mon.events.LINE | mon.events.PY_RETURN | mon.events.PY_YIELD
etat = dict(
    nom=__name__, argv=sys.argv[1:],
    global_line=bool(mon.get_events(mon.PROFILER_ID) & line_tracing),
    marquee_locale=bool(mon.get_local_events(mon.PROFILER_ID, marquee.__code__)
                        & mon.events.LINE),
    temoin_local=bool(mon.get_local_events(mon.PROFILER_ID, temoin.__code__)),
)
with open({sortie!r}, 'w') as flux:
    json.dump(etat, flux)
""")
        resultat, timings, _prof = lancer(
            dossier, script, targets={script: [2]}, args=('alpha', '--beta'),
            avec_prof=False)      # sans cProfile : son outil sys.monitoring fausserait l'etat
        assert resultat.returncode == 0, resultat.stderr
        with open(sortie, 'r', encoding='utf-8') as flux:
            etat = json.load(flux)
        assert etat == {'nom': '__main__', 'argv': ['alpha', '--beta'],
                        'global_line': False, 'marquee_locale': True,
                        'temoin_local': False}, etat
        assert timings == {'marquee': hits_boucle(100)}, timings


def test_mode_tout_le_code_utilisateur():
    # all_user : toutes les fonctions du script ET des modules importes sous la racine,
    # paresseusement (un module non importe n'est jamais charge - pas d'effet de bord).
    with tempfile.TemporaryDirectory() as dossier:
        pkg = osp.join(dossier, 'pkg')
        os.makedirs(pkg)
        with open(osp.join(pkg, '__init__.py'), 'w', encoding='utf-8'):
            pass
        with open(osp.join(pkg, 'm.py'), 'w', encoding='utf-8') as flux:
            flux.write(f"def importee(n):\n{CORPS_BOUCLE}")
        with open(osp.join(pkg, 'danger.py'), 'w', encoding='utf-8') as flux:
            flux.write("raise RuntimeError('un module non importe ne doit JAMAIS etre "
                       "charge')\n")
        script = osp.join(dossier, 'run.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write("from pkg.m import importee\n"
                       "def du_script(n):\n    return n\n"
                       "print(importee(200), du_script(1))\n")
        resultat, timings, _prof = lancer(
            dossier, script, all_user=True, racine=dossier)
        assert resultat.returncode == 0, resultat.stderr
        assert timings.get('importee') == hits_boucle(200), timings
        assert 'du_script' in timings, timings


def test_cprofile_attribue_correctement_les_temps():
    # Defaut signale par l'utilisateur le 24/07/2026 : "marquer lente ralentit
    # considerablement rapide" - en realite line_profiler VOLAIT le rappel PY_RETURN de
    # cProfile (meme outil sys.monitoring en 3.12), qui ne voyait plus aucune fin de
    # fonction : lente disparaissait des stats et son temps line-profile etait impute au
    # CUMTIME de rapide, non marquee. Correctif : wrap_trace=True (retransmission des
    # evenements voles). Ce test verrouille l'attribution : rapide n'appelle rien, donc son
    # cumtime doit EGALER son tottime (les enfants fantomes le feraient exploser), et lente
    # doit apparaitre dans le .prof en portant elle-meme son cout.
    with tempfile.TemporaryDirectory() as dossier:
        script = osp.join(dossier, 'attribution.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write(f"""def lente(n):
{CORPS_BOUCLE}
def rapide(n):
{CORPS_BOUCLE}
print(rapide(3000), lente(30000))
""")
        resultat, timings, prof = lancer(dossier, script, targets={script: [1]})
        assert resultat.returncode == 0, resultat.stderr
        assert timings == {'lente': hits_boucle(30000)}, timings
        stats = pstats.Stats(prof).stats
        par_nom = {cle[2]: valeurs for cle, valeurs in stats.items()}
        assert 'lente' in par_nom, sorted(par_nom)
        assert 'rapide' in par_nom, sorted(par_nom)
        _cc, _nc, tottime, cumtime, _appelants = par_nom['rapide']
        # Sans appel dans son corps, cumtime == tottime ; la moindre "fille fantome"
        # (la lente line-profilee, des millisecondes) ferait exploser le rapport.
        assert cumtime <= tottime * 1.5 + 1e-6, (tottime, cumtime)


def test_script_qui_plante_laisse_ses_mesures():
    with tempfile.TemporaryDirectory() as dossier:
        script = osp.join(dossier, 'plante.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write(f"""def boum(n):
{CORPS_BOUCLE}
boum(50)
raise RuntimeError('boum')
""")
        resultat, timings, _prof = lancer(dossier, script, targets={script: [1]})
        assert resultat.returncode == 1, resultat.returncode
        assert 'RuntimeError: boum' in resultat.stderr, resultat.stderr
        assert timings == {'boum': hits_boucle(50)}, timings


def test_profile_nu_compatible_kernprof():
    # kernprof -b posait `profile` dans les builtins ; le lanceur maintient cette
    # compatibilite pour les scripts ecrits avec un @profile nu.
    with tempfile.TemporaryDirectory() as dossier:
        script = osp.join(dossier, 'nu.py')
        with open(script, 'w', encoding='utf-8') as flux:
            flux.write(f"""@profile
def decoree(n):
{CORPS_BOUCLE}
decoree(150)
""")
        resultat, timings, _prof = lancer(dossier, script)      # aucun marqueur
        assert resultat.returncode == 0, resultat.stderr
        assert timings == {'decoree': hits_boucle(150)}, timings


def main():
    tests = [(nom, objet) for nom, objet in sorted(globals().items())
             if nom.startswith('test_') and callable(objet)]
    echecs = []
    for nom, test in tests:
        try:
            test()
        except Exception as erreur:
            echecs.append((nom, erreur))
            print(f"ECHEC  {nom}\n       {type(erreur).__name__}: {erreur}")
        else:
            print(f"ok     {nom}")
    print(f"\n{len(tests) - len(echecs)}/{len(tests)} tests passent")
    return 1 if echecs else 0


if __name__ == '__main__':
    sys.exit(main())
