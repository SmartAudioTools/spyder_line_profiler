#!/usr/bin/env python3
"""Ajoute au plugin spyder-line-profiler des marqueurs "profiler cette fonction" cliquables dans
la marge de l'editeur, calques sur les points d'arret du debogueur.

Contexte (TODO CachyOS "TODO - Spyder - line profiler.txt", demande explicite de
l'utilisateur : "je souhaiterai pouvoir eviter d'avoir a ajouter le decorateur @profile sur
chaque fonction que l'on souhaite profiler pour eviter de casser l'execution sans line
profiler") :

Le plugin lance "python -m kernprof -lvb -o <sortie> <script>". Le "-b" installe un objet
`profile` dans les builtins, donc un `@profile` nu marche QUAND on passe par le plugin et leve
NameError sinon - c'est precisement le probleme signale. Le champ "Command line options" de la
boite de dialogue Run ne peut pas aider : ses arguments sont ajoutes APRES le nom du script,
donc transmis au script, pas a kernprof.

line_profiler 5.x sait profiler des fonctions non decorees via "-p/--prof-mod <nom.pointe>"
(autoprofile). Ce patch fournit l'interface qui permet de designer ces cibles a la souris :
on clique dans une nouvelle marge, a cote de celle des points d'arret, et le marqueur est
traduit en "-p pkg.module.Classe.methode" au lancement du profilage. Le code source n'est jamais
modifie, donc l'execution normale du script n'est jamais cassee.

Ce que fait ce script, en trois temps :

1. INSTALLE le module runtime profile_targets.py (copie depuis
   Commun/spyder_plugins/spyder_line_profiler_targets/, qui est la source de verite versionnee)
   dans site-packages/spyder_line_profiler/spyder/. Il contient la persistance des marqueurs
   (CONF 'spyder_line_profiler'/'profile_targets', meme forme que 'breakpoints' de la section
   [debugger]), le ProfileTargetsManager (etat porte par les BlockUserData, comme les points
   d'arret, donc les marqueurs suivent les editions de lignes), la marge cliquable
   ProfileTargetsPanel, et la configuration {fichier: [lignes]} transmise au lanceur
   (config_lanceur). La copie est TOUJOURS reecrite :
   c'est un fichier a nous, pas un fichier amont, donc il n'y a rien a preserver et cela evite
   qu'une vieille version reste en place apres une mise a jour du depot.

2. PATCHE widgets.py pour REMPLACER la ligne de commande kernprof par notre lanceur
   lp_launcher.py (refonte du 24/07/2026, cf. lp_launcher.py : execution UNIQUE du script,
   decoration AST des seuls `def` marques, cProfile enveloppant le seul code de l'utilisateur).
   Ancrage sur le texte exact de la ligne d'affectation de p_args, presente une seule fois.
   MIGRATION : les blocs des versions anterieures (cibles "-p", racines PYTHONPATH,
   sitecustomize _lp_surgical, enveloppe cProfile+kernprof) sont retires par remplacement de
   leur texte exact - ce script en est l'unique auteur, le texte est donc connu au caractere
   pres ; un residu inattendu fait echouer le patch BRUYAMMENT.

3. PATCHE plugin.py pour instancier un ProfileTargetsManager par editeur de code, en se
   branchant sur sig_codeeditor_created / sig_codeeditor_deleted du plugin Editor - exactement
   la voie qu'emprunte le debogueur de Spyder (spyder/plugins/debugger/plugin.py, lignes 283-285
   et 469-486). C'est une API publique du plugin Editor : AUCUNE modification du coeur de Spyder
   n'est necessaire, tout tient dans le plugin spyder_line_profiler.

Localisation des points d'insertion de l'etape 3 via le module `ast` (comme
patch_spyder_window_title.py et patch_spyder_burger_menu.py) : les methodes sont retrouvees par
leur nom dans la classe, quels que soient les commentaires et decorateurs autour. Les
insertions sont appliquees de la plus basse a la plus haute (offsets decroissants) pour qu'une
insertion ne decale pas les suivantes.

Idempotent : si le marqueur du patch est deja present dans un fichier, ce fichier est laisse
tel quel (l'etape 1, elle, recopie toujours le module).

Filet de securite : chaque fichier patche est reparse avant ecriture ; en cas d'erreur de
syntaxe, rien n'est ecrit et le script sort en erreur. Un site-packages casse rendrait Spyder
impossible a demarrer.
"""

import ast
import os
import os.path as osp
import shutil
import sys


RUNTIME_MODULES = ('profile_targets.py', 'profile_results.py', 'profile_history.py',
                   'profile_cprofile.py', 'lp_launcher.py')

# --- Etape 2 : widgets.py -----------------------------------------------------------------
# Ancre : ligne de construction de la ligne de commande kernprof, unique dans le fichier
# AMONT (vierge). Sur une installation deja patchee, c'est OLD_CPROFILE_WRAP qui la remplace.
WIDGETS_ANCHOR = (
    "        p_args = ['-X', 'utf8', '-m', 'kernprof', '-lvb', '-o', self.DATAPATH]\n"
)

