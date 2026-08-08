# -*- coding: utf-8 -*-
"""Marqueurs "profiler cette fonction" dans la marge de l'editeur Spyder.

Installe par Commun/scripts_installation/spyder_patch/patch_spyder_line_profiler_targets.py dans
site-packages/spyder_line_profiler/spyder/profile_targets.py. Ce fichier est la source de
verite (versionne dans le depot) ; la copie dans site-packages est jetable et recreee a chaque
execution de installation_SmartPythonEditor.sh.

Contexte (TODO CachyOS "TODO - Spyder - line profiler.txt", demande explicite de
l'utilisateur : "je souhaiterai pouvoir eviter d'avoir a ajouter le decorateur @profile sur
chaque fonction que l'on souhaite profiler pour eviter de casser l'execution sans line
profiler", puis, apres presentation de quatre options, choix de celle calquee sur le mecanisme
des points d'arret du debogueur) :

Le plugin spyder-line-profiler lancait "python -m kernprof -lvb -o <out> <script>" ; depuis la
refonte du 24/07/2026 il lance notre lp_launcher.py, qui decore lui-meme les `def` marques au
moment de compiler (une seule execution du script, cf. lp_launcher.py). Ce module fournit
l'interface qui permet de designer les cibles a la souris, et la configuration transmise au
lanceur ({fichier: [lignes marquees]}, cf. config_lanceur).

Architecture, calquee point par point sur le debogueur de Spyder
(spyder/plugins/debugger/utils/breakpointsmanager.py + panels/debuggerpanel.py) :

1. Etat dans l'editeur : l'attribut `profile_target` est pose sur le BlockUserData du bloc de
   texte, exactement comme `breakpoint` l'est pour les points d'arret. Les blocs Qt suivent
   automatiquement les insertions/suppressions de lignes, donc les marqueurs ne se decalent pas
   quand on edite le fichier. BlockUserData n'a pas de __slots__ (verifie dans
   spyder/plugins/editor/utils/editor.py) : ajouter un attribut est licite. On REUTILISE le
   BlockUserData existant s'il y en a un (`block.userData()`) au lieu d'en creer un nouveau,
   sans quoi on ecraserait le point d'arret et les bookmarks poses sur la meme ligne.
   `getattr(data, 'profile_target', False)` partout en lecture : un BlockUserData cree par un
   autre composant de Spyder (debogueur, bookmarks, analyse de code) n'a pas cet attribut.

2. Persistance : CONF.set('spyder_line_profiler', 'profile_targets', {fichier: [lignes]}), donc
   dans ~/.config/spyder-py3/plugins/spyder_line_profiler/spyder.ini (le plugin declare
   CONF_FILE = True, ses options vivent dans SON PROPRE fichier - pas dans le spyder.ini
   principal, erreur de commentaire corrigee le 02/08/2026 apres qu'elle a fait chercher au
   mauvais endroit en diagnostiquant clear_targets(), cf. DONE du meme jour). Meme forme que
   l'option 'breakpoints' de la section
   [debugger], y compris la normalisation des chemins par osp.normcase (evite d'avoir le meme
   fichier sous deux cles differentes). Les marqueurs survivent donc a la fermeture de Spyder,
   comme les points d'arret.

3. Transmission au lanceur, faite au lancement du profilage (config_lanceur) : les marqueurs
   partent TELS QUELS, {fichier: [lignes]}, dans un JSON que lit lp_launcher.py. C'est le
   lanceur qui resout chaque ligne vers son `def` (meme regle que _function_at_line : une ligne
   n'importe ou dans le corps suffit), sur la source qu'il compile REELLEMENT - les numeros de
   ligne ne peuvent pas diverger de ce qui s'execute. Spyder enregistre tout avant de lancer
   (option 'save_all_before_run', vraie par defaut), donc disque, editeur et lanceur voient la
   meme version.

LIMITE CONNUE, assumee : une fonction imbriquee dans une autre fonction n'est pas ciblable
telle quelle (le lanceur ne decore jamais dans le corps d'une fonction : decorer une closure a
chaque execution du `def` englobant enregistrerait un objet-code different a chaque passage).
Dans ce cas on cible la fonction de premier niveau qui la contient, ce qui profile ses lignes
propres mais pas le corps de la closure. Les methodes de classes, y compris dans des classes
imbriquees, ne sont PAS concernees.
"""

