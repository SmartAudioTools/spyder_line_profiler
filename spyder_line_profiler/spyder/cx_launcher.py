# -*- coding: utf-8 -*-
"""Lanceur du mode « Σ » : compte les operations elementaires du script (complexity_profiler).

Demande de l'utilisateur du 30/10/2026 (relayee par la session SmartTeacher) : un second mode du
greffon Line Profiler, qui compte au lieu de chronometrer, sur les MEMES fonctions marquees par
l'horloge. Lance par widgets.py dans un processus a part, avec l'interpreteur de l'utilisateur,
comme lp_launcher.py dont il reprend la configuration (fichier JSON des cibles), l'environnement
d'execution (sys.argv, sys.path[0], vrai module __main__) et l'arret gracieux (SIGTERM).

Deux lancements separes plutot qu'une mesure combinee (decision de l'utilisateur) : le comptage
ralentit le programme de x4 a x9, avec une variation de 24 a 78 % d'un essai a l'autre - on ne peut
donc pas le retrancher d'un temps mesure en meme temps.

ECRIT (--ops) un JSON :
    {"fichier": script, "source": texte execute, "portee": [lignes], "lignes": {...},
     "caches": [...], "total": n, "erreur": str|None, "hors_script": [fichiers marques non comptes],
     "fonctions": {ligne du def: [appels, operations]}}
`lignes` et `caches` sont ceux de Resultats.vers_dict(), DEJA FILTRES sur la portee : seul le corps
des fonctions marquees (ou de toutes les fonctions en mode « tout le code utilisateur ») est garde.
`source` permet a l'editeur de savoir si le code a change depuis (cf. profile_results).

LIMITES, celles du moteur (signalees a son auteur, la session SmartTeacher) :
  - le moteur instrumente TOUT le script : le ralentissement porte sur l'ensemble, le filtrage ne
    fait que choisir ce qu'on affiche ;
  - il ne compte que le fichier lance : une fonction marquee dans un module IMPORTE n'est pas
    comptee, et son appel vaut 1 operation (listee dans `hors_script`, affichee dans le panneau).
"""

import ast
import json
import os.path as osp
import sys
import traceback
import types

# Dossier de ce lanceur en tete de sys.path au lancement : lp_launcher (stdlib seule) s'importe
# donc directement. On en reprend les briques au lieu de les dupliquer.
from lp_launcher import (_armer_arret_gracieux, _charger_config, _defs_a_decorer,
                         _installer_pont_pyxel, _sous_racine_utilisateur)


def _parse_args(argv):
    """(options, script, args du script), meme convention que lp_launcher._parse_args."""
    options = {'--ops': None, '--config': None,
               '--pyxel-shm': None, '--pyxel-bridge-path': None}
    index = 0
    while index < len(argv) and argv[index] in options:
        if index + 1 >= len(argv):
            raise SystemExit(f"cx_launcher : valeur manquante pour {argv[index]}")
        options[argv[index]] = argv[index + 1]
        index += 2
    if index >= len(argv):
        raise SystemExit("cx_launcher : script a compter manquant")
    if not options['--ops'] or not options['--config']:
        raise SystemExit("cx_launcher : --ops et --config sont obligatoires")
    return options, argv[index], argv[index + 1:]


def portee(source, lignes_marquees):
    """Lignes du CORPS des fonctions retenues (marquees, ou toutes si `lignes_marquees` is None).

    Memes fonctions que celles que le line profiler decore (lp_launcher._defs_a_decorer) : la
    portee des deux modes est la meme. La ligne du `def` (et ses decorateurs) en est exclue : elle
    porte deja le Σ cumule de cProfile dans la marge, et n'est executee qu'a la definition.
    """
    tree = ast.parse(source)
    lignes = set()
    for noeud in _defs_a_decorer(tree, lignes_marquees):
        lignes.update(range(noeud.body[0].lineno, noeud.end_lineno + 1))
    return lignes


