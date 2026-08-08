# -*- coding: utf-8 -*-
"""Resultats du line profiler affiches DANS l'editeur : lignes colorees et temps en marge droite.

Installe par Commun/scripts_installation/spyder_patch/patch_spyder_line_profiler_targets.py dans
site-packages/spyder_line_profiler/spyder/profile_results.py. Ce fichier est la source de
verite (versionne dans le depot) ; la copie dans site-packages est jetable.

Contexte (TODO CachyOS "TODO - Spyder - line profiler.txt") : "que peut-on faire en sorte que
le resultat de profiling s'affiche directement dans l'editeur de code, en colorant directement
les lignes dans l'editeur et ajoutant des temps dans la marge a droite ?"

Jusqu'ici les resultats ne vivaient que dans le panneau Line Profiler : pour savoir quelle
ligne coute cher, il fallait lire un tableau a cote du code au lieu de le voir sur le code.

OU SONT LES RESULTATS (troisieme item du meme TODO, repondu ici)
    ~/.config/spyder-py3/lineprofiler.results, chemin construit par get_conf_path() dans
    widgets.py (constante DATAPATH). C'est le fichier que kernprof ecrit via son option "-o",
    un pickle de line_profiler.LineStats :

        lstats.unit      facteur de temps (1e-09 sur cette machine, donc des nanosecondes)
        lstats.timings   {(fichier, ligne_du_def, nom_fonction):
                            [(ligne, hits, temps_en_unites), ...]}

    ⚠ Ce fichier est ECRASE a chaque profilage - il ne contient que le dernier. C'est
    precisement ce qui rend l'item "garder un historique" non trivial, et il faudra passer par
    des copies datees.

CE QUE FAIT CE MODULE
    1. publish() relit ce pickle et le range dans un registre en memoire, indexe par fichier
       puis par ligne. Un bus Qt previent tous les editeurs ouverts qu'il y a du nouveau.
    2. ProfileResultsManager, un par editeur de code, peint deux choses :
       - le FOND des lignes mesurees, d'autant plus soutenu que la ligne pese lourd dans sa
         fonction. Par editor.decorations.add_key() et JAMAIS setExtraSelections(), qui
         remplacerait toute la liste et effacerait les decorations de Spyder (occurrences,
         ligne courante, marqueurs LSP) ;
       - une marge a DROITE avec le temps de chaque ligne.
    3. Une infobulle par ligne donne le detail : hits, temps total, temps par hit, pourcentage.

UNE ECHELLE THERMIQUE, PAS UNE COULEUR PAR FONCTION
    Demande explicite de l'utilisateur : "je ne veux pas une couleur par fonction, juste du
    rouge avec le meme degrade noir - violet - rouge - orange - jaune que pour les cameras
    thermiques". C'est l'echelle "ironbow" des cameras infrarouges, et elle se lit sans legende
    parce que tout le monde la connait : sombre = froid, orange = brulant. Elle s'arrete
    volontairement avant le jaune (cf. ECHELLE_THERMIQUE).

    Ce que ce choix apporte par rapport a mes deux versions precedentes :
      - contre une teinte unique dont seule l'opacite variait : l'oeil distingue bien plus
        finement une TEINTE qu'un niveau de transparence. Sur une echelle a une seule couleur,
        40 % et 60 % se ressemblent ; ici l'un est rouge et l'autre orange ;
      - contre une couleur par fonction (celles du panneau) : la couleur y servait a identifier
        la fonction, pas a dire le cout - or c'est le cout qu'on cherche en regardant le code.

    L'opacite ne fait plus que monter legerement avec le cout, pour que les lignes chaudes
    accrochent l'oeil ; c'est bien la teinte qui porte l'information.

CE QUI N'EST PAS COLORE
    Une ligne sans mesure (jamais executee, ou hors des fonctions profilees) ne recoit ni fond
    ni texte en marge. L'absence de couleur veut donc dire "pas de mesure", jamais "rapide" -
    distinction importante : une ligne a 0 % est mesuree et sombre (le froid de l'echelle), une
    ligne non executee n'est pas coloree du tout.
"""

import linecache
import math
import os
import os.path as osp
import pickle

from qtpy.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, Signal
from qtpy.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen


from spyder.plugins.editor.api.decoration import TextDecoration, DRAW_ORDERS
from spyder.plugins.editor.api.panel import Panel
from spyder.utils.icon_manager import ima


# Echelle thermique, inspiree de celle des cameras infrarouges ("ironbow") : du fond de
# l'editeur pour le froid a l'orange pour le chaud, en passant par violet et rouge.
#
# L'interet par rapport a une teinte unique dont seule l'opacite varierait : l'oeil distingue
# bien plus finement une TEINTE qu'un niveau de transparence. Sur une echelle a une seule
# couleur, 40 % et 60 % se ressemblent ; ici l'un est rouge et l'autre orange.
#
# Chaque entree est (position sur l'echelle, rouge, vert, bleu). Le palier a 0 vaut None : c'est
# la COULEUR DE FOND DE L'EDITEUR, passee a l'execution (demande de l'utilisateur, qui a
# remplace le noir du premier jet). Elle depend du theme, elle ne peut donc pas etre figee ici.
#
# Pourquoi c'est mieux que le noir : une ligne mesuree mais negligeable se fond alors dans
# l'editeur au lieu de l'assombrir. Sur un theme sombre, un aplat noir sur fond gris fonce se
# voyait comme une bande sale, sans rien vouloir dire ; sur un theme clair, il aurait carrement
# masque le code. Le degrade demarre desormais "a rien" et ne monte en couleur que quand la
# ligne commence a couter.
# ⚠ L'echelle S'ARRETE AU ROUGE (demande de l'utilisateur, 23/07/2026 : "que jusqu'au rouge
# plutot que jusqu'a l'orange"). Elle ne monte donc ni a l'orange ni au jaune : ces teintes claires
# de l'ironbow, en aplat derriere du code, ecrasent la coloration syntaxique. Le rouge garde tout le
# contraste utile - fond, violet, rouge se distinguent sans peine - sans rendre la ligne la plus
# interessante du fichier la plus penible a lire.
ECHELLE_THERMIQUE = [
    (0.00, None),             # couleur de fond de l'editeur - mesuree, mais negligeable
    (0.50, (105, 0, 160)),    # violet
    (1.00, (220, 20, 20)),    # rouge  - le plus chaud (demande de l'utilisateur : on s'arrete au
                              # ROUGE, on ne monte plus jusqu'a l'orange)
]


def eclaircir(couleur, force=0.15):
    """Version eclaircie d'une couleur : melange avec du blanc, `force` = part de blanc (0..1).

    Sert a marquer la ligne courante : un gris fonce devient gris clair, une couleur thermique
    devient la meme en plus clair. Le melange additif fonctionne aussi bien sur du sombre (ou
    QColor.lighter(), multiplicatif sur la valeur HSV, ne ferait presque rien) que sur une teinte
    saturee.

    force reglee a 0.15 sur retour de l'utilisateur ("l'eclaircissement est trop important" a
    0.30) : la ligne courante doit ressortir sans se detacher franchement du reste.
    """
    force = max(0.0, min(1.0, force))
    return QColor(round(couleur.red() * (1 - force) + 255 * force),
                  round(couleur.green() * (1 - force) + 255 * force),
                  round(couleur.blue() * (1 - force) + 255 * force))


def couleur_thermique(position, fond=None):
    """Couleur opaque de l'echelle thermique a `position` (0 = froid, 1 = chaud).

    `fond` est la couleur du palier froid, c'est-a-dire celle du fond de l'editeur. Noir a
    defaut, ce qui ne sert qu'aux tests appelant la fonction sans editeur sous la main.

    Interpolation lineaire entre les paliers, canal par canal.
    """
    fond = QColor(fond) if fond is not None else QColor(0, 0, 0)
    paliers = [(pos, rvb if rvb is not None else (fond.red(), fond.green(), fond.blue()))
               for pos, rvb in ECHELLE_THERMIQUE]

    position = max(0.0, min(1.0, position))
    for (debut, (r1, v1, b1)), (fin, (r2, v2, b2)) in zip(paliers, paliers[1:]):
        if position <= fin:
            # Position relative dans ce palier. Les paliers sont d'ecart non nul par
            # construction, la division est donc sure.
            t = (position - debut) / (fin - debut)
            return QColor(int(r1 + (r2 - r1) * t),
                          int(v1 + (v2 - v1) * t),
                          int(b1 + (b2 - b1) * t))
    return QColor(*paliers[-1][1])


# Cle sous laquelle nos decorations vivent dans editor.decorations. Nommee, donc retirable sans
# toucher a celles des autres.
DECORATION_KEY = 'smartos_profile_results'

# Configuration : la colonne des temps a DEUX etats, choisis a la souris - formule complete
# (hits × par_passage = total) ou temps total seul. On enregistre ce choix, pas une largeur en
# pixels : chaque etat dimensionne la colonne EXACTEMENT a son contenu (une largeur libre
# laisserait du vide ou tronquerait selon la police et le zoom). Meme section que les marqueurs
# (le greffon a CONF_FILE = True) : ~/.config/spyder-py3/plugins/spyder_line_profiler/spyder.ini.
CONF_SECTION = 'spyder_line_profiler'
COMPACT_OPTION = 'results_column_compact'


def charger_mode_compact():
    """Vrai si l'utilisateur a choisi la colonne compacte (temps total seul). Faux par defaut.

    Tolerant a l'absence de configuration (tests hors Spyder).
    """
    try:
        from spyder.config.manager import CONF
        return bool(CONF.get(CONF_SECTION, COMPACT_OPTION, False))
    except Exception:
        return False


def sauver_mode_compact(compact):
    """Enregistre le choix compact/complet. Silencieux si la configuration est inaccessible."""
    try:
        from spyder.config.manager import CONF
        CONF.set(CONF_SECTION, COMPACT_OPTION, bool(compact))
    except Exception:
        pass