import ast
import os
import os.path as osp
import tempfile
import weakref

from qtpy.QtCore import QObject, Signal

from spyder.config.manager import CONF
from spyder.plugins.editor.api.manager import Manager
from spyder.plugins.editor.utils.editor import BlockUserData


CONF_SECTION = 'spyder_line_profiler'
CONF_OPTION = 'profile_targets'


# ---- Persistance (calquee sur breakpointsmanager.load/save_breakpoints)
# -----------------------------------------------------------------------------
def _dans_dossier_temporaire(filename):
    """Vrai si `filename` est sous un dossier temporaire (/tmp, $TMPDIR, /var/tmp).

    Un fichier disparu de la n'y reviendra jamais (a l'inverse d'un fichier de projet sur un
    volume momentanement non monte) : son marqueur peut donc etre purge sans risque.
    """
    racines = {tempfile.gettempdir(), '/tmp', '/var/tmp'}
    try:
        cible = osp.normcase(osp.abspath(filename))
    except (TypeError, ValueError):
        return False
    return any(cible.startswith(osp.normcase(osp.abspath(r)) + os.sep) for r in racines)


def _load_all_targets():
    """Tous les marqueurs enregistres, sous la forme {chemin normalise: [lignes]}.

    Purge au passage les marqueurs ORPHELINS des dossiers TEMPORAIRES : un fichier temporaire
    marque puis supprime laisserait sinon une entree fantome dans spyder.ini. Inoffensive tant
    que le fichier reste absent (config_lanceur la saute), mais elle pourrait
    « revivre » et declencher le line profiler a tort si un nouveau fichier temporaire reutilisait
    son chemin. On purge aussi les entrees a la liste de lignes vide.

    ⚠ On ne purge QUE les orphelins des dossiers temporaires. Un marqueur sur un fichier de projet
    momentanement indisponible (volume non monte, tampon pas encore charge) est CONSERVE - c'est la
    regle deliberee de "ne rien perdre au chargement" (cf.
    test_un_marqueur_qui_ne_designe_plus_rien_est_conserve). Un fichier temporaire, lui, ne revient
    jamais : le purger est sans risque.

    La purge n'est reecrite dans la CONF que si quelque chose a change, donc au plus une fois : les
    chargements suivants ne trouvent plus d'orphelin et n'ecrivent rien.
    """
    targets = CONF.get(CONF_SECTION, CONF_OPTION, {})
    change = False
    for filename in list(targets.keys()):
        # Ne pas garder le meme fichier sous deux noms differents.
        new_filename = osp.normcase(filename)
        if new_filename != filename:
            lines = targets.pop(filename)
            targets[new_filename] = sorted(
                set(targets.get(new_filename, [])) | set(lines))
            change = True
    for filename in list(targets.keys()):
        orphelin_temporaire = (not osp.isfile(filename)
                               and _dans_dossier_temporaire(filename))
        if not targets[filename] or orphelin_temporaire:
            targets.pop(filename, None)
            change = True
    if change:
        CONF.set(CONF_SECTION, CONF_OPTION, targets)
    return targets


def load_targets(filename):
    """Lignes marquees dans `filename`."""
    return _load_all_targets().get(osp.normcase(filename), [])


def save_targets(filename, linenos):
    """Enregistre les lignes marquees de `filename` (efface l'entree si vide)."""
    targets = _load_all_targets()
    key = osp.normcase(filename)
    if linenos:
        targets[key] = sorted(linenos)
    else:
        # Ne pas laisser s'accumuler des entrees vides dans spyder.ini.
        targets.pop(key, None)
    CONF.set(CONF_SECTION, CONF_OPTION, targets)


# ---- "Un marqueur n'existe que pour un fichier OUVERT dans Spyder"
# -----------------------------------------------------------------------------
# Registre des editeurs Python actuellement ouverts (WeakSet : ne retient pas un editeur detruit).
# Alimente par attach_editor / detach_editor. Sert a (1) purger les marqueurs d'un fichier a la
# fermeture de son onglet, et (2) purger a l'ouverture de Spyder les marqueurs des fichiers non
# reouverts (cf. purger_marqueurs_des_fichiers_fermes).
_OPEN_EDITORS = weakref.WeakSet()


