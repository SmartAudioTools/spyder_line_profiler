# -*- coding: utf-8 -*-
"""Lanceur de profilage du plugin line-profiler : remplace kernprof.

Installe par Commun/scripts_installation/spyder_patch/patch_spyder_line_profiler_targets.py dans
site-packages/spyder_line_profiler/spyder/lp_launcher.py, et lance par widgets.py (patch) :

    python -X utf8 lp_launcher.py --lprof <sortie.lprof> [--prof <sortie.prof>]
                                  --config <config.json>
                                  [--pyxel-shm <segment> --pyxel-bridge-path <dossier>]
                                  <script> [args du script...]

Ce module est AUTONOME : il n'importe que la stdlib et line_profiler, jamais Spyder ni Qt.
Il tourne dans l'interpreteur du PROJET (main_interpreter), pas dans celui de Spyder - la
seule exigence est que line_profiler y soit installe, exactement comme kernprof avant lui.

POURQUOI un lanceur a nous plutot que kernprof (TODO - Spyder - line profiler.txt, refonte
validee sur les cas durs, mesures du 24/07/2026) :

  - kernprof ENREGISTRE les cibles pointees ("-p pkg.maths.lente") apres avoir IMPORTE le
    module : l'execution du code de premier niveau a deja eu lieu, puis le script est rejoue
    sous __main__ avec des objets-code recompiles, DISTINCTS de ceux enregistres. Double
    execution des effets de bord, et divergence __main__ : les appels du vrai run echappent a
    l'instrumentation. Qu'une fonction s'en sortait quand meme etait un ACCIDENT de
    l'armement GLOBAL (attribution par hash de bytecode) - accident que l'armement LOCAL
    (module du fork SmartOS, cf. requirements_Spyder-*.txt) supprime : avec kernprof et
    l'armement local, plus RIEN n'etait mesure dans ce flux (mesure le 24/07/2026).
  - Ici, c'est NOUS qui compilons le code : le decorateur est ajoute sur les seuls `def`
    marques, l'enregistrement se fait au moment ou le `def` s'execute, l'armement au meme
    instant, et le script ne s'execute qu'UNE fois. Nous possedons le seul point
    d'enregistrement du systeme - le monkeypatch _lp_surgical (sitecustomize injecte par
    PYTHONPATH) disparait, et le bug de son `continue` avec lui.
  - cProfile enveloppe la SEULE execution du script, plus toute la machinerie d'import de
    kernprof : les chiffres du panneau Profileur deviennent ceux du code de l'utilisateur.

MECANISME (constats a, c, d du TODO, tous mesures) :

  - le decorateur est ajoute EN FIN de decorator_list : applique EN PREMIER, il recoit la
    fonction BRUTE (un @lru_cache place au-dessus recevrait ensuite la fonction deja
    enregistree ; dans l'autre sens on recevrait un wrapper sans __code__ exploitable) ;
  - il appelle prof.add_function(fonction) PUIS relit fonction.__code__ pour l'armement :
    add_function peut REMPLACER l'objet-code (padding anti-doublons de bytecode) ;
  - un module importe qui porte des marqueurs est compile depuis sa SOURCE, jamais depuis son
    .pyc (le cache contiendrait la version non decoree) ; le .pyc transforme n'est pas ecrit.

ARMEMENT DES EVENEMENTS (sys.monitoring, Python >= 3.12) :

  - module line_profiler DU FORK SmartOS (marqueur SMARTOS_LOCAL_EVENTS) : il arme lui-meme les
    evenements PAR OBJET-CODE, rien a faire ici ;
  - module line_profiler STOCK (venv ou le fork n'est pas installe) : line_profiler arme
    LINE/PY_RETURN/PY_YIELD en GLOBAL a l'activation, ce qui ralentit ~x7-x9 TOUT le
    programme (mesure). SANS cProfile, on les retire du global juste apres l'activation et
    add_function arme chaque fonction enregistree en LOCAL - memes mesures, plein regime sur
    le code non marque. AVEC cProfile, ce re-scoping serait defait par le module stock
    lui-meme (cf. _preparer_rescope) : on y renonce, mesures justes mais lenteur d'origine.
    RAISE et RERAISE ne sont pas des evenements locaux valides (ValueError: invalid local
    event set, mesure) : ils restent globaux, cout par exception levee, negligeable ;
  - trace "legacy" (Python < 3.12 ou LINE_PROFILER_CORE=legacy) : rien de tout cela
    n'existe, on laisse faire - mesures correctes, juste lentes.

LIMITE CONNUE, assumee (la meme que celle de la marge, cf. profile_targets.py) : une fonction
imbriquee dans une autre fonction n'est pas ciblable individuellement ; le marqueur decore la
fonction de premier niveau qui la contient. Les methodes de classes, y compris imbriquees,
fonctionnent normalement.
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import os.path as osp
import sys
import traceback
import types


#: Nom du decorateur injecte dans les espaces de noms des modules transformes.
#: Improbable a dessein : il ne doit jamais entrer en collision avec un nom de l'utilisateur.
DECORATEUR = '__smartos_lp_add__'

#: Dossiers jamais consideres comme du code utilisateur (mode "tout le code utilisateur").
#: Memes regles que profile_targets._dossier_a_ignorer - les deux doivent rester alignes.
DOSSIERS_IGNORES = {'__pycache__', 'node_modules', 'site-packages', 'dist-packages'}


# ---- Transformation AST ------------------------------------------------------------------
def _defs_a_decorer(tree, lignes):
    """Noeuds FunctionDef/AsyncFunctionDef a decorer.

    `lignes` : ensemble de numeros de lignes marquees, ou None pour TOUTES les fonctions
    (mode "tout le code utilisateur"). Un `def` est retenu si une ligne marquee tombe dans
    son intervalle [lineno, end_lineno]. On ne descend jamais dans le corps d'une fonction
    (les closures ne sont pas ciblables individuellement) ; on descend dans les ClassDef
    (les methodes le sont).
    """
    cibles = []

    def visit(node, inside_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if inside_function:
                    continue
                if lignes is None or any(
                        child.lineno <= ligne <= child.end_lineno for ligne in lignes):
                    cibles.append(child)
                visit(child, True)
            else:
                visit(child, inside_function)

    visit(tree, False)
    return cibles


def _transformer(source, filename, lignes):
    """(objet-code compile, nb de fonctions decorees) pour `source`.

    Le decorateur est ajoute EN FIN de decorator_list (applique en premier, recoit la
    fonction brute - constat d du TODO). ast.copy_location ne deplace pas co_firstlineno
    (constat e) : l'affichage des resultats, aligne sur la ligne du `def`, reste juste.
    """
    tree = ast.parse(source, filename)
    cibles = _defs_a_decorer(tree, lignes)
    for noeud in cibles:
        marque = ast.Name(id=DECORATEUR, ctx=ast.Load())
        ast.copy_location(marque, noeud)
        noeud.decorator_list.append(marque)
    if cibles:
        ast.fix_missing_locations(tree)
    return compile(tree, filename, 'exec'), len(cibles)


# ---- Armement local (module line_profiler stock uniquement) ------------------------------
def _preparer_rescope(prof, cprofile_actif):
    """Retire du GLOBAL les evenements localisables si le module installe est le stock.

    Renvoie True si add_function doit armer chaque fonction en local, False si le module
    recompile s'en charge deja (ou si l'on est en trace legacy, sans sys.monitoring).
    A appeler juste apres prof.enable_by_count() : l'activation vient de poser les
    evenements globaux qu'on retire ici.

    ⚠ Module STOCK + cProfile : re-scoper serait INUTILE, on y renonce (mesure le
    24/07/2026). Le profileur est cree avec wrap_trace=True pour que cProfile recoive les
    evenements PY_RETURN que line_profiler lui vole (sans quoi cProfile ne voit plus aucune
    fin de fonction et impute le temps des appelees suivantes a la mauvaise fonction, cf.
    main()). Or le call_callback du module STOCK re-arme TOUS les evenements de line-tracing
    en GLOBAL apres chaque evenement retransmis : le re-scoping serait defait au premier
    retour de fonction venu. On garde alors des mesures JUSTES au prix de la lenteur
    d'origine sur le code non marque - c'est le repli des plateformes sans recompilation ;
    le module recompile, lui, n'a pas ce re-armement (smartos_global_events) et cumule
    justesse et vitesse.
    """
    noyau = sys.modules.get('line_profiler._line_profiler')
    if noyau is None or getattr(noyau, 'SMARTOS_LOCAL_EVENTS', False):
        return False
    if cprofile_actif:
        return False
    try:
        from line_profiler._diagnostics import USE_LEGACY_TRACE
    except ImportError:
        USE_LEGACY_TRACE = False
    mon = getattr(sys, 'monitoring', None)
    if mon is None or USE_LEGACY_TRACE:
        return False
    try:
        events = mon.get_events(prof.tool_id)
    except ValueError:      # tool id non enregistre : rien a re-scoper
        return False
    locaux = _evenements_locaux(mon)
    if not events & locaux:
        return False
    mon.set_events(prof.tool_id, events & ~locaux)
    return True


def _evenements_locaux(mon):
    """Evenements de line-tracing qui EXISTENT en local (RAISE/RERAISE n'existent pas)."""
    return mon.events.LINE | mon.events.PY_RETURN | mon.events.PY_YIELD


def _armer_local(tool_id, func):
    """Arme les evenements de line-tracing en LOCAL sur l'objet-code de `func`.

    Relire func.__code__ ICI, apres add_function : celui-ci peut avoir remplace l'objet-code
    (padding anti-doublons). Idempotent (OR d'evenements deja poses).
    """
    code = getattr(func, '__code__', None)
    if code is None:
        code = getattr(getattr(func, '__func__', None), '__code__', None)
    if code is None:
        return      # objet sans code : add_function l'a deja ecarte avec un warning
    mon = sys.monitoring
    mon.set_local_events(tool_id, code,
                         mon.get_local_events(tool_id, code)
                         | _evenements_locaux(mon))


def _fabriquer_profileur():
    """Instance LineProfiler dont TOUT enregistrement arme en local si necessaire.

    L'armement est centralise dans add_function (et non dans le seul decorateur injecte) pour
    couvrir aussi les enregistrements qui ne passent pas par lui : un `@profile` nu
    (compatibilite kernprof -b, builtins.profile pose plus bas) enregistre via
    LineProfiler.__call__/add_callable, qui aboutissent tous a add_function.
    """
    from line_profiler import LineProfiler

    class _LineProfilerLanceur(LineProfiler):
        _rescope = False    # pose apres enable_by_count() par _preparer_rescope()

        def add_function(self, func):
            super().add_function(func)
            if self._rescope:
                _armer_local(self.tool_id, func)

    # wrap_trace=True : line_profiler remplace les rappels sys.monitoring de l'outil deja en
    # place (cProfile, meme tool id en 3.12) pour PY_RETURN/PY_YIELD/RAISE/RERAISE ; sans
    # retransmission, cProfile ne voit plus AUCUNE fin de fonction et prend chaque fonction
    # suivante pour une fille de la precedente - mesure le 24/07/2026 : `lente` disparaissait
    # des stats cProfile et son temps (line-profile, donc enorme) etait impute au cumtime de
    # `rapide`, non marquee. Defaut PREEXISTANT du montage kernprof, corrige ici.
    return _LineProfilerLanceur(wrap_trace=True)


def _fabriquer_decorateur(prof):
    """Le decorateur injecte : enregistre la fonction BRUTE et la renvoie inchangee."""
    def _ajouter(func):
        prof.add_function(func)
        return func
    return _ajouter


# ---- Crochet d'import (modules marques et code utilisateur) ------------------------------
def _sous_racine_utilisateur(chemin, racine):
    """Vrai si `chemin` est un module UTILISATEUR sous `racine` (mode tout-le-code).

    Ecarte les segments caches et DOSSIERS_IGNORES, et tout dossier d'environnement virtuel
    (pyvenv.cfg) - on ne profile pas les paquets installes. Memes regles que
    profile_targets._fichiers_py_utilisateur.
    """
    racine = osp.normcase(osp.abspath(racine))
    if not chemin.startswith(racine + os.sep):
        return False
    dossier = racine
    for segment in osp.dirname(chemin)[len(racine):].split(os.sep):
        if not segment:
            continue
        if segment.startswith('.') or segment in DOSSIERS_IGNORES:
            return False
        dossier = osp.join(dossier, segment)
        if osp.isfile(osp.join(dossier, 'pyvenv.cfg')):
            return False
    return True


class _ChargeurProfile(importlib.machinery.SourceFileLoader):
    """Chargeur qui compile depuis la SOURCE (jamais le .pyc) en decorant les cibles."""

    def __init__(self, fullname, path, lignes, decorateur):
        super().__init__(fullname, path)
        self._lignes = lignes           # set[int] | None (None = toutes les fonctions)
        self._decorateur = decorateur

    def get_code(self, fullname):
        # Compile TOUJOURS depuis la source : le .pyc en cache contiendrait la version non
        # decoree, et on n'ecrit jamais le .pyc de la version decoree.
        code, _ = _transformer(self.get_source(fullname), self.path, self._lignes)
        return code

    def exec_module(self, module):
        module.__dict__[DECORATEUR] = self._decorateur
        super().exec_module(module)


class _FinderProfile(importlib.abc.MetaPathFinder):
    """Detourne l'import des fichiers a instrumenter vers _ChargeurProfile.

    Paresseux par construction : un module que le script n'importe pas n'est jamais charge,
    donc jamais transforme (recette sure du mode "tout le code utilisateur", sans pre-import
    - le pre-import de kernprof executait le code de premier niveau de tous les modules
    cibles, il a lance un jeu Pyxel au chargement, cf. profile_targets.py).
    """

    def __init__(self, cibles, racine, decorateur):
        self._cibles = cibles           # {chemin normalise: set[lignes]}
        self._racine = racine           # str | None (mode tout-le-code)
        self._decorateur = decorateur

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (spec is None or spec.origin is None
                or type(spec.loader) is not importlib.machinery.SourceFileLoader):
            return None     # pas un module source ordinaire : mecanique normale
        chemin = osp.normcase(osp.abspath(spec.origin))
        lignes = self._cibles.get(chemin)
        if lignes is None:
            if not (self._racine and _sous_racine_utilisateur(chemin, self._racine)):
                return None     # ni marque ni code utilisateur : mecanique normale
            # Mode tout-le-code : toutes les fonctions du module.
        spec.loader = _ChargeurProfile(
            fullname, spec.origin, lignes, self._decorateur)
        spec.cached = None      # jamais de .pyc, ni en lecture ni en ecriture
        return spec


# ---- Ligne de commande et execution ------------------------------------------------------
def _installer_pont_pyxel(shm_name, bridge_path):
    """Detourne un jeu Pyxel profile vers le panneau Spyder au lieu de sa fenetre SDL.

    Ce lanceur tourne dans un QProcess independant (widgets.py, patch SmartOS) : hors de
    toute console/noyau IPython, donc hors du crochet que bridge_manager.attach_console
    installe d'ordinaire par silent_execute (cf. spyder_pyxel/spyder/bridge_manager.py). Le
    canal, lui, est cree cote Spyder AVANT le lancement (bridge_manager.attach_process) ; son
    nom nous arrive ici en argument, et c'est nous qui posons le crochet, depuis CE
    processus - exactement ce que ferait un noyau IPython, via le meme point d'entree.

    `bridge_path` est le dossier ajoute au sys.path de SPYDER par son .pth d'installation
    (cf. Commun/spyder_plugins/install_spyder_plugin.py) : le sous-module bridge/ (par
    opposition a spyder_pyxel/spyder/, qui LUI depend de Qt et de Spyder) est concu pour
    tourner dans n'importe quel interprete ou pyxel est installe, pas seulement celui de
    Spyder (cf. l'en-tete de bridge/hooks.py) - il suffit de le rendre importable ici, qui
    plus est SANS l'avoir installe (pip) dans main_interpreter.

    Best-effort et jamais fatal : ce lanceur tourne aussi sur des scripts qui n'importent pas
    pyxel, et meme un script Pyxel garde un repli correct (sa fenetre SDL normale) si le pont
    ne peut pas s'installer - paquet introuvable, segment deja ferme, etc. Le profilage
    lui-meme ne doit jamais en dependre.
    """
    # append, PAS insert(0) : ne doit pas passer devant le dossier du script profile dans la
    # resolution des imports (sys.path[0], pose juste au-dessus dans main()).
    if bridge_path and bridge_path not in sys.path:
        sys.path.append(bridge_path)
    try:
        import spyder_pyxel.bridge.kernel as pont
        pont.install(shm_name)
    except Exception:
        pass


def _parse_args(argv):
    """(options, script, args du script). Les options sont TOUJOURS avant le script : tout ce
    qui suit le premier argument positionnel appartient au script, jamais a nous."""
    options = {'--lprof': None, '--prof': None, '--config': None,
               '--pyxel-shm': None, '--pyxel-bridge-path': None}
    index = 0
    while index < len(argv) and argv[index] in options:
        if index + 1 >= len(argv):
            raise SystemExit(f"lp_launcher : valeur manquante pour {argv[index]}")
        options[argv[index]] = argv[index + 1]
        index += 2
    if index >= len(argv):
        raise SystemExit("lp_launcher : script a profiler manquant")
    if not options['--lprof'] or not options['--config']:
        raise SystemExit("lp_launcher : --lprof et --config sont obligatoires")
    return options, argv[index], argv[index + 1:]


def _charger_config(chemin):
    """Configuration ecrite par profile_targets.ecrire_config_lanceur (Spyder, autre process).

    Renvoie ({chemin normalise: set[lignes]}, racine tout-le-code ou None).
    """
    with open(chemin, 'r', encoding='utf-8') as flux:
        config = json.load(flux)
    cibles = {osp.normcase(osp.abspath(fichier)): set(lignes)
              for fichier, lignes in config.get('targets', {}).items() if lignes}
    racine = config.get('racine') if config.get('all_user') else None
    return cibles, racine


def _armer_arret_gracieux():
    """SIGTERM se comporte comme un Ctrl+C : leve KeyboardInterrupt au lieu de tuer net.

    Sans cela, un arret depuis Spyder (widgets.py, patch SmartOS : terminate() d'abord,
    kill() seulement si le processus ne repond pas) ne laisserait jamais le `finally` de
    main() ecrire les mesures accumulees - contrairement a un profilage in-noyau (bouton
    "Profiler" natif de Spyder sur un fichier SANS marqueur line-profiler), ou interrompre
    le noyau produit deja un profil partiel via un KeyboardInterrupt ordinaire. Signale par
    l'utilisateur le 01/08/2026 : meme resultat attendu sur les deux chemins.

    SIGKILL reste le filet de securite cote Spyder si ce lanceur ne repond pas a temps
    (bloque dans du code natif, ex. pyxel.run()) : SIGKILL, lui, ne se rattrape pas, et
    c'est voulu - "Stop" doit TOUJOURS finir par arreter le processus.
    """
    import signal

    def _sur_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _sur_sigterm)