# TROISIEME etat, ajoute le 26/07/2026 a la demande de l'utilisateur : « cacher resultats et
# colorations de lignes si on glisse la limite jusqu'aux horloges, pour n'afficher plus que les
# horloges cochees ». La colonne se reduit alors a la largeur d'une icone, aucun temps n'est ecrit,
# et LA COLORATION DES LIGNES DE L'EDITEUR EST RETIREE - c'est cette derniere qui fait tout
# l'interet de l'etat : on retrouve un editeur normal en gardant les marqueurs de profilage.
#
# ⚠ UNE SECONDE OPTION BOOLEENNE plutot qu'une option unique a trois valeurs : le choix
# compact/complet est deja enregistre chez l'utilisateur sous COMPACT_OPTION. Le remplacer par une
# chaine perdrait son reglage en silence a la premiere lecture. Les deux booleens se lisent comme un
# etat a trois valeurs dans _mode, ou HORLOGES a la priorite.
HORLOGES_OPTION = 'results_column_clocks_only'


def charger_horloges_seules():
    """Vrai si l'utilisateur a replie la colonne jusqu'aux seules horloges. Faux par defaut."""
    try:
        from spyder.config.manager import CONF
        return bool(CONF.get(CONF_SECTION, HORLOGES_OPTION, False))
    except Exception:
        return False


def sauver_horloges_seules(horloges):
    """Enregistre le repli sur les horloges. Silencieux si la configuration est inaccessible."""
    try:
        from spyder.config.manager import CONF
        CONF.set(CONF_SECTION, HORLOGES_OPTION, bool(horloges))
    except Exception:
        pass

# Registre en memoire : {chemin normalise: {ligne: LigneMesuree}}. Rempli par publish(), lu par
# chaque ProfileResultsManager. En memoire seulement : le pickle sur disque reste la source.
_RESULTS = {}

# Vrai des que le fichier de resultats du disque a ete lu (une fois par processus).
_CHARGE = False


class LigneMesuree:
    """Mesures d'une ligne. `percent` est relatif au temps total de SA fonction.

    `kind` distingue deux origines, colorees sur la MEME echelle log (cf. profile_cprofile) :
      - 'line' : une ligne mesuree par le line profiler (fonction marquee). hits/per_hit/percent
        sont renseignes, cumtime/tottime/ncalls valent None.
      - 'def'  : la ligne du `def` d'une fonction, coloree par son cumtime cProfile (coeur de la
        coloration "tout le code"). cumtime/tottime/ncalls sont renseignes, hits/per_hit/percent
        valent None. La valeur qui pilote la couleur (`seconds`) porte alors le cumtime.
    """

    __slots__ = ('hits', 'seconds', 'per_hit', 'percent', 'function', 'heat',
                 'cumtime', 'tottime', 'ncalls', 'kind')

    def __init__(self, hits, seconds, per_hit, percent, function, heat=0.0,
                 cumtime=None, tottime=None, ncalls=None, kind='line'):
        self.hits = hits
        self.seconds = seconds
        self.per_hit = per_hit
        # `percent` est la vraie part du temps de la fonction : c'est ce qu'on affiche.
        self.percent = percent
        self.function = function
        # cProfile (pour une ligne 'def') : cumule, temps propre, nombre d'appels - infobulle.
        self.cumtime = cumtime
        self.tottime = tottime
        self.ncalls = ncalls
        self.kind = kind
        # `heat` est ce meme cout ramene a la ligne la plus chere de TOUT LE PROFILAGE - toutes
        # les fonctions selectionnees confondues - et c'est lui qui choisit la couleur.
        #
        # Normalisation GLOBALE et non par fonction (demande explicite de l'utilisateur, apres
        # une premiere version par fonction) : la couleur dit alors le poids reel dans le temps
        # total, et non un classement interne a chaque fonction. Une fonction bon marche reste
        # donc sombre de bout en bout, au lieu d'afficher un faux point chaud sur sa ligne la
        # moins insignifiante. C'est exactement ce que montre une camera thermique d'une scene :
        # une echelle unique pour tout le champ.
        #
        # Elle reste indispensable sous une forme ou une autre : sans normalisation du tout, le
        # pourcentage d'une ligne plafonne a la part qu'elle occupe dans sa fonction - deux
        # lignes qui se partagent le temps ne depassent pas 50 %, donc le rouge, et ni l'orange
        # ni l'orange ne sortiraient JAMAIS (constate a l'ecran sur la demo).
        self.heat = heat


class _Bus(QObject):
    """Previent les editeurs ouverts qu'un nouveau profilage est disponible.

    Un objet de module plutot qu'un signal porte par le greffon : les managers sont crees par
    editeur, bien apres le greffon, et n'ont aucun moyen simple de remonter jusqu'a lui.
    """

    sig_results_changed = Signal()

    # ⚠ SIGNAL DISTINCT, et il ne peut PAS etre remplace par le precedent (26/07/2026).
    # sig_results_changed est emis dans TROIS cas - nouveau profilage line profiler, nouveau
    # cProfile, et EFFACEMENT des resultats - et le manager y branche son refresh(). Or refresh()
    # est aussi appele par le changement d'etat de la colonne lui-meme : s'en servir pour
    # redeployer la colonne ferait redeployer AUSSITOT toute tentative de repli, et l'utilisateur
    # ne pourrait plus jamais replier. Celui-ci n'est emis QUE pour de vrais nouveaux resultats.
    sig_nouveaux_resultats = Signal()


BUS = _Bus()


def format_duree(seconds):
    """Duree lisible et courte, unite adaptee a l'ordre de grandeur.

    Une marge d'editeur est etroite : on prefere "6.6 ms" a "0.006644 s". Trois chiffres
    significatifs suffisent pour comparer des lignes entre elles, ce qui est le seul usage.
    """
    if seconds is None:
        return ''
    if seconds >= 1.0:
        return f'{seconds:.2f} s'
    if seconds >= 1e-3:
        return f'{seconds * 1e3:.1f} ms'
    if seconds >= 1e-6:
        return f'{seconds * 1e6:.1f} us'
    return f'{seconds * 1e9:.0f} ns'


def _cprofile_path():
    """Chemin du .prof cProfile ecrit par le run COMBINE (cf. WIDGETS_LAUNCHER_WRAP du patch)."""
    from spyder.config.base import get_conf_path
    return get_conf_path('lineprofiler_cprofile.prof')


def load_results(path):
    """Lit le pickle du line profiler ET le .prof cProfile ; renvoie {chemin: {ligne: LigneMesuree}}.

    DEUX couches, colorees sur la MEME echelle log normalisee sur `vmax` = le plus gros cumtime du
    CODE UTILISATEUR (user_reference ; PAS total_tt, gonfle par la machinerie de kernprof) :
      - lignes du line profiler (corps des fonctions marquees) ;
      - ligne du `def` de chaque fonction du .prof, coloree par son cumtime.
    Contrainte de coherence : la ligne "appeler B" d'une fonction marquee vaut le cumtime de B (le
    temps/ligne du line profiler inclut les sous-appels), donc a la meme couleur que le `def` de B.

    REPLI sans .prof (vmax <= 0) : comportement d'origine du line profiler (echelle LINEAIRE sur la
    ligne la plus chere, pas de coloration de `def`) - le line profiler marche donc seul.

    Renvoie {} si aucune source lisible : un resultat manquant ne doit jamais empecher un editeur
    de s'ouvrir.
    """
    # Import tardif et tolerant (module pur, installe a cote ; peut manquer d'un vieux site-packages).
    try:
        from spyder_line_profiler.spyder.profile_cprofile import (
            read_prof, log_heat, def_values_for_file, user_reference)
    except ImportError:
        read_prof = log_heat = def_values_for_file = user_reference = None

    try:
        with open(path, 'rb') as flux:
            lstats = pickle.load(flux)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
        lstats = None

    prof_stats = {}
    if read_prof is not None:
        try:
            prof_stats, _total_tt = read_prof(_cprofile_path())
        except Exception:
            prof_stats = {}
    # HAUT DE L'ECHELLE = plus gros cumtime du CODE UTILISATEUR, PAS total_tt (gonfle par la
    # machinerie de kernprof, ce qui eteignait la coloration - cf. user_reference).
    vmax = user_reference(prof_stats) if (user_reference is not None and prof_stats) else 0.0
    floor = vmax * 1e-3 if vmax > 0 else 0.0
    en_log = vmax > 0 and log_heat is not None

    if lstats is None and not prof_stats:
        return {}

    resultats = {}
    # linecache garde en cache le contenu des fichiers ; sans cette invalidation, un fichier
    # modifie depuis le dernier profilage serait relu dans sa version perimee.
    linecache.checkcache()

    # --- Couche line profiler (corps des fonctions marquees).
    if lstats is not None:
        unit = getattr(lstats, 'unit', 1e-6)
        # Repli lineaire (sans .prof) : la ligne la plus chere fixe le haut de l'echelle.
        pire_globale = max(
            (temps for timings in lstats.timings.values() for _l, _h, temps in timings),
            default=0) * unit
        # Pas de filtre d'affichage : depuis la refonte du lanceur (lp_launcher.py, 24/07/2026),
        # seules les fonctions effectivement visees sont instrumentees - les timings contiennent
        # exactement les fonctions marquees (ou tout le code utilisateur si l'option est active).
        # L'ancien filtre compensait le ciblage par chemin de kernprof, qui profilait TOUTES les
        # fonctions du fichier lance (rev. 375, retiree).
        for (filename, _start_line, funcname), timings in lstats.timings.items():
            if not timings:
                continue
            # Le pourcentage est relatif a la fonction (question "quelle ligne coute cher ICI").
            total = sum(temps for _ligne, _hits, temps in timings) * unit
            par_fichier = resultats.setdefault(osp.normcase(filename), {})
            for ligne, hits, temps in timings:
                secondes = temps * unit
                heat = (log_heat(secondes, vmax, floor) if en_log
                        else ((secondes / pire_globale) if pire_globale else 0.0))
                par_fichier[ligne] = LigneMesuree(
                    hits=hits, seconds=secondes, per_hit=secondes / hits if hits else None,
                    percent=(secondes / total) if total else 0.0, function=funcname,
                    heat=heat, kind='line')

    # --- Couche cProfile (ligne du `def` de chaque fonction). Seulement avec un .prof.
    if en_log and prof_stats and def_values_for_file is not None:
        par_prof = {}
        for cle, valeur in prof_stats.items():
            if cle[0]:
                par_prof.setdefault(cle[0], {})[cle] = valeur
        for filename, fstats in par_prof.items():
            defs = def_values_for_file(filename, fstats)
            if not defs:
                continue
            par_fichier = resultats.setdefault(osp.normcase(filename), {})
            for def_line, info in defs.items():
                # cProfile enregistre le corps des CLASSES comme un code object (co_name = nom de
                # la classe) : sa ligne est "class X:", pas un `def`. On ne colore que les VRAIES
                # lignes de `def` de fonction/methode. linecache lit la version DISQUE (celle que le
                # profilage a executee), donc coherente avec le .prof. Ligne absente/illisible ->
                # chaine vide -> ecartee (sur).
                ligne_src = linecache.getline(filename, def_line).lstrip()
                if not (ligne_src.startswith('def ') or ligne_src.startswith('async def ')):
                    continue
                # Ne pas ecraser une ligne du line profiler : le line profiler ne mesure pas la
                # ligne du `def` (jamais executee), donc en principe pas de collision - mais on se
                # protege d'un fichier edite depuis le run.
                if def_line in par_fichier:
                    continue
                par_fichier[def_line] = LigneMesuree(
                    hits=None, seconds=info['cumtime'], per_hit=None, percent=None,
                    function=info['function'], heat=log_heat(info['cumtime'], vmax, floor),
                    cumtime=info['cumtime'], tottime=info['tottime'], ncalls=info['ncalls'],
                    kind='def')
    return resultats