# Lanceur SmartOS (refonte du 24/07/2026) : remplace kernprof par lp_launcher.py, qui execute
# le script UNE seule fois, decore les seuls `def` marques (configuration JSON ecrite par
# ecrire_config_lanceur) et enveloppe l'execution de cProfile. Idempotence par le marqueur
# '_smartos_lp_launcher'.
WIDGETS_LAUNCHER_MARKER = '_smartos_lp_launcher'
WIDGETS_LAUNCHER_WRAP = (
    "        # Lanceur SmartOS (cf. patch_spyder_line_profiler_targets.py et lp_launcher.py) :\n"
    "        # remplace kernprof. Execution UNIQUE du script, decoration AST des seuls `def`\n"
    "        # marques (ou de tout le code utilisateur si l'option est cochee), et cProfile\n"
    "        # enveloppant la seule execution du script : les chiffres du panneau Profileur\n"
    "        # sont ceux du code de l'utilisateur, pas de la machinerie du profileur.\n"
    "        # logger.debug et NON warning : sans gestionnaire, le logging retombe sur stderr,\n"
    "        # que la console interne de Spyder traite comme des erreurs.\n"
    "        from spyder_line_profiler.spyder.profile_targets import ecrire_config_lanceur\n"
    "        self._smartos_prof_path = get_conf_path('lineprofiler_cprofile.prof')\n"
    "        _smartos_lp_launcher = os.path.join(os.path.dirname(__file__), 'lp_launcher.py')\n"
    "        _smartos_lp_config = get_conf_path('lineprofiler_targets.json')\n"
    "        ecrire_config_lanceur(filename, _smartos_lp_config, log=logger.debug)\n"
    "        p_args = ['-X', 'utf8', _smartos_lp_launcher,\n"
    "                  '--lprof', self.DATAPATH, '--prof', self._smartos_prof_path,\n"
    "                  '--config', _smartos_lp_config]\n"
)

# Redirection Pyxel (ajout SmartOS, cf. Commun/spyder_plugins/spyder_pyxel/ et
# TODO - Spyder - Pyxel.txt) : un jeu Pyxel lance par le profileur passe par un QProcess
# independant (lp_launcher.py), hors de toute console/noyau - le crochet habituel
# (spyder_pyxel/spyder/bridge_manager.attach_console, pose par silent_execute dans un noyau
# IPython) n'y est jamais atteint, et le jeu ouvre sa propre fenetre SDL. On cree ici un canal
# GENERIQUE (bridge_manager.attach_process, attache au widget - pas a une console) avant de
# lancer le sous-processus, et on transmet son nom au lanceur : c'est lp_launcher.py, DANS le
# sous-processus, qui posera lui-meme le crochet (spyder_pyxel.bridge.kernel.install), le
# sous-module bridge/ etant concu pour tourner dans n'importe quel interprete ou pyxel est
# installe (cf. son en-tete), independamment de main_interpreter. Sans consequence pour un
# script qui n'importe pas pyxel : le canal reste simplement inutilise. Best-effort total : le
# greffon Pyxel peut ne pas etre installe, le profilage normal ne doit jamais en dependre.
# Ancre : derniere ligne de la construction de p_args (WIDGETS_LAUNCHER_WRAP ci-dessus), AVANT
# l'ajout du nom du script - _parse_args (lp_launcher.py) exige toutes les options avant lui.
WIDGETS_PYXEL_MARKER = '_smartos_pyxel_shm'
WIDGETS_PYXEL_ANCHOR = "                  '--config', _smartos_lp_config]\n"
WIDGETS_PYXEL_PATCH = (
    "        # Redirection Pyxel (ajout SmartOS, cf. patch_spyder_line_profiler_targets.py) :\n"
    "        # si le script profile importe pyxel, son ecran doit apparaitre dans le panneau\n"
    "        # Pyxel plutot que dans sa propre fenetre SDL - best-effort, sans consequence pour\n"
    "        # un script ordinaire ni si le greffon Pyxel n'est pas installe.\n"
    "        try:\n"
    "            from spyder_pyxel.spyder.bridge_manager import get_bridge as "
    "_smartos_get_bridge\n"
    "            import spyder_pyxel as _smartos_spyder_pyxel\n"
    "            _smartos_pyxel_bridge_dir = os.path.dirname(\n"
    "                os.path.dirname(_smartos_spyder_pyxel.__file__))\n"
    "            _smartos_pyxel_shm = _smartos_get_bridge().attach_process(self)\n"
    "        except Exception:\n"
    "            _smartos_pyxel_shm = None\n"
    "        if _smartos_pyxel_shm:\n"
    "            p_args += ['--pyxel-shm', _smartos_pyxel_shm,\n"
    "                       '--pyxel-bridge-path', _smartos_pyxel_bridge_dir]\n"
)

# Meme fonctionnalite, cote liberation : le canal cree par WIDGETS_PYXEL_PATCH doit etre libere
# a la fin du profilage, qu'il ait servi ou non - sans quoi le segment de memoire partagee fuit
# a chaque lancement. `finished()` est appele par QProcess.finished dans TOUS les cas (fin
# normale, plantage, kill_if_running) : cf. self.process.finished.connect(self.finished) plus
# haut dans start().
WIDGETS_PYXEL_DETACH_MARKER = 'detach_console(self)  # SmartOS'
WIDGETS_FINISHED_ANCHOR = "    def finished(self):\n        self.timer.stop()\n"
WIDGETS_PYXEL_DETACH_PATCH = (
    "        # Redirection Pyxel (ajout SmartOS) : libere le canal cree avant le lancement.\n"
    "        try:\n"
    "            from spyder_pyxel.spyder.bridge_manager import get_bridge as "
    "_smartos_get_bridge\n"
    "            _smartos_get_bridge().detach_console(self)  # SmartOS\n"
    "        except Exception:\n"
    "            pass\n"
)