def _filename_editeur(codeeditor):
    """Chemin du fichier d'un editeur, ou None (editeur Qt detruit / sans fichier)."""
    try:
        return getattr(codeeditor, 'filename', None)
    except RuntimeError:  # objet C++ sous-jacent detruit
        return None


def _fichiers_ouverts():
    """Ensemble des chemins normalises actuellement ouverts dans un editeur Python."""
    ouverts = set()
    for editeur in list(_OPEN_EDITORS):
        nom = _filename_editeur(editeur)
        if nom:
            ouverts.add(osp.normcase(nom))
    return ouverts


def _spyder_se_ferme():
    """Vrai si l'application Qt est en train de se fermer (arret de Spyder).

    On ne purge PAS pendant l'arret : sinon la fermeture de tous les onglets effacerait tous les
    marqueurs, alors qu'on veut les conserver pour les fichiers ouverts (restaures au prochain
    demarrage). QApplication.closingDown() est vrai uniquement pendant la sequence de quit.
    """
    try:
        from qtpy.QtWidgets import QApplication
        app = QApplication.instance()
        return app is not None and app.closingDown()
    except Exception:
        return False


def oublier_marqueurs_a_la_fermeture(codeeditor):
    """Purge IMMEDIATE des marqueurs d'un fichier quand on ferme son onglet.

    Appelee par detach_editor. Ne fait rien si (a) Spyder est en train de se fermer, ou (b) le meme
    fichier reste ouvert dans un AUTRE editeur (vue scindee). L'invariant vise : un marqueur
    n'existe que pour un fichier ouvert.
    """
    nom = _filename_editeur(codeeditor)
    if not nom or _spyder_se_ferme():
        return
    key = osp.normcase(nom)
    # Encore ouvert ailleurs (split) ? codeeditor a deja ete retire de _OPEN_EDITORS par detach.
    if key in _fichiers_ouverts():
        return
    if load_targets(nom):
        save_targets(nom, [])   # [] -> supprime l'entree du fichier


def purger_marqueurs_des_fichiers_fermes():
    """A l'OUVERTURE de Spyder (apres restauration de session) : purge les marqueurs de TOUS les
    fichiers non reouverts, pour tenir l'invariant "un marqueur = un fichier ouvert".

    Purge STRICTE, sans garde-fou d'existence : un fichier non ouvert -> son marqueur est supprime,
    point. Choix DELIBERE de l'utilisateur : il prefere risquer de perdre un marquage (ex. si /DATA
    VeraCrypt tarde a monter au demarrage, ses fichiers ne sont pas reouverts -> leurs marqueurs
    sont purges) plutot que de laisser trainer des marqueurs oublies sur des fichiers fermes. A noter
    que Spyder OUBLIE de toute facon lui-meme les fichiers manquants de la session (on_close
    reecrit 'filenames' = fichiers ouverts seulement), donc un marqueur non purge serait de la crasse.
    """
    ouverts = _fichiers_ouverts()
    targets = _load_all_targets()
    change = False
    for filename in list(targets.keys()):
        if filename not in ouverts:
            targets.pop(filename, None)
            change = True
    if change:
        CONF.set(CONF_SECTION, CONF_OPTION, targets)


# ---- Resolution ligne -> fonction (pose des marqueurs dans la marge)
# -----------------------------------------------------------------------------
def _function_at_line(tree, lineno):
    """(nom qualifie, ligne du `def`) de la fonction contenant `lineno`, ou (None, None).

    Parcourt l'arbre en profondeur en empilant les noms des ClassDef traverses et en retenant la
    fonction la plus imbriquee qui contient la ligne. Voir la limite connue documentee en tete
    de module pour les fonctions imbriquees dans des fonctions : `inside_function` fige la
    recherche des qu'on est entre dans une fonction, donc on ne descend jamais dans une closure.

    La ligne du `def` sert a POSER le marqueur : line_profiler ne sait profiler que des fonctions
    entieres, donc un marqueur affiche au milieu d'un corps de fonction laisserait croire que
    seule cette ligne est mesuree. Cliquer n'importe ou dans la fonction reste possible - c'est
    plus tolerant que d'exiger de viser le `def` - mais l'icone remonte sur le `def`.
    """
    found = []

    def visit(node, prefix, inside_function):
        for child in ast.iter_child_nodes(node):
            start = getattr(child, 'lineno', None)
            end = getattr(child, 'end_lineno', None)
            if start is None or end is None or not (start <= lineno <= end):
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, prefix + [child.name], inside_function)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if inside_function:
                    # Closure : on garde la fonction de premier niveau deja trouvee.
                    continue
                # child.lineno est la ligne du `def` meme quand la fonction est decoree : les
                # decorateurs vivent dans decorator_list, avec leurs propres numeros de ligne.
                found.append(('.'.join(prefix + [child.name]), child.lineno))
                visit(child, prefix + [child.name], True)
            else:
                visit(child, prefix, inside_function)

    visit(tree, [], False)
    return found[-1] if found else (None, None)