def main(argv):
    _armer_arret_gracieux()
    options, script, args_script = _parse_args(argv)
    script = osp.abspath(script)
    cibles, racine = _charger_config(options['--config'])

    with open(script, 'r', encoding='utf-8') as flux:
        source = flux.read()

    # Environnement d'execution identique a `python script.py` : argv du script, dossier du
    # script en tete de sys.path A LA PLACE du dossier de ce lanceur (qui ne doit pas etre
    # importable par le code de l'utilisateur).
    sys.argv = [script] + list(args_script)
    dossier_lanceur = osp.normcase(osp.abspath(osp.dirname(__file__)))
    if sys.path and osp.normcase(osp.abspath(sys.path[0] or '.')) == dossier_lanceur:
        sys.path[0] = osp.dirname(script)
    else:
        sys.path.insert(0, osp.dirname(script))

    if options['--pyxel-shm']:
        _installer_pont_pyxel(options['--pyxel-shm'], options['--pyxel-bridge-path'])

    prof = _fabriquer_profileur()
    builtins.profile = prof     # compat @profile nu (kernprof -b faisait pareil)

    # cProfile s'active AVANT le line profiler, jamais l'inverse : en 3.12 les deux se
    # partagent le MEME outil sys.monitoring (PROFILER_ID). line_profiler sait se greffer
    # sur un outil deja en place (il conserve son nom et ses evenements globaux) ; cProfile,
    # lui, refuse de demarrer si l'outil est pris ("Another profiling tool is already
    # active", mesure le 24/07/2026). L'ancien montage "python -m cProfile -m kernprof"
    # fonctionnait pour la meme raison : cProfile etait le processus englobant.
    profil_c = None
    if options['--prof']:
        import cProfile
        profil_c = cProfile.Profile()
        profil_c.enable()

    prof.enable_by_count()
    prof._rescope = _preparer_rescope(prof, profil_c is not None)
    decorateur = _fabriquer_decorateur(prof)
    sys.meta_path.insert(0, _FinderProfile(cibles, racine, decorateur))

    # Le script lui-meme : decore si marque (ou mode tout-le-code), tel quel sinon.
    chemin_script = osp.normcase(script)
    if racine is not None:
        lignes_script = None if _sous_racine_utilisateur(chemin_script, racine) else set()
    else:
        lignes_script = cibles.get(chemin_script, set())
    code, _ = _transformer(source, script, lignes_script)

    # Un vrai module __main__ (et pas un simple dict) : pickle, dataclasses et consorts
    # retrouvent les classes definies par le script via sys.modules['__main__'].
    module_main = types.ModuleType('__main__')
    module_main.__dict__.update({
        '__file__': script, '__builtins__': builtins, DECORATEUR: decorateur})
    sys.modules['__main__'] = module_main

    code_retour = 0
    try:
        exec(code, module_main.__dict__)
    except SystemExit as sortie:
        if isinstance(sortie.code, int):
            code_retour = sortie.code
        elif sortie.code is not None:
            print(sortie.code, file=sys.stderr)
            code_retour = 1
    except KeyboardInterrupt:
        # Arret volontaire (Stop dans Spyder, cf. _armer_arret_gracieux) : pas de traceback,
        # ce n'est pas un plantage. Les mesures accumulees jusqu'ici sont ecrites quand meme
        # (finally ci-dessous).
        code_retour = 1
    except BaseException:
        # Le script a plante : on l'affiche comme l'interpreteur l'aurait fait, mais on
        # ecrit quand meme les mesures accumulees (finally ci-dessous) - un profil partiel
        # vaut mieux que rien pour comprendre ce qui a precede le plantage.
        traceback.print_exc()
        code_retour = 1
    finally:
        # Ordre inverse de l'activation : le line profiler se deconnecte d'abord (il rend a
        # cProfile les rappels et evenements qu'il avait remplaces), cProfile ensuite.
        prof.disable_by_count()
        if profil_c is not None:
            profil_c.disable()
        prof.dump_stats(options['--lprof'])
        if profil_c is not None:
            profil_c.dump_stats(options['--prof'])
    return code_retour


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