# Arret gracieux (ajout SmartOS, cf. TODO - Spyder - Pyxel.txt, demande de l'utilisateur le
# 01/08/2026 : "on devrait avoir des stats apres un stop, comme pour un script sans marqueur -
# on devrait avoir la meme chose pour le jeu"). Un script SANS marqueur line-profiler tourne
# IN-NOYAU (bouton "Profiler" natif de Spyder, patch_spyder_profiler_reroute.py) : interrompre
# le noyau y produit un KeyboardInterrupt ordinaire, donc un profil PARTIEL. Un script AVEC
# marqueur passe par ce lanceur (lp_launcher.py), en QProcess independant - kill_if_running()
# faisait un process.kill() (SIGKILL) NU, qui ne laisse RIEN s'ecrire (verifie : SIGKILL est
# inconditionnel, aucun code Python ne peut s'executer apres). D'ou l'asymetrie signalee.
# Repris ici a l'identique du principe qui marche deja : terminate() (SIGTERM) D'ABORD - que
# lp_launcher.py attrape desormais et transforme en KeyboardInterrupt (cf. _armer_arret_
# gracieux dans lp_launcher.py) pour ecrire ses mesures partielles comme le fait le noyau -
# et kill() (SIGKILL) SEULEMENT si le processus ne repond pas dans le delai (bloque en code
# natif, ex. pyxel.run() coince entre deux images) : la, aucune mesure n'est recuperable,
# l'etat redevient "aborted" comme avant, sans regression sur la garantie que Stop arrete
# TOUJOURS le processus.
WIDGETS_KILL_MARKER = 'Profiling interrupted'
WIDGETS_KILL_ANCHOR = (
    "    def kill_if_running(self):\n"
    "        self.datelabel.setText(_('Profiling aborted.'))\n"
    "        if self.process is not None:\n"
    "            if self.process.state() == QProcess.Running:\n"
    "                self.process.kill()\n"
    "                self.output = 'aborted'\n"
    "                self.process.waitForFinished()\n"
)
WIDGETS_KILL_PATCH = (
    "    def kill_if_running(self):\n"
    "        # Arret gracieux (ajout SmartOS, cf. patch_spyder_line_profiler_targets.py) :\n"
    "        # SIGTERM d'abord (lp_launcher.py l'attrape et ecrit ses mesures partielles),\n"
    "        # SIGKILL en repli si le processus ne repond pas dans le delai.\n"
    "        if self.process is not None:\n"
    "            if self.process.state() == QProcess.Running:\n"
    "                self.process.terminate()\n"
    "                if self.process.waitForFinished(5000):\n"
    "                    self.datelabel.setText(_('Profiling interrupted.'))\n"
    "                else:\n"
    "                    self.process.kill()\n"
    "                    self.output = 'aborted'\n"
    "                    self.datelabel.setText(_('Profiling aborted.'))\n"
    "                    self.process.waitForFinished()\n"
    "            else:\n"
    "                self.datelabel.setText(_('Profiling aborted.'))\n"
    "        else:\n"
    "            self.datelabel.setText(_('Profiling aborted.'))\n"
)

# --- Blocs des versions ANTERIEURES, a retirer en migration. Textes exacts poses par les
# versions precedentes de ce script (qui en est l'unique auteur) : un remplacement par ''
# suffit, et un residu detecte apres coup fait echouer le patch.
# Le bloc des cibles a existe en PLUSIEURS variantes (commentaires enrichis au fil des
# versions, migration build_ -> run_prof_mod_args) : il est retire par SENTINELLES - de sa
# premiere ligne de commentaire (stable depuis l'origine) a sa ligne finale p_args.extend -
# plutot que par texte exact.
OLD_TARGETS_DEBUT = ("        # Cibles issues des marqueurs poses dans la marge de "
                     "l'editeur (ajout SmartOS, cf.\n")
OLD_TARGETS_FIN = "        p_args.extend(_smartos_prof_mod)\n"

OLD_CPROFILE_WRAP = (
    "        # Profilage COMBINE (ajout SmartOS, cf. patch_spyder_line_profiler_targets.py) :\n"
    "        # cProfile enveloppe kernprof dans UN SEUL process, donc une seule execution du\n"
    "        # script donne a la fois les mesures par ligne (kernprof -> self.DATAPATH) et un\n"
    "        # pstats cProfile (self._smartos_prof_path) pour le panneau Profileur integre. Les\n"
    "        # temps sont un peu gonfles (chaque outil compte dans le champ de l'autre), compromis\n"
    "        # accepte (cf. TODO - Spyder - line profiler.txt).\n"
    "        self._smartos_prof_path = get_conf_path('lineprofiler_cprofile.prof')\n"
    "        p_args = ['-X', 'utf8', '-m', 'cProfile', '-o', self._smartos_prof_path,\n"
    "                  '-m', 'kernprof', '-lvb', '-o', self.DATAPATH]\n"
)

OLD_ENV_PATCH = '''
        # Racines de paquet des fichiers marques, ajoutees au PYTHONPATH (ajout SmartOS, cf.
        # Commun/scripts/patch_spyder_line_profiler_targets.py). Sans elles, une cible
        # "pkg.maths.lente" n'est resolue par kernprof que si le script profile se trouve par
        # hasard a cote de "pkg" : profiler pkg/maths.py lui-meme met "<projet>/pkg" en
        # sys.path[0] et kernprof abandonne avec "N import targets cannot be resolved".
        # Doit etre fait AVANT setProcessEnvironment() ci-dessous, sinon le processus recoit
        # l'environnement d'avant.
        from spyder_line_profiler.spyder.profile_targets import run_prof_mod_roots
        _smartos_roots = run_prof_mod_roots(filename, log=logger.debug)
        if _smartos_roots:
            _smartos_pythonpath = [
                path for path in proc_env.value('PYTHONPATH', '').split(os.pathsep) if path]
            for _smartos_root in _smartos_roots:
                if _smartos_root not in _smartos_pythonpath:
                    _smartos_pythonpath.append(_smartos_root)
            proc_env.insert('PYTHONPATH', os.pathsep.join(_smartos_pythonpath))
            logger.debug(f'PYTHONPATH du profilage : {_smartos_pythonpath}')
'''