# ---- Mode "profiler TOUT le code utilisateur" (option, CONF 'profile_all_user')
# -----------------------------------------------------------------------------
# Au lieu de profiler seulement les fonctions marquees, profiler TOUTES les fonctions des modules
# utilisateur du projet (hors stdlib/site-packages). Demande de l'utilisateur. C'est une OPTION,
# desactivee par defaut : line-profiler tout le code utilisateur ajoute ~130 ns par execution de
# ligne sur CHAQUE fonction (mesure), ce qui peut ralentir un run d'un ordre de grandeur sur du
# code a boucles serrees. Le drill-down (carte cProfile -> marquer les fonctions chaudes) reste le
# mode normal, moins couteux.
CONF_OPTION_ALL = 'profile_all_user'


def profile_all_user_actif():
    """Vrai si l'option "profiler toutes les fonctions du code utilisateur" est activee (CONF)."""
    try:
        return bool(CONF.get(CONF_SECTION, CONF_OPTION_ALL, False))
    except Exception:
        return False


def _racine_projet(filename):
    """Racine a explorer : la racine du PAQUET du script (le dossier a mettre sur sys.path pour
    l'importer), donnee par split_modpath ; sinon le dossier du script.

    On N'utilise SURTOUT PAS le depot (.hg/.git) : dans un monorepo il engloberait TOUT le code, y
    compris des scripts sans rapport a effets de bord (constate le 23/07/2026 : en profilant
    HGIGNORED/lp_demo/pkg/maths.py avec la racine = depot SmartOS entier, l'enumeration a pris
    Commun/.../spyder_pyxel/exemples/demo_pyxel.py, que kernprof a PRE-IMPORTE - il lance un jeu au
    chargement, pyxel.init a plante). La racine du paquet du script limite l'enumeration au projet
    reel du script (ex. maths.py dans pkg/ -> racine lp_demo -> main.py + pkg/*.py, pas le depot).
    """
    filename = osp.abspath(str(filename))
    try:
        from line_profiler.autoprofile.util_static import split_modpath
        racine = split_modpath(filename)[0]
        if racine:
            return racine
    except Exception:
        pass
    return osp.dirname(filename)


# ---- Configuration du lanceur lp_launcher.py (remplace la traduction en cibles kernprof)
# -----------------------------------------------------------------------------
# Depuis la refonte du lanceur (24/07/2026, cf. lp_launcher.py), le profilage ne passe plus par
# kernprof : lp_launcher.py recoit un JSON {fichier: [lignes marquees]} et decore lui-meme les
# `def` vises au moment de compiler. Fini donc :
#   - la traduction ligne -> nom pointe ("-p pkg.module.fonction") et ses racines PYTHONPATH ;
#   - le pre-import et la double execution de kernprof (divergence __main__) ;
#   - le ciblage par chemin du fichier lance et le filtre d'affichage qui le compensait.
# La resolution ligne -> `def` se fait DANS le lanceur, sur la source qu'il compile reellement -
# les numeros de ligne ne peuvent pas diverger de ce qui s'execute.

def config_lanceur(filename, log=None):
    """Configuration du lanceur : marqueurs par (fichier, lignes) + mode tout-le-code.

    `filename` est le script lance (sert a calculer la racine du mode "tout le code
    utilisateur"). Un fichier marque disparu est journalise et ignore, jamais bloquant :
    on profile ce qui reste.
    """
    all_user = profile_all_user_actif()
    cibles = {}
    if not all_user:
        for fichier, lignes in sorted(_load_all_targets().items()):
            if not lignes:
                continue
            if not osp.isfile(fichier):
                if log is not None:
                    log(f"{fichier} : introuvable, marqueurs ignores")
                continue
            cibles[fichier] = sorted(lignes)
    racine = _racine_projet(filename) if all_user else None
    if log is not None:
        if all_user:
            log(f"profilage de TOUT le code utilisateur sous {racine}")
        else:
            log(f"marqueurs de profilage : {cibles}")
    return {'targets': cibles, 'all_user': all_user, 'racine': racine}