def _archive_run(path):
    """Archive le profilage `path` dans l'historique versionne de chaque fichier profile.

    Reponse a la premiere question de la section "historique" du TODO : apres chaque profilage,
    un run est ecrit dans <racine du depot>/.profiler/<chemin relatif du fichier>/ (cf.
    profile_history, decisions du 22/07/2026). On deserialise ici le MEME pickle que
    load_results, et on delegue le reste a profile_history.archive_lstats, qui ne depend ni de Qt
    ni de Spyder et est teste a part.

    Silencieux et sans consequence en cas d'echec : l'historique est un plus, il ne doit JAMAIS
    empecher l'affichage des resultats. Import tardif et tolerant (le module peut manquer d'un
    vieux site-packages non repatche).
    """
    try:
        from spyder_line_profiler.spyder.profile_history import archive_lstats
    except ImportError:
        return
    try:
        with open(path, 'rb') as flux:
            lstats = pickle.load(flux)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
        return
    try:
        archive_lstats(lstats)
    except Exception:
        # archive_lstats est deja tolerant (hors depot -> rien), mais rien ne doit remonter ici.
        pass


def publish(path):
    """Recharge les resultats depuis `path`, archive le run, et previent les editeurs ouverts."""
    global _RESULTS, _CHARGE
    _RESULTS = load_results(path)
    # Enregistre ce profilage dans l'historique versionne (cf. _archive_run). Fait ici, au meme
    # instant que la lecture, pour archiver exactement ce qui est publie.
    _archive_run(path)
    # Deja charge, et par une source plus fraiche que le disque : ensure_loaded() ne doit plus
    # rien faire, sans quoi elle ecraserait ceci au premier editeur ouvert ensuite.
    _CHARGE = True
    BUS.sig_results_changed.emit()
    # Apres sig_results_changed : les donnees sont en place, la colonne peut se redeployer dessus.
    BUS.sig_nouveaux_resultats.emit()
    return _RESULTS


def publish_cprofile(prof_buffer):
    """Alimente la def-heatmap de l'editeur depuis un run cProfile SEUL (F10/bouton sans marqueur).

    POURQUOI. Quand AUCUNE fonction n'est marquee, le bouton "Profiler le fichier" ne lance pas le
    line profiler : il fait un cProfile in-noyau (Profiler.profile_file -> console.exec_files). Ce
    chemin ne passe jamais par le widget line profiler, donc n'appelait jamais publish() -> la marge
    de droite de l'editeur restait VIDE, alors qu'on veut y voir le cumtime de chaque fonction a sa
    ligne de `def` (la def-heatmap, "colorer TOUT le code depuis cProfile"). On se greffe donc sur
    l'arrivee du resultat cProfile (ProfilerSubWidget.show_profile_buffer, patche par
    patch_spyder_profiler_reroute.py) et on publie la couche def-heatmap.

    COMMENT. On ecrit le .prof recu la ou load_results le lit (_cprofile_path()), puis on publie
    avec des resultats de LIGNES vides (os.devnull) : seule la couche cProfile colore, soit un
    chiffre par fonction a sa ligne de `def`. Pas de temps par ligne (cProfile seul n'en a pas ;
    ceux-la restent la specialite du line profiler, sur fonctions marquees).

    Un run COMBINE (avec marqueurs) ne passe PAS par ici : il tourne en sous-process kernprof et
    publie ses DEUX couches (lignes + def) via widgets.py. Aucun conflit.
    """
    global _RESULTS, _CHARGE
    if not prof_buffer:
        return None
    try:
        with open(_cprofile_path(), 'wb') as flux:
            flux.write(prof_buffer)
    except OSError:
        return None
    # os.devnull en guise de "resultats de lignes" : lstats reste None (cProfile seul), et
    # load_results lit le .prof ecrit ci-dessus pour la couche def-heatmap.
    _RESULTS = load_results(os.devnull)
    _CHARGE = True
    BUS.sig_results_changed.emit()
    # Un profilage cProfile est un nouveau resultat comme un autre : la colonne repliee se redeploie.
    BUS.sig_nouveaux_resultats.emit()
    return _RESULTS


def default_results_path():
    """Chemin du fichier de resultats, celui-la meme que le panneau Line Profiler utilise.

    Lu sur la classe du widget plutot que reconstruit : si le plugin change un jour de nom de
    fichier, on suit automatiquement. L'import est tardif et rattrape, car widgets.py importe
    lui-meme ce module - un import en tete provoquerait un cycle.
    """
    try:
        from spyder_line_profiler.spyder.widgets import SpyderLineProfilerWidget
        return SpyderLineProfilerWidget.DATAPATH
    except Exception:
        from spyder.config.base import get_conf_path
        return get_conf_path('lineprofiler.results')


def ensure_loaded():
    """Charge les resultats du disque une seule fois, a la premiere demande.

    Sans cela, rouvrir Spyder afficherait un editeur vierge alors que le dernier profilage est
    toujours sur le disque : il faudrait relancer un profilage pour revoir des couleurs qu'on
    avait sous les yeux avant de fermer. Le chargement est PARESSEUX (au premier editeur Python
    ouvert) et non fait au demarrage du greffon, pour ne pas allonger le lancement de Spyder -
    qui fait deja l'objet d'un chantier a part.
    """
    global _CHARGE
    if _CHARGE:
        return
    _CHARGE = True
    resultats = load_results(default_results_path())
    if resultats:
        _RESULTS.update(resultats)


def results_for(filename):
    """Mesures du fichier donne, sous la forme {ligne: LigneMesuree}."""
    if not filename:
        return {}
    ensure_loaded()
    return _RESULTS.get(osp.normcase(str(filename)), {})


def clear():
    """Oublie les resultats (utilise par le bouton d'effacement du panneau)."""
    global _RESULTS, _CHARGE
    _RESULTS = {}
    # ⚠ Sans ce drapeau, l'effacement serait annule des l'editeur suivant : ensure_loaded()
    # rechargerait le fichier du disque, que le bouton d'effacement ne supprime pas.
    _CHARGE = True
    BUS.sig_results_changed.emit()


class _EspaceurPanel(Panel):
    """Panneau INERTE de quelques pixels, qui fabrique une marge la ou Qt n'en offre aucune par cote.

    ⚠ POURQUOI UN PANNEAU POUR SI PEU. Les trois leviers du document ont ete mesures le 26/07/2026,
    et deux sur trois ne servent a rien :
      - documentMargin fonctionne, mais il est SYMETRIQUE : impossible d'ouvrir la gauche sans
        ouvrir la droite ;
      - les marges de CADRE sont IGNOREES (declarees gauche 4, haut 4, bas 4 : curseur du debut de
        document a l'abscisse 0) ;
      - les marges de BLOC aussi (declaree gauche 12 : curseur toujours a 0).
    QPlainTextDocumentLayout, la mise en page de tout editeur de Spyder, est une mise en page
    simplifiee qui n'honore ni cadres ni blocs. Le SEUL reglage reellement par cote est la LARGEUR
    DES PANNEAUX, dont Spyder deduit les marges du viewport, cote par cote.
    D'ou ce panneau : il donne, A GAUCHE seulement, l'espace qui manquait entre le code et la marge de
    pliage. Un second exemplaire a brievement servi a droite, pour separer le code de la colonne des
    temps ; l'utilisateur l'a fait retirer le 26/07/2026 - il ne veut aucune marge de ce cote, meme
    quand des temps sont affiches. La classe reste ecrite pour les deux cotes : c'est le SEUL moyen
    d'obtenir une marge par cote, et le besoin peut revenir.

    Il peint le FOND DE L'EDITEUR et non celui des gouttieres : Panel.paintEvent utilise
    sideareas_color, ce qui ferait lire la bande comme un prolongement de la marge au lieu d'un
    blanc tournant.
    """

    def __init__(self, largeur=0):
        Panel.__init__(self)
        self._largeur = largeur
        # Inerte : aucun clic ne doit lui etre pris, ni a la poignee de la colonne des temps ni au
        # code.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def regler_largeur(self, largeur):
        """Change la largeur. Renvoie True si elle a bouge - a l'appelant de rafraichir les marges."""
        if largeur == self._largeur:
            return False
        self._largeur = largeur
        self.updateGeometry()
        return True

    def sizeHint(self):
        return QSize(self._largeur, self._largeur)

    def paintEvent(self, event):
        if self._largeur and self.editor is not None:
            QPainter(self).fillRect(event.rect(), self.editor.palette().base().color())