OLD_SURGICAL_PATCH = '''
        # Rend le line profiler chirurgical (ajout SmartOS, cf.
        # Commun/scripts/patch_spyder_line_profiler_targets.py).
        _smartos_surgical_dir = os.path.join(os.path.dirname(__file__), '_lp_surgical')
        if os.path.isdir(_smartos_surgical_dir):
            _smartos_surgical_pp = [
                path for path in proc_env.value('PYTHONPATH', '').split(os.pathsep) if path]
            if _smartos_surgical_dir not in _smartos_surgical_pp:
                _smartos_surgical_pp.insert(0, _smartos_surgical_dir)
                proc_env.insert('PYTHONPATH', os.pathsep.join(_smartos_surgical_pp))
'''

#: Residus qui ne doivent PLUS apparaitre dans widgets.py apres le patch. Chacun signerait un
#: melange de versions que ce script ne sait pas demeler automatiquement.
WIDGETS_RESIDUS_INTERDITS = (
    "'-m', 'kernprof'", '_lp_surgical', '_smartos_roots', 'run_prof_mod_args',
    '_smartos_prof_mod')

# Ancre : chargement des resultats par l'arbre du panneau, unique dans le fichier.
WIDGETS_RESULTS_ANCHOR = "        self.datatree.load_data(self.DATAPATH)\n"

WIDGETS_RESULTS_PATCH = '''
        # Publication des resultats vers les editeurs ouverts (ajout SmartOS, cf.
        # Commun/scripts/patch_spyder_line_profiler_targets.py) : lignes colorees et temps dans
        # la marge de droite. On se greffe ICI, apres le chargement par l'arbre du panneau, pour
        # relire le MEME fichier au MEME moment - donc jamais de desynchronisation entre ce que
        # montre le panneau et ce que montre l'editeur.
        from spyder_line_profiler.spyder.profile_results import publish
        publish(self.DATAPATH)
'''

# Ancre : effacement des donnees du panneau, unique dans le fichier.
WIDGETS_CLEAR_ANCHOR = "    def clear_data(self):\n        self.datatree.clear()\n"

WIDGETS_CLEAR_PATCH = '''        # Le bouton d'effacement du panneau vide aussi l'editeur (ajout SmartOS) : sans cela
        # les lignes resteraient colorees par un profilage que l'utilisateur vient d'effacer.
        from spyder_line_profiler.spyder.profile_results import clear as _smartos_clear_results
        _smartos_clear_results()
'''

# Point 2 du profilage COMBINE : afficher le pstats cProfile dans le panneau Profileur integre.
# Insere APRES le chargement/publication des resultats (WIDGETS_RESULTS_ANCHOR), au meme instant
# et dans le meme objet ou self._smartos_prof_path a ete pose (cf. WIDGETS_CPROFILE_WRAP). On
# reutilise show_profile_buffer, la methode meme que le noyau appelle pour afficher un profilage
# cProfile. Silencieux si le fichier manque ou si le plugin Profileur est absent : l'affichage
# cProfile est un plus, il ne doit jamais empecher les mesures par ligne. Marqueur d'idempotence
# propre : '_smartos_feed_profiler'.
WIDGETS_FEED_MARKER = '_smartos_feed_profiler'
WIDGETS_FEED_PATCH = '''
        # Profilage COMBINE, point 2 (ajout SmartOS) : le pstats cProfile produit par le run
        # enveloppe (self._smartos_prof_path) est pousse dans le panneau Profileur integre, via
        # show_profile_buffer - exactement ce que fait le noyau apres un profilage. Cf.
        # Commun/scripts/patch_spyder_line_profiler_targets.py.
        def _smartos_feed_profiler(self=self):
            _smartos_prof = getattr(self, '_smartos_prof_path', None)
            if not _smartos_prof:
                return
            try:
                import os.path as _smartos_osp
                from spyder.api.plugins import Plugins as _SmartosPlugins
                if not _smartos_osp.isfile(_smartos_prof):
                    return
                _smartos_profiler = self.get_plugin().get_plugin(
                    _SmartosPlugins.Profiler, error=False)
                if _smartos_profiler is None:
                    return
                _smartos_sub = _smartos_profiler.get_widget().current_widget()
                if _smartos_sub is None:
                    return
                with open(_smartos_prof, 'rb') as _smartos_f:
                    _smartos_sub.show_profile_buffer(_smartos_f.read(), [])
            except Exception:
                import traceback as _smartos_tb
                _smartos_tb.print_exc()
        _smartos_feed_profiler()
'''

# --- Etape 3 : plugin.py ------------------------------------------------------------------
# Rend le plugin Profileur atteignable depuis le Line Profiler (get_plugin) pour le point 2.
PLUGIN_OPTIONAL_ANCHOR = "    OPTIONAL = []\n"
PLUGIN_OPTIONAL_PATCH = "    OPTIONAL = [Plugins.Profiler]  # SmartOS : profilage combine (point 2)\n"