def ecrire_config_lanceur(filename, chemin, log=None):
    """Ecrit la configuration JSON du lanceur dans `chemin`. Point d'entree de widgets.py."""
    import json
    with open(chemin, 'w', encoding='utf-8') as flux:
        json.dump(config_lanceur(filename, log=log), flux, ensure_ascii=False, indent=1)
    return chemin


# ---- Branchement sur un editeur de code
# -----------------------------------------------------------------------------
def is_python_editor(codeeditor):
    """Vrai si l'editeur porte un fichier Python (meme critere que le debogueur de Spyder).

    Un fichier non Python n'a ni marqueur ni resultat, et `ast` ne saurait pas l'analyser.
    """
    from spyder.plugins.editor.utils.editor import get_file_language
    from spyder.plugins.editor.utils.languages import ALL_LANGUAGES

    if codeeditor.filename is None:
        return False
    language = get_file_language(codeeditor.filename, codeeditor.get_text_with_eol())
    return language.lower() in ALL_LANGUAGES["Python"]


def attach_editor(codeeditor):
    """Installe les deux marges du line profiler sur un editeur.

    Appelee par le bloc injecte dans plugin.py, qui ne fait QUE deleguer ici : toute la logique
    vit dans ce module, recopie a chaque execution du script de patch. Faire evoluer le
    branchement ne demande donc jamais de reecrire du code deja injecte dans un fichier amont.
    """
    from spyder_line_profiler.spyder.profile_results import ProfileResultsManager

    codeeditor.profile_targets_manager = None
    codeeditor.profile_results_manager = None
    if not is_python_editor(codeeditor):
        return

    _OPEN_EDITORS.add(codeeditor)   # cf. "un marqueur = un fichier ouvert"

    # Les resultats d'abord : ce sont eux qui creent et enregistrent la marge de droite, que les
    # marqueurs partagent ensuite.
    resultats = ProfileResultsManager(codeeditor)
    codeeditor.profile_results_manager = resultats
    codeeditor.sig_filename_changed.connect(resultats.set_filename)

    manager = ProfileTargetsManager(codeeditor, panel=resultats.panel)
    codeeditor.profile_targets_manager = manager
    # ProfileTargetsManager.set_filename(str) a exactement la signature de ce signal : on le
    # branche directement, sans slot intermediaire. La methode liee est le meme objet a la
    # connexion et a la deconnexion, donc disconnect() retrouve bien sa cible.
    codeeditor.sig_filename_changed.connect(manager.set_filename)


def detach_editor(codeeditor):
    """Retire les marges quand l'editeur disparait."""
    resultats = getattr(codeeditor, 'profile_results_manager', None)
    if resultats is not None:
        # Indispensable : le bus des resultats est un objet de MODULE, donc il survit a
        # l'editeur. Sans ce detachement, chaque fichier ferme laisserait une connexion vers un
        # widget detruit, que le prochain profilage reveillerait.
        resultats.detach()
        _deconnecte(codeeditor.sig_filename_changed, resultats.set_filename)
        codeeditor.profile_results_manager = None

    manager = getattr(codeeditor, 'profile_targets_manager', None)
    if manager is not None:
        _deconnecte(codeeditor.sig_filename_changed, manager.set_filename)
        codeeditor.profile_targets_manager = None

    # "Un marqueur = un fichier ouvert" : retirer d'abord du registre (pour que le test "encore
    # ouvert ailleurs ?" ne se voie pas lui-meme), puis purger a la fermeture d'onglet.
    _OPEN_EDITORS.discard(codeeditor)
    oublier_marqueurs_a_la_fermeture(codeeditor)


def _deconnecte(signal, slot):
    """Deconnexion tolerante : deja deconnecte, ou objet Qt detruit avant nous."""
    try:
        signal.disconnect(slot)
    except (TypeError, RuntimeError):
        pass