class ProfileResultsManager(QObject):
    """Peint les mesures du dernier profilage dans un editeur de code."""

    #: Espace, en pixels, entre le code et ce qui l'encadre : a gauche la marge de pliage, a droite
    #: la colonne des temps. Reprend la marge par defaut de Qt (documentMargin valant 4), pour que
    #: l'editeur respire comme partout ailleurs.
    ESPACE_CODE = 4

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.filename = editor.filename
        self.lignes = {}

        self.panel = ProfileResultsPanel(self)
        editor.panels.register(self.panel, Panel.Position.RIGHT)
        # ⚠ Les panneaux de DROITE sont places du bord de la fenetre vers l'interieur, par
        # order_in_zone DECROISSANT (PanelsManager.resize : sort(reverse=True) puis empilement
        # depuis crect.right()). Le rang le plus BAS finit donc COLLE au code.
        #
        # register() attribue le rang suivant disponible, soit 1 ici, la barre des drapeaux de
        # defilement de Spyder (ScrollFlagArea) ayant pris le 0 a la creation de l'editeur. Notre
        # marge se retrouvait alors a l'extreme droite, avec cette barre INTERCALEE entre le code
        # et elle - ce que l'utilisateur a vu comme "une marge a gauche" separant les deux fonds
        # colores. Un rang negatif la ramene contre le code : les deux aplats se touchent, et la
        # marge des temps se lit comme le prolongement de la ligne.
        #
        # -2 et non -1 : la marge des marqueurs de profilage prend -1, donc se place juste a
        # notre droite. Ordre final, du code vers le bord : temps, marqueurs, drapeaux.
        self.panel.order_in_zone = -2
        # Panel.on_install() ne fait que reparenter : sans setVisible, la marge existe mais
        # reste invisible (piege deja rencontre avec la marge des marqueurs).
        self.panel.setVisible(True)

        # Espaceurs : la marge par cote que le document ne sait pas donner (cf. _EspaceurPanel).
        # ⚠ Les panneaux de GAUCHE se placent du bord vers l'interieur par order_in_zone
        # DECROISSANT : le rang le plus BAS finit COLLE au code. Ceux de Spyder occupent 0 (pliage),
        # 1 (debogueur) et 2 (numeros de ligne) - mesure -, d'ou -1 pour se glisser entre le pliage
        # et le code.
        self.espaceur_gauche = _EspaceurPanel(self.ESPACE_CODE)
        editor.panels.register(self.espaceur_gauche, Panel.Position.LEFT)
        self.espaceur_gauche.order_in_zone = -1
        self.espaceur_gauche.setVisible(True)

        # Derniere ligne courante traitee par _suivre_curseur, pour ne rien refaire tant que le
        # curseur reste sur la meme ligne.
        self._derniere_ligne_courante = None

        BUS.sig_results_changed.connect(self.refresh)
        # Un nouveau profilage REDEPLOIE la colonne si elle etait repliee sur les horloges (demande
        # de l'utilisateur du 26/07/2026) : on vient justement de demander des temps, les cacher
        # serait absurde. Signal DISTINCT de sig_results_changed, cf. _Bus.
        BUS.sig_nouveaux_resultats.connect(self._redeployer_si_replie)
        # Recalcule la largeur de la colonne quand la taille des caracteres change (zoom
        # Ctrl+molette). Sans cela, la largeur restait figee a sa valeur du dernier profilage :
        # en zoomant, les durees debordaient de la colonne, devenue trop etroite (remarque de
        # l'utilisateur). rafraichir() relit QFontMetrics(editor.font()), donc la police
        # courante, et redimensionne la marge en consequence.
        editor.sig_font_changed.connect(self.panel.rafraichir)
        # Au deplacement du curseur : reposer les decorations (l'eclairci de la ligne courante
        # dans la ZONE DE CODE doit suivre le curseur) ET repeindre la colonne (meme eclairci cote
        # timings). Sans reposer les decorations, seule la colonne suivait le curseur, pas le
        # code.
        editor.cursorPositionChanged.connect(self._suivre_curseur)

        self.refresh()

    def _suivre_curseur(self):
        """Au changement de LIGNE courante : repose les fonds (eclaircissement de la ligne active)
        cote code, et repeint la colonne, EN MEME TEMPS.

        - Ne fait rien si le curseur bouge sans changer de ligne (cursorPositionChanged se
          declenche aussi pour un deplacement horizontal) : inutile de tout reposer a chaque
          frappe.
        - Force la mise a jour IMMEDIATE des decorations. add_key() ne fait que demarrer le timer
          de 15 ms (UPDATE_TIMEOUT) du gestionnaire de Spyder : sans ce flush, le cote CODE se
          repeindrait ~15 ms apres la colonne, d'ou un petit decalage visible de part et d'autre
          du trait de demarcation (remarque de l'utilisateur). On vide le timer et on rend la main
          au meme moment que panel.update(), pour que les deux cotes se repeignent dans la meme
          image.
        """
        ligne = self.ligne_courante()
        if ligne == self._derniere_ligne_courante:
            return
        self._derniere_ligne_courante = ligne

        self._apply_decorations()
        deco = self.editor.decorations
        try:
            deco.update_timer.stop()
            deco._update()
        except (AttributeError, RuntimeError):
            # API interne de Spyder : si elle change, on retombe sur le rafraichissement differe
            # (le decalage reapparait, mais rien ne casse).
            pass
        self.panel.update()

    def _poser_marges(self):
        """Poser les marges du code : documentMargin a ZERO, et les espaceurs par cote.

        HISTORIQUE, en trois etapes, parce que les deux premieres etaient fausses et que la trace
        evite de les refaire :
          1. documentMargin non nul : symetrique, donc impossible de fermer la droite quand la
             colonne des temps est vide - ce que l'utilisateur demandait ("le documentMargin doit
             revenir a zero s'il n'y a pas/plus de temps a afficher") ;
          2. documentMargin a zero + pilotage des quatre cotes par le CADRE RACINE du document.
             ⚠ INOPERANT : QPlainTextDocumentLayout ignore les marges de cadre. Le code etait donc
             colle aux numeros de ligne, ce que l'utilisateur a signale le 26/07/2026, alors que le
             commentaire d'alors affirmait "la gauche est preservee" - jamais verifie a l'ecran, et
             le test relisait la valeur qu'il venait d'ecrire ;
          3. celle-ci : documentMargin a zero, et la marge de GAUCHE seule, portee par un panneau
             inerte de quelques pixels (cf. _EspaceurPanel). La largeur des panneaux est le seul
             levier reellement par cote, Spyder en deduisant les marges du viewport.
             ⚠ AUCUNE marge a droite, dans aucun cas : l'utilisateur a tranche le 26/07/2026 contre
             la separation qu'il avait lui-meme retenue plus tot ("sur un fichier avec temps, et je
             n'en veux plus"). La colonne des temps est donc COLLEE au code, ses deux aplats se
             touchant - cf. test_la_colonne_des_temps_est_collee_au_code.

        ⚠ Toute mutation du document leve son drapeau "modifie" : les onglets s'affichaient
        "maths.py*" alors qu'AUCUN caractere n'avait change, et fermer Spyder proposait d'enregistrer
        un fichier intact - risque d'ecraser le vrai contenu. On restaure donc le drapeau.
        """
        document = self.editor.document()
        if document.documentMargin() != 0:
            etait_modifie = document.isModified()
            document.setDocumentMargin(0)
            document.setModified(etait_modifie)

    def set_filename(self, filename):
        """Suit un "enregistrer sous"."""
        if filename and filename != self.filename:
            self.filename = filename
            self.refresh()

    def ligne_courante(self):
        """Numero (base 1) de la ligne surlignee par l'editeur, ou None si le surlignage est
        desactive. Source UNIQUE de cette identite, partagee par les decorations du code et par
        le dessin de la colonne (sinon la meme expression etait dupliquee dans deux classes)."""
        if not getattr(self.editor, 'highlight_current_line_enabled', False):
            return None
        return self.editor.textCursor().blockNumber() + 1

    def detach(self):
        """A appeler quand l'editeur disparait, pour ne pas garder le bus branche dessus."""
        for signal, slot in ((BUS.sig_results_changed, self.refresh),
                             (BUS.sig_nouveaux_resultats, self._redeployer_si_replie),
                             (self.editor.sig_font_changed, self.panel.rafraichir),
                             (self.editor.cursorPositionChanged, self._suivre_curseur)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _redeployer_si_replie(self):
        """Un nouveau profilage sort la colonne du repli sur les horloges - et rien de plus.

        Demande de l'utilisateur du 26/07/2026 : « je veux que le lancement d'un profil ré-agrandisse
        la zone de timing, sur la position intermédiaire », puis « on complète si on était déjà sur la
        vue complète ». La regle est donc exactement : on QUITTE l'etat 'horloges' pour l'etat
        intermediaire, et on ne touche a RIEN d'autre.

        ⚠ Le test porte sur l'etat 'horloges' et non sur le booleen : c'est _mode qui est la source
        unique, et lui seul sait qu'un etat replie sans aucun temps a afficher vaut 'vide' - cas ou il
        n'y a rien a redeployer.
        """
        panneau = getattr(self, 'panel', None)
        if panneau is not None and panneau.horloges_seules():
            panneau._basculer_vers('total')
            # ⚠ ENREGISTRER ICI, explicitement : la persistance du glissement se fait au RELACHEMENT
            # de la souris (sinon elle ecrirait a chaque pixel parcouru), et ce chemin-la ne passe pas
            # par la souris. Sans ces deux lignes, la session suivante relirait 'horloges' et
            # replierait la colonne, alors que l'utilisateur vient de la voir deployee.
            sauver_horloges_seules(False)
            sauver_mode_compact(True)

    def _horloges_seules(self):
        """Vrai si la colonne est repliee sur les horloges - donc aucune coloration de lignes.

        Interroge le panneau, seul detenteur de cet etat, avec un garde-fou : le manager existe
        avant lui dans certains chemins (tests, construction), et une AttributeError ici effacerait
        silencieusement toute la coloration.
        """
        panneau = getattr(self, 'panel', None)
        return bool(panneau is not None and panneau.horloges_seules())

    def refresh(self):
        """Relit le registre et repeint fonds, marge et infobulles."""
        self.lignes = results_for(self.filename)
        self._poser_marges()
        self._apply_decorations()
        self.panel.rafraichir()

    def _apply_decorations(self):
        """Pose le fond des lignes mesurees.

        add_key() et non add() : la cle nous appartient, donc reposer la liste entiere efface
        d'un coup les decorations du profilage precedent, sans jamais toucher a celles des
        autres composants de Spyder.

        ⚠ RIEN N'EST POSE quand la colonne est repliee sur les horloges (demande de l'utilisateur du
        26/07/2026 : « cacher resultats ET colorations de lignes »). On sort AVANT de construire la
        liste - pas apres - pour que le remove_key final s'applique et retire la coloration
        precedente : c'est ce retrait qui rend l'editeur a son aspect normal.
        """
        if self._horloges_seules():
            self.editor.decorations.remove_key(DECORATION_KEY)
            return

        document = self.editor.document()
        fond = self.couleur_de_fond_neutre()
        ligne_courante = self.ligne_courante()
        decorations = []
        for ligne, mesure in self.lignes.items():
            bloc = document.findBlockByNumber(ligne - 1)
            if not bloc.isValid():
                # Le fichier a ete raccourci depuis le profilage : mesure sans ligne, on la
                # laisse tomber plutot que de peindre n'importe ou.
                continue
            if ligne == ligne_courante:
                # Ligne courante MESUREE : couleur eclaircie et OPAQUE, dessinee AU-DESSUS de la
                # surbrillance de ligne courante de l'editeur.
                #
                # ⚠ Cette surbrillance de Spyder passe par le MEME gestionnaire de decorations
                # (set_extra_selections -> decorations.add_key) et a draw_order =
                # DRAW_ORDERS['current_line'] (= 3). Les decorations sont empilees par draw_order
                # CROISSANT (le plus grand est dessine par-dessus). A draw_order 2, notre eclairci
                # passait DESSOUS le gris opaque de l'editeur : le fond restait grise sur la ligne
                # profilee active (remarque de l'utilisateur). On se place donc a current_line + 1
                # pour recouvrir le gris ; opaque, l'eclairci le masque entierement, et le code
                # reste lisible (le texte est peint par-dessus les fonds).
                couleur = eclaircir(self.couleur_de_fond(mesure.heat, fond))
                ordre = DRAW_ORDERS['current_line'] + 1
            else:
                couleur = self.couleur_de_fond(mesure.heat, fond)
                ordre = 1
            decoration = TextDecoration(document, start_line=ligne - 1,
                                        end_line=ligne - 1, draw_order=ordre,
                                        full_width=True)
            decoration.set_background(QBrush(couleur))
            # Infobulle UNIQUEMENT sur les lignes de `def` (choix de l'utilisateur) : elle y porte
            # le "propre / cumule", seule info que la couleur (cumulee) ne peut pas donner. Sur une
            # ligne de corps, la marge affiche deja le temps, l'infobulle ferait doublon.
            if mesure.kind == 'def':
                decoration.tooltip = self.infobulle(ligne, mesure)
            decorations.append(decoration)

        if decorations:
            self.editor.decorations.add_key(DECORATION_KEY, decorations)
        else:
            self.editor.decorations.remove_key(DECORATION_KEY)

    # Bornes d'opacite du fond, sur 255.
    #
    # L'information est portee par la TEINTE (l'echelle thermique), pas par l'opacite : celle-ci
    # ne fait que monter legerement avec le cout, pour que les lignes chaudes accrochent l'oeil.
    #
    # Plancher a 90 : le froid de l'echelle est du NOIR ; trop transparent, il ne se verrait pas
    # sur un fond d'editeur deja sombre, et on confondrait une ligne mesuree a 0 % avec une
    # ligne jamais executee - qui, elle, n'est pas coloree du tout.
    #
    # Plafond a 145 et non 255 : le texte du code est colore par la coloration syntaxique, et un
    # fond opaque rendrait illisibles ses teintes sombres (chaines, commentaires) - d'autant
    # plus que le haut de l'echelle est deja clair. Valeur reglee A L'ECRAN : a 185, le
    # "for i in range(n):" d'une ligne chaude etait deja delave. La teinte porte l'information,
    # l'opacite n'a pas besoin d'etre poussee.
    ALPHA_MIN = 90
    ALPHA_MAX = 145

    def couleur_de_fond_neutre(self):
        """Fond de la ZONE DE CODE (palette base) : fond par defaut de la colonne ET palier froid
        de l'echelle thermique.

        Demande de l'utilisateur : "reprends par defaut la couleur de fond de l'editeur de
        texte". La colonne se lit alors comme un prolongement du code - meme fond -, et une ligne
        profilee monte en couleur par-dessus. Un essai avec le gris des marges (sideareas_color)
        a ete fait puis abandonne.

        Relue a chaque rafraichissement plutot que mise en cache : l'utilisateur peut changer de
        theme sans redemarrer Spyder, et l'echelle doit suivre.
        """
        return self.editor.palette().base().color()

    @classmethod
    def couleur_de_fond(cls, heat, fond=None):
        """Couleur thermique de la ligne : fond, violet, rouge puis orange selon son cout.

        `heat` est le cout de la ligne ramene a la ligne la plus chaude de SA fonction. Dans
        chaque fonction profilee, la ligne la plus couteuse est donc orange et les negligeables
        restent sombres. C'est bien la question qu'on se pose en lisant une fonction - "quelle
        ligne coute cher ICI" - et non une comparaison entre fonctions.
        """
        couleur = couleur_thermique(heat, fond)
        couleur.setAlpha(int(cls.ALPHA_MIN + max(0.0, min(1.0, heat))
                             * (cls.ALPHA_MAX - cls.ALPHA_MIN)))
        return couleur

    @staticmethod
    def infobulle(ligne, mesure):
        """Infobulle d'une ligne de `def` (profilage cProfile).

        Ne s'affiche que sur les `def` (cf. _apply_decorations) : elle y porte le CUMULE (ce qui
        pilote la couleur) ET le PROPRE (tottime), la seule information que la couleur - cumulee -
        ne peut pas donner. C'est ce qui distingue une fonction chere par elle-meme d'un aiguilleur
        chaud seulement par delegation : "propre 2 % / cumule 98 %" = le vrai cout est en dessous.
        """
        parts = [f"{mesure.function} (profilage normal)",
                 f"cumule {format_duree(mesure.cumtime)}",
                 f"propre {format_duree(mesure.tottime)}"]
        if mesure.ncalls:
            parts.append(f"{mesure.ncalls} appel(s)")
        return '\n'.join(parts)


class ProfileResultsPanel(Panel):
    """Marge de DROITE affichant le temps de chaque ligne mesuree."""

    # Retrait du texte des durees par rapport au bord DROIT de la colonne, en pixels.
    #
    # Il vaut aussi le retrait a gauche, puisque la largeur de la colonne est calculee comme
    # "duree la plus large + 2 x PADDING" et que les durees sont calees a droite. Ramene de 6 a
    # 2 sur remarque de l'utilisateur : "l'icone de l'horloge est trop a gauche par rapport aux
    # temps, il faut reduire la marge a gauche pour les temps". L'icone etant calee a x=0, six
    # pixels de retrait la decalaient visiblement des chiffres.
    PADDING = 2

    # Cote de l'icone de marqueur, en pixels. Meme valeur que la marge des points d'arret du
    # debogueur, pour que les deux pastilles aient la meme taille a l'ecran.
    TAILLE_ICONE = 16

    # Largeur de la poignee de redimensionnement, sur le bord GAUCHE de la colonne (cote code).
    # Un clic dans cette bande fait glisser la largeur ; ailleurs, il pose un marqueur.
    POIGNEE = 4

    def __init__(self, results_manager):
        Panel.__init__(self)
        self.results_manager = results_manager
        # Renseigne par ProfileTargetsManager quand il se cable sur cette marge. None tant qu'il
        # n'existe pas : la marge sait alors afficher les temps, mais pas poser de marqueur.
        self.targets_manager = None
        self.line_number_hint = None
        self.setMouseTracking(True)
        self.scrollable = True
        self._largeur = 0

        # Etat compact (temps total seul) ou complet (formule entiere), choisi a la souris et
        # conserve d'une session a l'autre. La colonne est TOUJOURS dimensionnee exactement a son
        # contenu selon cet etat - pas de largeur libre.
        self._compact = charger_mode_compact()
        # Troisieme etat : replie jusqu'aux seules horloges, sans temps ni coloration de lignes.
        self._horloges = charger_horloges_seules()
        self._redimensionne = False
        self._cols = None

        # Meme icone que l'action "Profile file" / "Analyser les performances du fichier" de la
        # barre d'outils (demande de l'utilisateur). C'est l'icone nommee 'profiler' du
        # gestionnaire d'icones de Spyder - celle qu'utilise le profileur integre
        # (SpyderProfiler.get_icon -> create_icon('profiler')). On la relit par ima.icon() plutot
        # que de coder une icone qtawesome en dur : si le theme de Spyder la change, le marqueur
        # suit. L'apercu au survol reutilise la meme icone, peinte a opacite reduite.
        self._icone = ima.icon('profiler')

    # Ecart horizontal entre deux sous-colonnes voisines.
    ECART_COLONNES = 5

    # Geometrie du filet de demarcation, en PIXELS PHYSIQUES, relative a la premiere colonne
    # physique entierement contenue dans le panneau (cf. _dessiner_demarcation). Ces deux valeurs
    # ne se devinent pas : elles ont ete choisies par BALAYAGE, en comptant les colonnes physiques
    # reellement peintes sur 24 largeurs d'editeur a l'echelle 1,3 (cf. _dessiner_demarcation).
    # Les modifier sans refaire cette mesure fait revenir soit le trait qui clignote, soit le
    # trait qui fait tantot 1 tantot 2 px.
    #
    # Largeur : 1.0 px physique donnait 2 colonnes peintes dans 7 cas sur 24 ; 0.5 en donnait 0
    # (trait invisible) dans 10 cas. 0.75 est la seule valeur qui tienne.
    # Decalage : a 0.75 de large, la bande CONTIGUE qui donne toujours 1 colonne va de 0.05 a
    # 0.35 (0.0 et 0.40 echouent). On prend son MILIEU, 0.20, pour avoir 0.15 de marge de chaque
    # cote plutot que de se poser au bord d'une valeur qui passe.
    DEMARCATION_DECALAGE_PX = 0.20
    DEMARCATION_LARGEUR_PX = 0.75

    # Cinq sous-colonnes : hits, ×, temps par passage, =, temps total. Alignement de chacune.
    # Les NOMBRES sont cales a droite (leurs chiffres et unites s'empilent), les OPERATEURS sont
    # centres dans leur propre colonne (× et = forment alors deux colonnes verticales nettes).
    #
    # Cinq colonnes plutot que trois (question posee par l'utilisateur) : a trois - "hits ×" /
    # "per_hit" / "= total" - le × et le = restaient colles aux nombres et ne s'alignaient pas
    # d'une ligne a l'autre. Isoles dans leur colonne, ils s'alignent, et l'ensemble se lit comme
    # un tableau d'equations.
    _ALIGNEMENTS = (Qt.AlignRight, Qt.AlignHCenter, Qt.AlignRight, Qt.AlignHCenter, Qt.AlignRight)

    @staticmethod
    def _segments(mesure):
        """Les cinq morceaux de la formule : hits, ×, temps par passage, =, temps total.

        Demande de l'utilisateur : afficher "hits x temps_par_passage = temps_total" plutot que
        le seul temps total. Le temps par passage et le total passent par format_duree, qui
        choisit l'unite selon l'ordre de grandeur : les deux peuvent donc s'afficher dans des
        unites differentes (p. ex. "0.1 us" par passage, "30.0 ms" au total), ce qui est
        justement l'information utile.

        Ligne de `def` (mesure cProfile) : MEME format, mais avec les grandeurs de la fonction
        entiere - n = nombre d'appels, temps_par_passage = cumtime / n, temps_total = cumtime.
        Demande de l'utilisateur (23/07/2026) : "le meme type d'affichage pour les fonctions que
        pour les lignes", prefixe d'un Σ au dessin (cf. paintEvent) pour distinguer le cumule
        cProfile du temps par ligne du line profiler.
        """
        if mesure.kind == 'def':
            n = mesure.ncalls or 0
            par_appel = (mesure.cumtime / n) if n else None
            return (str(mesure.ncalls),          # nombre d'appels
                    "×",                         # operateur
                    format_duree(par_appel),         # temps moyen par appel
                    "=",                         # operateur
                    format_duree(mesure.cumtime))    # temps cumule
        return (str(mesure.hits),                # hits
                "×",                             # operateur
                format_duree(mesure.per_hit),        # temps par passage
                "=",                             # operateur
                format_duree(mesure.seconds))        # temps total

    def _largeurs_colonnes(self, metrics):
        """Largeur de chacune des cinq sous-colonnes, = plus large segment de son rang."""
        largeurs = [0] * len(self._ALIGNEMENTS)
        for mesure in self.results_manager.lignes.values():
            # Les lignes de `def` (cProfile) n'affichent pas de temps dans la marge (pas de hits) :
            # les exclure du calcul de largeur, sinon leur "None" gonflerait la colonne des hits.
            if mesure.kind == 'def':
                continue
            for i, texte in enumerate(self._segments(mesure)):
                largeurs[i] = max(largeurs[i], metrics.horizontalAdvance(texte))
        return largeurs

    def _x_colonnes(self, cols):
        """Abscisse gauche de chaque sous-colonne, le bloc etant cale a droite dans la marge."""
        xs = []
        x = self._largeur - self.PADDING - (
            sum(cols) + self.ECART_COLONNES * (len(cols) - 1))
        for largeur in cols:
            xs.append(x)
            x += largeur + self.ECART_COLONNES
        return xs

    def _plancher(self):
        """Largeur minimale : de quoi montrer le seul temps total, et au moins une icone.

        En dessous, on ne pourrait plus lire la valeur ni poser de marqueur.
        """
        icone = self.TAILLE_ICONE + 2 * self.PADDING
        if not self._cols:
            return icone
        return max(self._cols[-1] + 2 * self.PADDING, icone)

    def _largeur_complete(self):
        """Largeur necessaire a la formule entiere (cinq sous-colonnes)."""
        if not self._cols or not any(self._cols):
            return 0
        return (sum(self._cols) + self.ECART_COLONNES * (len(self._cols) - 1)
                + 2 * self.PADDING)

    def _texte_def(self, mesure):
        """Texte affiche a droite d'une ligne de `def`, SANS le Σ (dessine a part, en gras).

        Suit le MEME etat que les lignes du corps : la formule entiere quand la colonne est
        deployee, le seul temps cumule quand elle est repliee. Source unique, lue par
        _largeur_defs() et par paintEvent() - sinon la largeur calculee et le texte dessine
        peuvent diverger.

        Sans cette regle, la formule Σ maintenait la colonne large meme en mode compact : 145 px
        contre 72 px attendus, pour 160 px en deploye (mesure). Les deux etats devenaient
        quasi identiques, tirer la poignee ne repliait plus rien et le redimensionnement
        paraissait mort (regression du 23/07/2026 relevee par l'utilisateur).

        En mode 'vide' - cProfile seul, aucun temps par ligne - on DEPLOIE : il n'y a rien
        d'autre a montrer dans la colonne, et la poignee y est de toute facon inerte
        (mousePressEvent exige _largeur_complete(), nul dans cet etat).
        """
        if self._mode == 'total':
            return format_duree(mesure.cumtime)
        return ' '.join(self._segments(mesure))

    def _largeur_defs(self, metrics):
        """Largeur necessaire aux formules cumulees cProfile (Σ n × moy = cumule) des `def`, ou 0.

        La formule complete "Σ n × temps_par_appel = temps_cumule" (meme format que les lignes)
        est dessinee d'un bloc, cale a droite. Reserve en plus la place d'une icone a droite
        (marqueur ou apercu), qui cohabite avec le Σ sur une meme ligne de `def`. Sans ca, la
        colonne serait dimensionnee sur les seuls temps par ligne du line profiler et tronquerait
        les Σ des fonctions non marquees.
        """
        police_gras = QFont(self.editor.font())
        police_gras.setBold(True)
        w_sigma = QFontMetrics(police_gras).horizontalAdvance('Σ ')   # Σ en gras (cf. paintEvent)
        pire = 0
        for mesure in self.results_manager.lignes.values():
            if mesure.kind == 'def' and mesure.cumtime is not None:
                pire = max(pire, w_sigma
                           + metrics.horizontalAdvance(self._texte_def(mesure)))
        if not pire:
            return 0
        return int(pire + self.TAILLE_ICONE + 3 * self.PADDING)

    @property
    def _mode(self):
        """Etat d'affichage, SOURCE UNIQUE, derive de _cols, _horloges et _compact :
          - 'vide'     : aucun temps a afficher (la marge ne porte que des marqueurs) ;
          - 'horloges' : replie jusqu'aux seules horloges - aucun temps, et la coloration des
                         lignes de l'editeur est RETIREE (cf. Manager._apply_decorations) ;
          - 'total'    : temps global seul (etat compact) ;
          - 'complet'  : la formule entiere.
        rafraichir() et paintEvent() lisent tous deux cet etat au lieu de le re-deriver chacun.
        """
        if not self._largeur_complete():
            return 'vide'
        if self._horloges:
            return 'horloges'
        return 'total' if self._compact else 'complet'

    def horloges_seules(self):
        """Vrai si la colonne est repliee sur les horloges. Lu par le Manager, qui decide alors de
        ne PAS colorer les lignes de l'editeur. Methode plutot qu'attribut : c'est une lecture
        d'etat depuis un autre objet, et elle reste juste si l'etat change de forme."""
        return self._mode == 'horloges'

    # Largeur de la colonne selon le mode : de quoi cliquer (icone), le temps total, ou la formule.
    def _largeur_du_mode(self, mode):
        if mode in ('vide', 'horloges'):
            return self.TAILLE_ICONE + 2 * self.PADDING
        if mode == 'total':
            return int(self._plancher())
        return int(self._largeur_complete())

    def rafraichir(self):
        """Recalcule les largeurs de colonnes, ajuste la largeur a l'etat courant, puis repeint.

        La largeur suit EXACTEMENT le contenu de l'etat (cf. _mode / _largeur_du_mode) : passer de
        'complet' a 'total' reajuste donc la largeur (une version anterieure gardait la largeur
        tiree, laissant du vide en mode total - releve par l'utilisateur).

        refresh() et non resize() : refresh() met AUSSI a jour les marges du viewport, donc la
        zone de code se recale sur la nouvelle largeur de colonne. Sans cela, retrecir la colonne
        laissait un vide a droite au lieu d'elargir le code (le "retrecit par la droite" signale
        par l'utilisateur).
        """
        lignes = self.results_manager.lignes if self.editor is not None else None
        metrics = QFontMetrics(self.editor.font()) if self.editor is not None else None
        self._cols = self._largeurs_colonnes(metrics) if lignes else None
        mode = self._mode
        self._largeur = self._largeur_du_mode(mode)
        # La marge doit aussi loger les temps cumules "∑ ..." des lignes de `def` (cProfile), qui
        # ne passent pas par les sous-colonnes du line profiler.
        # ⚠ SAUF en mode 'horloges' : rien n'y est ecrit, et ce max() y maintiendrait la colonne
        # large - le repli ne replierait donc rien, exactement la regression du 23/07/2026 sur le
        # mode compact (la formule Σ gardait la colonne large).
        if lignes and metrics is not None and mode != 'horloges':
            self._largeur = max(self._largeur, self._largeur_defs(metrics))

        if self.editor is not None:
            self.editor.panels.refresh()
        self.update()

    def _couleur_demarcation(self):
        """Fond de l'editeur A PEINE eclairci, pour la ligne de separation code / colonne.

        Volontairement TRES DISCRET (demande de l'utilisateur : le bleu/gris du fond de l'editeur
        "un tout petit peu plus clair", juste pour deviner la poignee de redimensionnement). Force
        d'eclaircissement basse (0.10) pour rester proche du fond et preserver sa teinte : un
        melange plus fort virerait vers le blanc et delaverait le bleu. Theme-aware par
        construction.
        """
        return eclaircir(self.editor.palette().base().color(), 0.10)

    def sizeHint(self):
        return QSize(self._largeur, self._largeur)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._largeur:
            return
        painter = QPainter(self)
        # Palier froid / fond par defaut de la colonne = fond de l'editeur de texte. Une seule
        # lecture, reutilisee pour le fond global et le fond de chaque ligne.
        fond = self.results_manager.couleur_de_fond_neutre()
        painter.fillRect(event.rect(), fond)

        painter.setFont(self.editor.font())
        lignes = self.results_manager.lignes
        # En mode 'complet', position gauche et largeur de chacune des cinq sous-colonnes.
        cols = self._cols
        mode = self._mode
        xs = self._x_colonnes(cols) if (cols and mode == 'complet') else None
        # Repli sur les horloges : ni temps ni fonds thermiques, seulement les icones (cf. la boucle).
        horloges_seules = (mode == 'horloges')

        # Le texte est en couleur de texte normale (et non thermique) : c'est le FOND de la case
        # qui porte la couleur de la ligne, le chiffre reste donc lisible a toute temperature.
        painter.setPen(self.editor.palette().windowText().color())

        # Ligne courante : au lieu de la peindre d'un gris fixe, on ECLAIRCIT son propre fond
        # (demande de l'utilisateur : "gris fonce -> gris clair, colore par line profiler -> meme
        # couleur plus claire"). L'information thermique est ainsi preservee - une ligne profilee
        # sous le curseur reste de sa couleur, simplement eclaircie - et le surlignage s'applique
        # a TOUTES les lignes courantes, y compris timees et return (elles sont mesurees, et
        # l'ancien 'elif' apres le fond thermique ne les atteignait jamais).
        ligne_courante = self.results_manager.ligne_courante()

        for haut, numero, bloc in self.editor.visible_blocks:
            hauteur = self._hauteur_ligne(bloc)
            mesure = lignes.get(numero)
            if horloges_seules:
                # Repli sur les horloges : on traite chaque ligne comme NON MESUREE. Les fonds
                # thermiques et tous les textes - formule Σ des `def` comme temps des lignes de
                # corps - disparaissent alors d'eux-memes, puisque leurs branches testent `mesure`.
                # Seules restent les branches des icones (marqueur, apercu), c'est-a-dire exactement
                # ce que l'utilisateur veut garder. Un seul test ici plutot qu'un `if` dans chaque
                # branche : rien ne peut etre oublie.
                mesure = None
            fond_ligne = (ProfileResultsManager.couleur_de_fond(mesure.heat, fond)
                          if mesure is not None else None)
            if numero == ligne_courante:
                fond_ligne = eclaircir(fond_ligne if fond_ligne is not None else fond)
            if fond_ligne is not None:
                painter.fillRect(0, haut, self._largeur, hauteur, fond_ligne)

            donnees = bloc.userData()
            marque = donnees is not None and getattr(donnees, 'profile_target', False)
            if mesure is not None and mesure.kind == 'def':
                # Ligne du `def` : la mesure CUMULEE cProfile, prefixee d'un Σ pour la distinguer
                # du temps par ligne du line profiler. Le texte suit l'etat de la colonne comme
                # celui des lignes du corps - formule "n × temps_moyen = temps_cumule" deployee,
                # temps cumule seul en compact : cf. _texte_def. On utilise Σ (U+03A3, sigma
                # capitale GRECQUE, une lettre presente dans la police de code, calee sur la ligne
                # de base) et NON ∑ (U+2211, l'operateur "n-ary summation") : ce dernier, souvent
                # absent de la police de code, est rendu par une police de repli a la ligne de base
                # differente et tombe visiblement plus bas que les chiffres (defaut signale par
                # l'utilisateur). Une icone cohabite a droite (marqueur si line-profilee, apercu au
                # survol) ; le Σ + la formule se calent alors a sa gauche.
                icone = ('marqueur' if marque
                         else ('apercu' if self.line_number_hint == numero else None))
                reserve = (self.TAILLE_ICONE + self.PADDING) if icone else 0
                gauche = self._largeur - self.PADDING - reserve
                # Σ en GRAS (demande de l'utilisateur), la formule en graisse normale. Deux
                # drawText : Qt ne melange pas les graisses dans un seul appel. On cale le bloc
                # "Σ formule" a DROITE (bord droit = `gauche`), le Σ juste a gauche de la formule.
                formule = self._texte_def(mesure)
                police = self.editor.font()
                police_gras = QFont(police)
                police_gras.setBold(True)
                w_formule = QFontMetrics(police).horizontalAdvance(formule)
                w_sigma = QFontMetrics(police_gras).horizontalAdvance('Σ ')
                x0 = gauche - (w_sigma + w_formule)
                painter.setFont(police_gras)
                painter.drawText(x0, haut, w_sigma, hauteur,
                                 int(Qt.AlignLeft | Qt.AlignVCenter), 'Σ ')
                painter.setFont(police)
                painter.drawText(x0 + w_sigma, haut, w_formule, hauteur,
                                 int(Qt.AlignLeft | Qt.AlignVCenter), formule)
                if icone:
                    self._dessiner_icone(painter, haut, hauteur, icone)
            elif marque:
                # Marqueur sur une ligne du `def` sans mesure cProfile (pas de .prof) : ancien cas.
                self._dessiner_icone(painter, haut, hauteur, 'marqueur')
            elif self.line_number_hint == numero:
                self._dessiner_icone(painter, haut, hauteur, 'apercu')
            elif mesure is not None:
                # Ligne de corps (line profiler) : formule complete ou temps total seul.
                if xs is not None:
                    for texte, x, larg, alin in zip(
                            self._segments(mesure), xs, cols, self._ALIGNEMENTS):
                        painter.drawText(x, haut, larg, hauteur,
                                         int(alin | Qt.AlignVCenter), texte)
                else:
                    painter.drawText(0, haut, self._largeur - self.PADDING, hauteur,
                                     int(Qt.AlignRight | Qt.AlignVCenter),
                                     format_duree(mesure.seconds))

        # Fin trait de demarcation entre le code et la colonne, UNIQUEMENT quand il y a des temps
        # a afficher (demande de l'utilisateur : "pas par defaut").
        #
        # HAIRLINE PIXEL-PERFECT : un QPen COSMETIQUE, dont la largeur est en pixels PHYSIQUES,
        # donc le trait fait toujours EXACTEMENT 1 px physique quel que soit le facteur d'echelle
        # KDE (fractionnaire compris). Sans cela, "1 px logique" devient 1.5 px physique a 150 %,
        # et l'arrondi donne 1 ou 2 px selon la position - le trait "fait 1 ou 2 px selon ou il
        # tombe" (remarque de l'utilisateur). Il n'y a pas de fonction KDE pour ca, ca se regle au
        # niveau du dessin Qt. Choix "hairline scale-independant" confirme par l'utilisateur, et
        # verifie par un test aux echelles 1.0/1.25/1.5/2.0 (toujours 1 colonne physique).
        #
        # ⚠ Dessine EN DERNIER, apres les fonds des lignes : les fillRect thermiques et de ligne
        # courante partent de x=0 et le recouvraient sinon sur les lignes colorees.
        # ⚠ Pas de trait en mode 'horloges' : il ne se justifie que "quand il y a des temps a
        # afficher" (demande de l'utilisateur pour ce trait), et il n'y en a justement plus.
        if lignes and not horloges_seules:
            self._dessiner_demarcation(painter, event.rect())

    def _dessiner_demarcation(self, painter, rect):
        """Le filet de separation code / colonne, CALE SUR LA GRILLE DE PIXELS PHYSIQUES.

        NE PAS revenir a `painter.drawLine(0, haut, 0, bas)` avec un QPen cosmetique : le trait
        etait alors dessine exactement SUR le bord gauche du panneau, et a une echelle KDE
        FRACTIONNAIRE ce bord ne tombe pas sur la grille physique. A l'echelle 1,3 de cette
        machine, un panneau a x=526 logiques est a 683,8 px physiques : Qt arrondit la
        coordonnee, et une fois sur deux le trait atterrit hors du rectangle de decoupe du
        widget - il est alors purement SUPPRIME, pas attenue.

        Mesure a l'appui (rendu dans une QImage a devicePixelRatio 1,3, douze largeurs
        d'editeur) : trait ABSENT pour six d'entre elles, present a pleine intensite pour les
        six autres. D'ou le defaut signale par l'utilisateur : "c'est quand je redimensionne
        l'editeur que le trait disparait/reapparait" - redimensionner promene le bord du
        panneau sur la grille physique.

        Le pen cosmetique garantissait la LARGEUR (1 px physique a toute echelle) mais pas la
        POSITION. On vise donc la premiere colonne physique entierement DANS le panneau (ceil),
        et on la vise PAR SON CENTRE.

        Pourquoi le centre, et non le rectangle exact [ceil, ceil+1] : l'aller-retour
        logique -> physique se fait en flottants, et le bord gauche retombe un epsilon SOUS
        l'entier vise (686.99999 au lieu de 687). Qt elargit alors sur les deux colonnes -
        mesure faite : trait sur 2 px physiques pour trois des douze largeurs testees, soit
        exactement le defaut "le trait fait 1 ou 2 px selon ou il tombe". En couvrant le demi-pixel
        CENTRAL de la colonne (marge de 0,25 px de chaque cote), le bruit flottant ne peut plus
        faire deborder sur la voisine.

        Le facteur d'echelle est lu sur le PERIPHERIQUE DE PEINTURE et non sur le widget : en
        rendu hors ecran (tests) la cible est une QImage, dont le ratio est le seul pertinent.
        """
        dpr = painter.device().devicePixelRatioF() or 1.0
        # Abscisse physique du bord gauche du panneau dans la fenetre qui porte le tampon.
        origine_physique = self.mapTo(self.window(), QPoint(0, 0)).x() * dpr
        # Report jusqu'a la premiere colonne physique entiere, + le decalage retenu par la mesure.
        gauche = (math.ceil(origine_physique) + self.DEMARCATION_DECALAGE_PX
                  - origine_physique) / dpr
        painter.fillRect(
            QRectF(gauche, rect.top(), self.DEMARCATION_LARGEUR_PX / dpr, rect.height()),
            self._couleur_demarcation())

    def _hauteur_ligne(self, bloc):
        """Hauteur d'une ligne, tiree de la geometrie REELLE du bloc de l'editeur.

        Meme source que la position `haut` fournie par visible_blocks : une seule source de
        verite, donc les fonds colores ne peuvent jamais etre en decalage avec les lignes.

        NE PAS revenir a QFontMetrics(self.editor.font()).height() : cette hauteur globale et la
        position des lignes se mettaient a jour a des moments differents du cycle de peinture,
        d'ou un retard d'une image sur la hauteur des fonds au changement de taille des
        caracteres (defaut signale par l'utilisateur, 21/07/2026).
        """
        return round(self.editor.blockBoundingRect(bloc).height())

    def _dessiner_icone(self, painter, haut, hauteur, nom):
        """Icone centree verticalement et calee a DROITE, comme les durees.

        Un essai a gauche a ete fait puis abandonne sur preference de l'utilisateur : icone et
        durees partagent la meme colonne, elles partagent donc le meme alignement, ce qui donne
        un bord droit net quelle que soit la ligne.
        """
        cote = min(self.TAILLE_ICONE, hauteur)
        rect = QRect(self._largeur - self.PADDING - cote,
                     haut + (hauteur - cote) // 2, cote, cote)
        if nom == 'apercu':
            # Apercu au survol : la meme icone, mais estompee. QIcon n'a pas d'option d'opacite,
            # on la reduit donc sur le peintre le temps du trace.
            painter.save()
            painter.setOpacity(0.3)
            self._icone.paint(painter, rect)
            painter.restore()
        else:
            self._icone.paint(painter, rect)

    # ---- Redimensionnement de la colonne, et pose des marqueurs
    # ------------------------------------------------------------------------
    def _sur_la_poignee(self, event):
        """Vrai si le curseur est sur la bande de redimensionnement (bord gauche de la colonne).

        C'est la reponse a "si la separation est nulle, peut-on tout de meme attraper la
        separation pour la deplacer ?" : oui. Le bord gauche de la colonne touche le code sans
        aucun espace VISIBLE, mais ces quelques pixels appartiennent au panneau et recoivent les
        clics. Le curseur qui passe en fleche horizontale au survol (cf. mouseMoveEvent) signale
        la poignee, sans qu'un trait soit necessaire.
        """
        return int(event.position().x()) <= self.POIGNEE

    def _basculer_vers(self, mode):
        """Passe dans l'etat demande ('complet', 'total' ou 'horloges'), si ce n'est pas deja le cas.

        ⚠ On appelle results_manager.refresh() et NON self.rafraichir() : entrer ou sortir de l'etat
        'horloges' change la COLORATION DES LIGNES de l'editeur, qui appartient au manager. Un simple
        rafraichir() du panneau repeindrait la colonne en laissant les fonds colores en place.
        """
        horloges = (mode == 'horloges')
        compact = (mode == 'total')
        if horloges == self._horloges and compact == self._compact:
            return
        self._horloges = horloges
        self._compact = compact
        if self.results_manager is not None:
            self.results_manager.refresh()
        else:
            self.rafraichir()

    def _mode_le_plus_proche(self, largeur_visee):
        """L'etat dont la largeur est la plus proche de celle vers laquelle on tire.

        TROIS largeurs depuis le 26/07/2026 - formule complete, temps total seul, horloges seules -
        donc deux seuils. Plutot que de les ecrire, on prend le plus proche : la regle reste juste si
        une quatrieme largeur apparait, et elle ne peut pas produire de seuils incoherents.

        ⚠ Les trois largeurs ne sont pas forcement distinctes : quand les temps sont courts, le
        'total' peut mesurer exactement la largeur d'une icone, donc valoir 'horloges'. En cas
        d'egalite, on garde l'etat le PLUS LARGE (l'ordre du parcours va du plus large au plus
        etroit et le test est strict) : sans cela, tirer vers le code ne pourrait plus jamais
        deployer, l'etat le plus etroit gagnant tous les ex aequo.
        """
        candidats = [(mode, self._largeur_du_mode(mode))
                     for mode in ('complet', 'total', 'horloges')]
        meilleur, distance_min = candidats[0][0], None
        for mode, largeur in candidats:
            distance = abs(largeur_visee - largeur)
            if distance_min is None or distance < distance_min:
                meilleur, distance_min = mode, distance
        return meilleur

    def _clic_sur_bouton_horloge(self, event, lineno):
        """Vrai si le clic tombe sur l'icone (le "bouton horloge") d'une ligne de definition.

        Le bouton n'existe que sur une ligne de `def` - la ou un marqueur se pose - et occupe la
        bande de l'icone, calee a droite de la colonne. Ailleurs, un clic selectionne la ligne.
        """
        if self.targets_manager is None:
            return False
        if self.targets_manager.def_line_for(lineno) != lineno:
            return False
        return event.position().x() >= self._largeur - self.PADDING - self.TAILLE_ICONE

    def _selectionner_ligne(self, lineno):
        """Place le curseur de l'editeur a la FIN de la ligne, sans faire defiler la vue.

        Demande de l'utilisateur : un clic dans la zone de temps, hors du bouton horloge (sur une
        ligne de definition notamment), doit selectionner la ligne dans l'editeur et deplacer le
        curseur a la fin de cette ligne. On deplace donc le curseur - ce qui surligne la ligne
        courante - plutot que de basculer un marqueur.
        setTextCursor plutot que go_to_line : ce dernier recentre la vue, brutal pour une ligne
        deja visible sur laquelle on vient de cliquer.
        """
        bloc = self.editor.document().findBlockByNumber(lineno - 1)
        if not bloc.isValid():
            return
        curseur = self.editor.textCursor()
        curseur.setPosition(bloc.position())
        curseur.movePosition(curseur.MoveOperation.EndOfBlock)
        self.editor.setTextCursor(curseur)
        self.editor.setFocus()

    def mousePressEvent(self, event):
        """Poignee -> redimensionne ; bouton horloge -> bascule un marqueur ; ailleurs ->
        selectionne la ligne dans l'editeur."""
        if self._sur_la_poignee(event) and self._largeur_complete():
            self._redimensionne = True
            return
        lineno = self.editor.get_linenumber_from_mouse_event(event)
        if self._clic_sur_bouton_horloge(event, lineno):
            self.targets_manager.toggle_target(lineno)
        else:
            self._selectionner_ligne(lineno)

    def mouseMoveEvent(self, event):
        """Redimensionne si on tire la poignee ; sinon apercu du marqueur et forme du curseur.

        La colonne n'a que DEUX largeurs (formule complete, temps total seul) : tirer la poignee
        vers le code (a gauche) elargit -> formule complete ; vers le bord de la fenetre (a
        droite) retrecit -> temps total. Le seuil est la mi-distance entre les deux largeurs. On
        compare la position globale du curseur au bord droit fixe de la colonne.

        L'apercu du marqueur est dessine sur la ligne du `def`. On ne repeint que si l'apercu
        change de ligne (mouseMoveEvent est appele a chaque pixel parcouru).
        """
        if self._redimensionne:
            complete = self._largeur_complete()
            if complete:
                bord_droit = self.mapToGlobal(
                    QRect(0, 0, self.width(), 0).topRight()).x()
                largeur_visee = bord_droit - event.globalPosition().toPoint().x()
                self._basculer_vers(self._mode_le_plus_proche(largeur_visee))
            return

        # Curseur en fleche horizontale au-dessus de la poignee : signale qu'on peut tirer.
        self.setCursor(Qt.SplitHCursor if self._sur_la_poignee(event) else Qt.ArrowCursor)

        if self.targets_manager is None:
            return
        hint = self.targets_manager.def_line_for(
            self.editor.get_linenumber_from_mouse_event(event))
        if hint != self.line_number_hint:
            self.line_number_hint = hint
            self.update()

    def mouseReleaseEvent(self, event):
        """Fin du redimensionnement : on enregistre l'etat choisi.

        Les DEUX booleens sont ecrits, pas seulement celui qui vient de changer : l'etat est leur
        combinaison, et n'en sauver qu'un laisserait la configuration incoherente d'une session a
        l'autre (par exemple 'horloges' vrai avec 'compact' resté vrai de la fois precedente).
        """
        if self._redimensionne:
            self._redimensionne = False
            sauver_mode_compact(self._compact)
            sauver_horloges_seules(self._horloges)

    def leaveEvent(self, event):
        self.setCursor(Qt.ArrowCursor)
        if self.line_number_hint is not None:
            self.line_number_hint = None
            self.update()

    def wheelEvent(self, event):
        """Laisse la molette faire defiler l'editeur au lieu de s'arreter sur la marge."""
        self.editor.wheelEvent(event)