def corps_propres(source, lignes_marquees):
    """{noeud def retenu: lignes de son corps, SANS le corps des fonctions imbriquees}.

    Une fonction imbriquee est comptee deux fois sinon : l'appel qui la lance porte deja son cout
    (le moteur compte INCLUSIF l'appel d'une fonction du script), et ses lignes ont leurs propres
    comptes. La ligne de son `def` reste : la definir est une operation de la fonction englobante.
    """
    tree = ast.parse(source)
    corps = {}
    for noeud in _defs_a_decorer(tree, lignes_marquees):
        lignes = set(range(noeud.body[0].lineno, noeud.end_lineno + 1))
        for interne in ast.walk(noeud):
            if interne is not noeud and isinstance(
                    interne, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lignes -= set(range(interne.body[0].lineno, interne.end_lineno + 1))
        corps[noeud] = lignes
    return corps


def compter_appels(script, noeuds):
    """Compte les appels des fonctions `noeuds` du `script` pendant l'execution.

    Le moteur ne donne pas de total par fonction, et complexity_profiler n'est pas a nous (il
    appartient a la session SmartTeacher) : on compte donc ici, par sys.monitoring (PY_START, qui
    part au DEBUT d'un appel et non a la reprise d'un generateur). Un code d'une autre fonction est
    desactive des son premier appel (DISABLE) : le surcout ne porte que sur les fonctions retenues.
    Rend (appels, arreter) : {ligne du def: nombre d'appels}, et la fonction qui rend l'outil.
    Sans sys.monitoring (Python < 3.12) ou sans identifiant libre, appels reste None.
    """
    moniteur = getattr(sys, 'monitoring', None)
    if moniteur is None or not noeuds:
        return None, lambda: None
    outil = next((i for i in range(6) if moniteur.get_tool(i) is None), None)
    if outil is None:
        return None, lambda: None
    # co_firstlineno vaut la ligne du premier DECORATEUR s'il y en a (et le nom distingue une
    # lambda posee sur cette meme ligne).
    cles = {(min([n.lineno] + [d.lineno for d in n.decorator_list]), n.name): n.lineno
            for n in noeuds}
    appels = dict.fromkeys(cles.values(), 0)

    def debut(code, _offset):
        ligne = (cles.get((code.co_firstlineno, code.co_name))
                 if code.co_filename == script else None)
        if ligne is None:
            return moniteur.DISABLE
        appels[ligne] += 1

    moniteur.use_tool_id(outil, 'cx_launcher')
    moniteur.register_callback(outil, moniteur.events.PY_START, debut)
    moniteur.set_events(outil, moniteur.events.PY_START)

    def arreter():
        moniteur.set_events(outil, 0)
        moniteur.register_callback(outil, moniteur.events.PY_START, None)
        moniteur.free_tool_id(outil)
    return appels, arreter


def totaux_fonctions(corps, appels, lignes):
    """{str(ligne du def): [appels, operations]} des fonctions appelees au moins une fois.
    Operations = somme des comptes des lignes de son corps propre (cf. corps_propres).

    LIMITE : une recursion CROISEE (f appelle g qui rappelle f) compte deux fois le cout de g dans
    f - l'appel de g depuis f est inclusif, et les lignes de f atteintes depuis g s'y ajoutent.
    """
    if appels is None:
        return {}
    totaux = {}
    for noeud, propres in corps.items():
        n = appels.get(noeud.lineno, 0)
        if n:
            ops = sum(lignes[str(l)][1] for l in propres if str(l) in lignes)
            totaux[str(noeud.lineno)] = [n, ops]
    return totaux


def filtrer(donnees, lignes):
    """Garde de vers_dict() les seules lignes et instructions cachees de la portee."""
    return {
        'lignes': {n: v for n, v in donnees['lignes'].items() if int(n) in lignes},
        'caches': [c for c in donnees['caches'] if c[0] in lignes],
        'total': donnees['total'],
        'erreur': donnees['erreur'],
    }


def main(argv):
    _armer_arret_gracieux()
    options, script, args_script = _parse_args(argv)
    script = osp.abspath(script)
    cibles, racine = _charger_config(options['--config'])

    # Import ICI et non en tete : le message d'absence doit sortir proprement dans le panneau.
    try:
        from complexity_profiler.noyau import profiler
    except ImportError as erreur:
        print("Le comptage des operations elementaires demande le paquet complexity_profiler "
              f"dans l'interpreteur utilise ({sys.executable}) : {erreur}", file=sys.stderr)
        return 1

    with open(script, 'r', encoding='utf-8') as flux:
        source = flux.read()

    chemin_script = osp.normcase(script)
    if racine is not None:
        lignes_marquees = None if _sous_racine_utilisateur(chemin_script, racine) else set()
    else:
        lignes_marquees = cibles.get(chemin_script, set())
    lignes_portee = portee(source, lignes_marquees)
    corps = corps_propres(source, lignes_marquees)
    hors_script = sorted(f for f in cibles if f != chemin_script)

    sys.argv = [script] + list(args_script)
    dossier_lanceur = osp.normcase(osp.abspath(osp.dirname(__file__)))
    if sys.path and osp.normcase(osp.abspath(sys.path[0] or '.')) == dossier_lanceur:
        sys.path[0] = osp.dirname(script)
    else:
        sys.path.insert(0, osp.dirname(script))

    if options['--pyxel-shm']:
        _installer_pont_pyxel(options['--pyxel-shm'], options['--pyxel-bridge-path'])

    # profiler() rend les comptes accumules meme sur un KeyboardInterrupt (Stop dans Spyder) ou un
    # SystemExit (moteur, commit b427675) : on ecrit les mesures partielles, comme lp_launcher.
    module_main = types.ModuleType('__main__')
    module_main.__dict__.update({'__file__': script, '__builtins__': __builtins__})
    sys.modules['__main__'] = module_main
    appels, arreter = compter_appels(script, list(corps))
    try:
        r = profiler(source, module_main.__dict__, script)
    finally:
        arreter()
    code_retour = 0
    erreur = r.erreur
    if isinstance(erreur, SystemExit):
        if isinstance(erreur.code, int):
            code_retour = erreur.code
        elif erreur.code is not None:
            print(erreur.code, file=sys.stderr)
            code_retour = 1
        erreur = None
    elif isinstance(erreur, KeyboardInterrupt):
        code_retour = 1
        erreur = None
    elif erreur is not None:
        traceback.print_exception(erreur)
        code_retour = 1
    r.erreur = erreur
    brutes = r.vers_dict()
    donnees = filtrer(brutes, lignes_portee)
    donnees.update({'fichier': script, 'source': source,
                    'fonctions': totaux_fonctions(corps, appels, brutes['lignes']),
                    'portee': sorted(lignes_portee), 'hors_script': hors_script})
    with open(options['--ops'], 'w', encoding='utf-8') as flux:
        json.dump(donnees, flux, ensure_ascii=False)
    return code_retour


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