# ---- Gestion des marqueurs pour un editeur donne
# -----------------------------------------------------------------------------
class ProfileTargetsManager(Manager, QObject):
    """Pose/retire les marqueurs de profilage dans un CodeEditor."""

    sig_repaint_targets = Signal()

    def __init__(self, editor, panel=None):
        super().__init__(editor)
        self.filename = editor.filename
        self._target_blocks = {}
        self.targets = []
        # Cache de l'arbre syntaxique, indexe par revision du document (cf. _syntax_tree).
        self._tree_revision = None
        self._tree_cache = None

        # UNE SEULE marge a droite, partagee avec les temps (demande de l'utilisateur : "le
        # bouton doit etre sur la meme colonne que les temps - on n'affiche pas le temps de la
        # ligne de definition d'une fonction"). C'est juste : la ligne du `def` n'est jamais
        # executee au sens du profileur, donc sa case de marge est toujours libre - exactement
        # celle ou le marqueur doit se poser. Deux colonnes cote a cote auraient gaspille de la
        # largeur pour n'etre, chacune, remplie qu'une ligne sur l'autre.
        #
        # Le panneau appartient au ProfileResultsManager, qui est cree en premier par
        # attach_editor() et nous le passe ici. `panel` peut etre None quand le manager est
        # instancie seul (dans un test cible), auquel cas il n'y a simplement pas d'affichage.
        self.panel = panel
        if panel is not None:
            panel.targets_manager = self
            # ⚠ INDISPENSABLE, et oublie lors de la fusion des deux marges : l'ancien
            # ProfileTargetsPanel branchait ce signal dans son on_state_changed(), methode
            # disparue avec lui. Sans cette ligne, poser ou retirer un marqueur ne redessine
            # rien - l'icone n'apparait qu'au premier evenement qui repeint la marge pour une
            # autre raison, typiquement un mouvement de souris. Le clic semble alors sans effet.
            #
            # rafraichir() et non update() : la presence d'un marqueur change la LARGEUR
            # necessaire de la marge (elle doit rester ouverte meme sans resultats, sinon on ne
            # pourrait plus retirer le marqueur faute de pouvoir cliquer dessus).
            self.sig_repaint_targets.connect(panel.rafraichir)

        self.load_targets()
        editor.blockCountChanged.connect(self.targets_changed)

    def set_filename(self, filename):
        """Suit un "enregistrer sous" : deplace les marqueurs vers le nouveau nom."""
        if filename is None or self.filename == filename:
            return
        old_filename = self.filename
        self.filename = filename
        if self.targets:
            save_targets(old_filename, [])
            self.save_targets()

    def _syntax_tree(self):
        """Arbre syntaxique du tampon, mis en cache par revision du document.

        QTextDocument.revision() ne change qu'a une modification reelle du texte : le cache
        reste donc valide tant qu'on ne fait que deplacer la souris. C'est ce qui rend
        supportable l'appel a def_line_for() depuis mouseMoveEvent, appele a chaque pixel
        parcouru dans la marge - sans cela on relancerait ast.parse() sur tout le fichier des
        dizaines de fois par seconde.

        Renvoie None si le tampon n'est pas du Python valide (frappe en cours).
        """
        revision = self.editor.document().revision()
        if revision != self._tree_revision:
            self._tree_revision = revision
            try:
                self._tree_cache = ast.parse(self.editor.toPlainText())
            except (SyntaxError, ValueError):
                self._tree_cache = None
        return self._tree_cache

    def def_line_for(self, line_number):
        """Ligne du `def` de la fonction contenant `line_number`, ou None.

        None signifie "cette ligne n'accepte pas de marqueur" : la marge s'en sert pour ne pas
        proposer d'apercu au survol la ou un clic ne ferait rien.
        """
        tree = self._syntax_tree()
        if tree is None:
            return None
        return _function_at_line(tree, line_number)[1]

    def toggle_target(self, line_number=None, snap=True):
        """Ajoute/retire un marqueur de profilage.

        Le marqueur est toujours pose sur la ligne du `def` de la fonction visee, quelle que
        soit la ligne cliquee : line_profiler ne mesure que des fonctions ENTIERES, jamais une
        ligne isolee, et une icone posee au milieu d'un corps de fonction laisserait croire le
        contraire. Cliquer dans le corps reste accepte (plus tolerant que d'exiger de viser le
        `def`), mais l'icone remonte sur la definition.

        `snap=False` desactive ce recalage : utilise par set_targets(), qui repose des lignes
        deja recalees lues en configuration. Sans cela, un fichier modifie hors de Spyder depuis
        le dernier enregistrement pourrait faire glisser silencieusement un marqueur.
        """
        if not self.editor.is_python_like():
            return
        if line_number is None:
            line_number = self.editor.textCursor().blockNumber() + 1

        if snap:
            # Tampon de l'editeur, pas le fichier du disque : c'est ce que l'utilisateur voit.
            def_line = self.def_line_for(line_number)
            if def_line is None:
                # Hors de toute fonction (ligne vide entre deux fonctions, import, corps de
                # classe...), ou fichier syntaxiquement invalide en cours de frappe : aucune
                # fonction a profiler, on ne pose rien plutot que de poser un marqueur qui ne
                # se resoudrait en rien au lancement.
                return
            line_number = def_line

        block = self.editor.document().findBlockByNumber(line_number - 1)

        data = block.userData()
        if not data:
            # Pas de userData : on en cree un. Le poser sur un bloc n'ecrase rien puisqu'il n'y
            # avait rien.
            data = BlockUserData(self.editor)
            data.profile_target = True
        else:
            # userData existant (point d'arret, bookmark, analyse de code...) : on ne fait que
            # basculer NOTRE attribut, tout le reste est preserve.
            data.profile_target = not getattr(data, 'profile_target', False)

        if data.profile_target:
            self._target_blocks[block.blockNumber()] = block

        block.setUserData(data)
        self.editor.sig_flags_changed.emit()
        self.targets_changed()

    def get_targets(self):
        """Lignes actuellement marquees, en elaguant les blocs detruits."""
        targets = []
        pruned = {}
        for block_id, block in self._target_blocks.items():
            if block.isValid():
                data = block.userData()
                if data and getattr(data, 'profile_target', False):
                    pruned[block_id] = block
                    targets.append(block.blockNumber() + 1)
        self._target_blocks = pruned
        return sorted(targets)

    def clear_targets(self):
        """Retire tous les marqueurs de cet editeur.

        Ne PAS pre-assigner self.targets = [] ici : targets_changed() ne sauvegarde que si
        self.targets differe du get_targets() fraichement recalcule, et une pre-assignation les
        rendrait identiques avant meme la comparaison - save_targets() ne serait alors jamais
        appele, laissant l'ancienne liste dans la configuration alors que l'editeur (et l'icone)
        ont bien retire les marqueurs. C'est targets_changed() qui doit voir l'ancien contre le
        nouveau, donc qui doit rester seul a assigner self.targets.
        """
        for data in self.editor.blockuserdata_list():
            data.profile_target = False
        self._target_blocks = {}
        self.targets_changed()
        self.editor.sig_flags_changed.emit()

    def set_targets(self, linenos):
        """Repose la liste de marqueurs donnee, en la recalant sur les lignes de `def`.

        Le recalage est fait ICI plutot qu'en deleguant a toggle_target(snap=True) parce que la
        regle n'est pas la meme qu'au clic : au clic, une ligne qui ne designe aucune fonction
        ne doit rien poser ; au chargement, elle doit etre CONSERVEE TELLE QUELLE. Un tampon pas
        encore rempli, ou un fichier temporairement invalide, ferait sinon disparaitre tous les
        marqueurs - et targets_changed() les effacerait aussitot de la configuration, donc pour
        de bon. Le repli sur la ligne d'origine rend la migration des anciens marqueurs (poses
        au milieu d'un corps de fonction avant le recalage) sans risque de perte.
        """
        self.clear_targets()
        for lineno in linenos:
            # def_line_for() relit l'arbre mis en cache par revision (un seul parse pour toute la
            # liste) ; il resout la meme chose que l'ancien _def_line_in_source, en une passe.
            def_line = self.def_line_for(lineno)
            self.toggle_target(def_line if def_line is not None else lineno, snap=False)
        self.targets = self.get_targets()

    def targets_changed(self):
        """Enregistre si la liste a reellement change (evite les ecritures inutiles)."""
        targets = self.get_targets()
        if self.targets != targets:
            self.targets = targets
            self.save_targets()
            self.sig_repaint_targets.emit()

    def save_targets(self):
        filename = osp.normpath(osp.abspath(str(self.filename)))
        save_targets(filename, self.targets)

    def load_targets(self):
        self.set_targets(load_targets(self.filename))