PLUGIN_CONNECT_PATCH = '''
        # Marqueurs "profiler cette fonction" dans la marge (ajout SmartOS, cf.
        # Commun/scripts/patch_spyder_line_profiler_targets.py). sig_codeeditor_created /
        # _deleted sont l'API publique par laquelle le debogueur de Spyder installe sa propre
        # marge de points d'arret : rien a patcher dans le coeur de Spyder.
        editor.sig_codeeditor_created.connect(self._smartos_add_codeeditor)
        editor.sig_codeeditor_deleted.connect(self._smartos_remove_codeeditor)
        # Purge des marqueurs des fichiers non reouverts, une fois la session RESTAUREE
        # (sig_open_files_finished) : "un marqueur = un fichier ouvert".
        editor.sig_open_files_finished.connect(self._smartos_purge_fichiers_fermes)
'''

PLUGIN_DISCONNECT_PATCH = '''
        # Ajout SmartOS (marqueurs de profilage dans la marge).
        editor.sig_codeeditor_created.disconnect(self._smartos_add_codeeditor)
        editor.sig_codeeditor_deleted.disconnect(self._smartos_remove_codeeditor)
        editor.sig_open_files_finished.disconnect(self._smartos_purge_fichiers_fermes)
'''

PLUGIN_BEGIN = "    # <<< SmartOS line profiler : debut du bloc injecte >>>\n"
PLUGIN_END = "    # <<< SmartOS line profiler : fin du bloc injecte >>>\n"

# Le bloc injecte dans plugin.py est volontairement MINCE : il ne fait que deleguer aux modules
# runtime, qui eux sont recopies a chaque execution de ce script. Toute evolution de la logique
# se fait donc dans profile_targets.py / profile_results.py, sans jamais avoir a reecrire du
# code deja injecte dans un fichier amont.
# C'est la lecon d'un vrai rate : la premiere version mettait toute la logique ici, et son
# marqueur d'idempotence ("_smartos_add_codeeditor" present ?) a fait sauter silencieusement la
# mise a jour du bloc lors de l'ajout des resultats dans l'editeur. D'ou aussi les deux
# sentinelles ci-dessus : le bloc est desormais REMPLACE quand il existe deja, jamais saute.
PLUGIN_METHODS_PATCH = PLUGIN_BEGIN + '''    # Marges du line profiler (marqueurs a gauche, temps a droite). Cf.
    # Commun/scripts/patch_spyder_line_profiler_targets.py.

    def _smartos_add_codeeditor(self, codeeditor):
        """Installe les deux marges du line profiler sur un editeur Python."""
        # Import tardif : ces modules tirent qtawesome et des modules d'editeur de Spyder,
        # inutiles tant qu'aucun fichier n'est ouvert.
        from spyder_line_profiler.spyder.profile_targets import attach_editor
        attach_editor(codeeditor)

    def _smartos_remove_codeeditor(self, codeeditor):
        """Detache les marges quand l'editeur est ferme."""
        from spyder_line_profiler.spyder.profile_targets import detach_editor
        detach_editor(codeeditor)

    def _smartos_purge_fichiers_fermes(self):
        """A la fin de la restauration de session : purge les marqueurs des fichiers non
        reouverts (fermes lors d'une session precedente). Cf. profile_targets."""
        from spyder_line_profiler.spyder.profile_targets import (
            purger_marqueurs_des_fichiers_fermes)
        purger_marqueurs_des_fichiers_fermes()
''' + PLUGIN_END


def find_method(tree, class_name, method_name):
    """Retourne le noeud AST de <class_name>.<method_name>, ou None."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name == method_name):
                    return sub
    return None


def find_class(tree, class_name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def write_checked(path, patched, what):
    """Ecrit `patched` dans `path` seulement si c'est du Python valide."""
    try:
        ast.parse(patched)
    except SyntaxError as error:
        print(f"Le {what} patche n'est pas du Python valide ({error}) - aucune modification "
              "ecrite.", file=sys.stderr)
        return False
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(patched)
    return True


def _runtime_modules_dir():
    """Dossier des modules runtime, resolu relativement a CE fichier.

    Deux dispositions possibles, essayees dans l'ordre :
      - depot SmartOS : ce patch est dans Commun/scripts_installation/spyder_patch/,
        les modules dans Commun/spyder_plugins/spyder_line_profiler_targets/ (deux
        crans au-dessus — un seul cran etait juste avant le deplacement du 08/08/2026,
        et le chemin casse ce jour-la a ete rattrape ici) ;
      - distribution SmartPythonEditor : ce patch est dans smartos-plugins/patchs-tiers/,
        les modules dans smartos-plugins/spyder_line_profiler_targets/ (un cran).
    """
    ici = osp.dirname(osp.abspath(__file__))
    candidats = (
        osp.normpath(osp.join(ici, osp.pardir, osp.pardir, 'spyder_plugins',
                              'spyder_line_profiler_targets')),
        osp.normpath(osp.join(ici, osp.pardir, 'spyder_line_profiler_targets')),
    )
    for dossier in candidats:
        if osp.isfile(osp.join(dossier, RUNTIME_MODULES[0])):
            return dossier
    print("Modules runtime introuvables (essaye : " + ", ".join(candidats) + ") - "
          "patch du line profiler non applique.", file=sys.stderr)
    return None


def install_runtime_modules(plugin_dir):
    """Etape 1 : copie les modules runtime dans le plugin installe."""
    dossier = _runtime_modules_dir()
    if dossier is None:
        return False
    for module in RUNTIME_MODULES:
        source = osp.join(dossier, module)
        if not osp.isfile(source):
            print(f"{source} introuvable - patch du line profiler non applique.",
                  file=sys.stderr)
            return False
        shutil.copyfile(source, osp.join(plugin_dir, module))
    return True


def remove_surgical_sitecustomize(plugin_dir):
    """Etape 1 bis : retire le sitecustomize _lp_surgical des versions anterieures.

    Depuis la refonte du lanceur (24/07/2026), l'armement local des evenements est fait soit
    par le module line_profiler du fork SmartOS (installe par le requirements), soit par
    lp_launcher.py lui-meme (repli sur module stock) : le monkeypatch injecte par PYTHONPATH
    n'a plus de raison d'etre - et son bug (re-scoping abandonne au 2e passage, cf. TODO) part
    avec lui.
    """
    surgical_dir = osp.join(plugin_dir, '_lp_surgical')
    if osp.isdir(surgical_dir):
        shutil.rmtree(surgical_dir)
        print("_lp_surgical/ retire (remplace par lp_launcher.py).")
    return True


def patch_widgets(path):
    """Etape 2 : fait lancer lp_launcher.py (et retire les blocs kernprof des versions passees)."""
    with open(path, encoding='utf-8') as stream:
        source = stream.read()

    besoin_lanceur = WIDGETS_LAUNCHER_MARKER not in source
    besoin_resultats = 'profile_results import publish' not in source
    besoin_effacement = '_smartos_clear_results' not in source
    besoin_feed = WIDGETS_FEED_MARKER not in source
    besoin_pyxel = WIDGETS_PYXEL_MARKER not in source
    besoin_pyxel_detach = WIDGETS_PYXEL_DETACH_MARKER not in source
    besoin_kill = WIDGETS_KILL_MARKER not in source
    # Migration : blocs des versions anterieures a retirer (presence testee par leur texte
    # exact, dont ce script est l'unique auteur ; le bloc des cibles, a variantes, par ses
    # sentinelles).
    vieux_blocs = [bloc for bloc in (OLD_ENV_PATCH, OLD_SURGICAL_PATCH) if bloc in source]
    vieilles_cibles = OLD_TARGETS_DEBUT in source
    if not any((besoin_lanceur, besoin_resultats, besoin_effacement, besoin_feed,
                besoin_pyxel, besoin_pyxel_detach, besoin_kill, vieux_blocs, vieilles_cibles)):
        print("widgets.py : lanceur, publication, alimentation du profileur, effacement, "
              "redirection Pyxel et arret gracieux deja injectes.")
        return True

    patched = source
    for bloc in vieux_blocs:
        patched = patched.replace(bloc, '')
    if vieilles_cibles:
        debut = patched.find(OLD_TARGETS_DEBUT)
        fin = patched.find(OLD_TARGETS_FIN, debut)
        if fin == -1:
            print("widgets.py : bloc des cibles kernprof sans sa ligne finale - patch "
                  "abandonne. Structure inattendue ?", file=sys.stderr)
            return False
        patched = patched[:debut] + patched[fin + len(OLD_TARGETS_FIN):]

    if besoin_lanceur:
        # Deux etats possibles : fichier AMONT vierge (ligne kernprof d'origine), ou
        # installation anterieure (enveloppe cProfile+kernprof posee par OLD_CPROFILE_WRAP).
        if OLD_CPROFILE_WRAP in patched:
            patched = patched.replace(OLD_CPROFILE_WRAP, WIDGETS_LAUNCHER_WRAP)
        elif patched.count(WIDGETS_ANCHOR) == 1:
            patched = patched.replace(WIDGETS_ANCHOR, WIDGETS_LAUNCHER_WRAP)
        else:
            print("widgets.py : ni la ligne kernprof d'origine ni l'enveloppe cProfile "
                  "connue - injection du lanceur abandonnee. Structure du fichier changee ?",
                  file=sys.stderr)
            return False

    for besoin, ancre, quoi in (
            (besoin_resultats, WIDGETS_RESULTS_ANCHOR, "le chargement des resultats"),
            (besoin_feed, WIDGETS_RESULTS_ANCHOR, "l'alimentation du profileur"),
            (besoin_effacement, WIDGETS_CLEAR_ANCHOR, "l'effacement des donnees"),
            (besoin_pyxel, WIDGETS_PYXEL_ANCHOR, "la redirection Pyxel (construction de p_args)"),
            (besoin_pyxel_detach, WIDGETS_FINISHED_ANCHOR,
             "la redirection Pyxel (liberation dans finished())"),
            (besoin_kill, WIDGETS_KILL_ANCHOR, "kill_if_running (arret gracieux)")):
        if besoin and patched.count(ancre) != 1:
            print(f"widgets.py : {quoi} n'a pas ete trouve (ou trouve plusieurs fois) - patch "
                  "abandonne. Structure du fichier changee ?", file=sys.stderr)
            return False

    if besoin_resultats:
        patched = patched.replace(
            WIDGETS_RESULTS_ANCHOR, WIDGETS_RESULTS_ANCHOR + WIDGETS_RESULTS_PATCH)
    if besoin_feed:
        # Point 2 : insere apres le chargement des resultats. Si la publication est posee au meme
        # tour, elle l'a deja ete juste apres l'ancre ; le remplacement ci-dessous glisse
        # l'alimentation entre l'ancre et la publication (ordre sans importance, les deux sont
        # independantes).
        patched = patched.replace(
            WIDGETS_RESULTS_ANCHOR, WIDGETS_RESULTS_ANCHOR + WIDGETS_FEED_PATCH)
    if besoin_effacement:
        patched = patched.replace(
            WIDGETS_CLEAR_ANCHOR, WIDGETS_CLEAR_ANCHOR + WIDGETS_CLEAR_PATCH)
    if besoin_pyxel:
        patched = patched.replace(
            WIDGETS_PYXEL_ANCHOR, WIDGETS_PYXEL_ANCHOR + WIDGETS_PYXEL_PATCH)
    if besoin_pyxel_detach:
        patched = patched.replace(
            WIDGETS_FINISHED_ANCHOR, WIDGETS_FINISHED_ANCHOR + WIDGETS_PYXEL_DETACH_PATCH)
    if besoin_kill:
        # Remplacement, PAS ajout : c'est le corps de kill_if_running() qui change, pas un
        # complement pose a cote (contrairement aux autres blocs ci-dessus).
        patched = patched.replace(WIDGETS_KILL_ANCHOR, WIDGETS_KILL_PATCH)

    # Filet : aucun residu des montages kernprof ne doit rester, sinon on a un melange de
    # versions que ce script ne sait pas demeler - mieux vaut echouer que laisser un widgets.py
    # incoherent (double lancement, PYTHONPATH fantome...).
    residus = [r for r in WIDGETS_RESIDUS_INTERDITS if r in patched]
    if residus:
        print(f"widgets.py : residus d'anciennes versions du patch detectes ({residus}) - "
              "aucune modification ecrite. Reinstaller le plugin puis relancer ce script.",
              file=sys.stderr)
        return False
    return write_checked(path, patched, 'widgets.py')


def replace_between_sentinels(source, begin, end, remplacement):
    """Remplace le bloc delimite par les sentinelles, ou renvoie None s'il n'y en a pas.

    Sert a METTRE A JOUR un bloc deja injecte au lieu de le sauter. Sans cela, un fichier amont
    deja patche garde indefiniment la premiere version du bloc, meme quand ce script evolue -
    exactement le piege ou ce patch est tombe le 21/07/2026 en ajoutant la marge des resultats.
    """
    debut = source.find(begin)
    if debut == -1:
        return None
    fin = source.find(end, debut)
    if fin == -1:
        return None
    return source[:debut] + remplacement + source[fin + len(end):]


def patch_plugin(path):
    """Etape 3 : branche les marges du line profiler sur chaque editeur de code.

    Trois blocs INDEPENDANTS, chacun avec son propre critere d'idempotence - la regle du depot
    est de ne jamais coiffer plusieurs blocs d'un marqueur unique, sous peine de sauter
    silencieusement l'un d'eux quand le patch evolue :
      - les deux lignes de connexion, dans on_editor_available ;
      - les deux lignes de deconnexion, dans on_editor_teardown ;
      - les methodes _smartos_*, delimitees par des sentinelles et donc REMPLACABLES.
    """
    with open(path, encoding='utf-8') as stream:
        source = stream.read()

    besoin_connect = 'connect(self._smartos_add_codeeditor)' not in source
    besoin_disconnect = 'disconnect(self._smartos_add_codeeditor)' not in source
    besoin_purge_ouverture = 'sig_open_files_finished' not in source

    patched = source
    faits = []

    # --- Migration : un install anterieur porte deja les connexions add/remove mais PAS celle de la
    # purge a l'ouverture (sig_open_files_finished). On ajoute les deux lignes manquantes apres les
    # existantes. Sur un install NEUF (besoin_connect), PLUGIN_CONNECT/DISCONNECT_PATCH les portent
    # deja - d'ou la garde "not besoin_connect".
    if besoin_purge_ouverture and not besoin_connect:
        anc = "        editor.sig_codeeditor_deleted.connect(self._smartos_remove_codeeditor)\n"
        if anc in patched:
            patched = patched.replace(anc, anc + "        editor.sig_open_files_finished."
                                      "connect(self._smartos_purge_fichiers_fermes)\n")
        ancd = "        editor.sig_codeeditor_deleted.disconnect(self._smartos_remove_codeeditor)\n"
        if ancd in patched:
            patched = patched.replace(ancd, ancd + "        editor.sig_open_files_finished."
                                      "disconnect(self._smartos_purge_fichiers_fermes)\n")
        faits.append("purge a l'ouverture branchee")

    # --- OPTIONAL : rend le plugin Profileur atteignable depuis le Line Profiler (get_plugin),
    # indispensable au point 2 du profilage combine. Simple remplacement de la liste vide ;
    # idempotent, l'ancre "OPTIONAL = []" disparaissant une fois remplacee. Si une version de
    # Spyder livrait un OPTIONAL non vide, l'ancre serait absente et on n'y toucherait pas : le
    # point 2 se degraderait alors en silence (feed sans effet), sans jamais casser le reste.
    if PLUGIN_OPTIONAL_ANCHOR in patched:
        patched = patched.replace(PLUGIN_OPTIONAL_ANCHOR, PLUGIN_OPTIONAL_PATCH)
        faits.append("OPTIONAL enrichi (Profiler, point 2)")

    # --- Bloc des methodes : remplace s'il existe deja, insere sinon.
    a_jour = replace_between_sentinels(
        patched, PLUGIN_BEGIN, PLUGIN_END, PLUGIN_METHODS_PATCH)
    if a_jour is not None:
        if a_jour != patched:
            faits.append("methodes mises a jour")
        patched = a_jour
    elif '_smartos_add_codeeditor(self, codeeditor)' in patched:
        # Bloc de la toute premiere version, pose avant l'introduction des sentinelles : on ne
        # sait pas ou il finit, donc on refuse plutot que de risquer une double insertion.
        print("plugin.py : methodes _smartos_* presentes mais sans sentinelles (version "
              "anterieure au 21/07/2026). Les retirer a la main, ou reinstaller le plugin, "
              "puis relancer ce script.", file=sys.stderr)
        return False

    # ⚠ "a_jour is not None" veut dire "le bloc etait deja la", PAS "il y a du travail". Une
    # premiere version de ce test confondait les deux et sortait en annoncant "deja a jour"
    # alors que les methodes manquaient completement - les connexions, elles, etaient bien
    # presentes, ce qui suffisait a satisfaire les deux autres criteres.
    besoin_methodes = a_jour is None
    if not any((faits, besoin_methodes, besoin_connect, besoin_disconnect)):
        print("plugin.py : marges deja branchees et a jour.")
        return True

    # Le besoin_purge_ouverture d'un install ancien est deja traite par la migration ci-dessus
    # (dans faits) ; sur un install neuf, PLUGIN_CONNECT_PATCH le porte.

    tree = ast.parse(patched)
    cls = find_class(tree, 'SpyderLineProfiler')
    available = find_method(tree, 'SpyderLineProfiler', 'on_editor_available')
    teardown = find_method(tree, 'SpyderLineProfiler', 'on_editor_teardown')
    if cls is None or available is None or teardown is None:
        print("plugin.py : SpyderLineProfiler.on_editor_available/on_editor_teardown "
              "introuvables - branchement des marges abandonne.", file=sys.stderr)
        return False

    insertions = []
    if besoin_methodes:
        insertions.append((cls.body[-1].end_lineno, PLUGIN_METHODS_PATCH))
        faits.append("methodes inserees")
    if besoin_disconnect:
        insertions.append((teardown.body[-1].end_lineno, PLUGIN_DISCONNECT_PATCH))
        faits.append("deconnexion branchee")
    if besoin_connect:
        insertions.append((available.body[-1].end_lineno, PLUGIN_CONNECT_PATCH))
        faits.append("connexion branchee")

    lines = patched.splitlines(keepends=True)
    # Offsets decroissants : une insertion ne doit pas decaler les suivantes.
    for offset, text in sorted(insertions, reverse=True):
        lines.insert(offset, text)

    print("plugin.py : " + ", ".join(faits) + ".")
    return write_checked(path, ''.join(lines), 'plugin.py')


# --- Etape 5 : confpage.py (case a cocher de l'option "profiler tout le code utilisateur") -----
CONFPAGE_MARKER = 'profile_all_user'
CONFPAGE_ANCHOR_CREATE = (
    "        use_color_box = self.create_checkbox(\n"
    "            _(\"Use deterministic colors to differentiate functions\"),\n"
    "            'use_colors', default=True)\n"
)
CONFPAGE_PATCH_CREATE = (
    "        # Ajout SmartOS : profiler TOUTES les fonctions des modules utilisateur du projet,\n"
    "        # sans marquage (option, cf. profile_targets.config_lanceur). Cochee = run plus lent\n"
    "        # (line-profiler ajoute un surcout par ligne sur chaque fonction).\n"
    "        profile_all_box = self.create_checkbox(\n"
    "            _(\"Profiler TOUTES les fonctions des modules utilisateur du projet \"\n"
    "              \"(sans marquage) - ralentit le run\"),\n"
    "            'profile_all_user', default=False)\n"
)
CONFPAGE_ANCHOR_ADD = "        settings_layout.addWidget(use_color_box)\n"
CONFPAGE_PATCH_ADD = "        settings_layout.addWidget(profile_all_box)  # SmartOS\n"


def patch_confpage(path):
    """Etape 5 : ajoute la case "profiler tout le code utilisateur" a la page de preferences.

    NON fatal si les ancres ont change : sans la case, l'option reste reglable via CONF
    ('spyder_line_profiler'/'profile_all_user'), le profilage la lit de toute facon.
    """
    with open(path, encoding='utf-8') as stream:
        source = stream.read()
    if CONFPAGE_MARKER in source:
        print("confpage.py : option 'profiler tout le code utilisateur' deja presente.")
        return True
    if source.count(CONFPAGE_ANCHOR_CREATE) != 1 or source.count(CONFPAGE_ANCHOR_ADD) != 1:
        print("confpage.py : ancres non trouvees (structure changee ?) - case a cocher non "
              "ajoutee (l'option reste reglable via CONF).", file=sys.stderr)
        return False
    patched = source.replace(CONFPAGE_ANCHOR_CREATE, CONFPAGE_ANCHOR_CREATE + CONFPAGE_PATCH_CREATE)
    patched = patched.replace(CONFPAGE_ANCHOR_ADD, CONFPAGE_ANCHOR_ADD + CONFPAGE_PATCH_ADD)
    return write_checked(path, patched, 'confpage.py')


def main():
    if len(sys.argv) != 2:
        print(f"Usage : {sys.argv[0]} <chemin vers site-packages/spyder_line_profiler/spyder>",
              file=sys.stderr)
        return 1

    plugin_dir = sys.argv[1]
    widgets = osp.join(plugin_dir, 'widgets.py')
    plugin = osp.join(plugin_dir, 'plugin.py')
    for path in (widgets, plugin):
        if not osp.isfile(path):
            print(f"{path} introuvable - marqueurs de profilage non installes.", file=sys.stderr)
            return 1

    if not install_runtime_modules(plugin_dir):
        return 1
    if not remove_surgical_sitecustomize(plugin_dir):
        return 1
    if not patch_widgets(widgets):
        return 1
    if not patch_plugin(plugin):
        return 1
    # Case a cocher de l'option "profiler tout le code utilisateur" : non fatale (l'option reste
    # reglable via CONF si la page de prefs a change de structure).
    confpage = osp.join(plugin_dir, 'confpage.py')
    if osp.isfile(confpage):
        patch_confpage(confpage)

    print("Marqueurs de profilage installes (marge cliquable dans l'editeur, profilage via "
          "lp_launcher.py).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
