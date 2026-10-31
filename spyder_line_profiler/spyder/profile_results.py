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

import ast
import json
import linecache
import math
import os
import os.path as osp
import pickle
import shutil

from qtpy.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from qtpy.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPolygonF, QRegion
from qtpy.QtWidgets import QMenu, QToolTip, QWidget


from spyder.plugins.editor.api.decoration import TextDecoration, DRAW_ORDERS
from spyder.plugins.editor.api.panel import Panel


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


def fond_editeur(editor):
    """Fond de la zone de code TEL QU'IL EST PEINT : palette base, eclaircie quand l'editeur a
    le focus (patch SmartOS patch_spyder_focus_volet.py : feuille de style `:focus` a
    base.lighter(SMARTOS_ECLAIRCISSEMENT)). Demande de l'utilisateur du 30/10/2026 : « le fond de
    la marge a droite doit suivre la couleur du fond de l'editeur quand il change lors du
    focus ». Sans le patch (Spyder amont), simplement la palette base."""
    couleur = editor.palette().base().color()
    try:
        from spyder.utils.stylesheet import SMARTOS_ECLAIRCISSEMENT
    except ImportError:
        return couleur
    return couleur.lighter(SMARTOS_ECLAIRCISSEMENT) if editor.hasFocus() else couleur


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

# Cle des SOULIGNEMENTS du mode « Σ » (instructions qui cachent des operations), distincte de
# celle des fonds : les deux se posent et se retirent independamment.
CACHES_KEY = 'smartos_profile_caches'

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

# DEUX zones de resultats (demande de l'utilisateur, 31/10/2026 : « je veux deux zones separees
# pour deux resultats et les deux menus en haut »). Chacune choisit, par le menu de son en-tete, la
# SOURCE qu'elle montre - la mesure de reference ou la derniere mesure - et UNE nature de mesure,
# chronometrage OU comptage Σ : ils ne sont pas mesures au meme moment, ils ne partagent pas une
# zone (utilisateur, 31/10/2026 : « le chronometrage et le compteur devraient etre dans deux zones
# differentes, il ne sont pas mesure au meme moment ! »). Defauts : les dernieres mesures, le
# chronometrage a gauche, le comptage a droite.
SOURCES_ZONES = ('reference', 'derniere')
NATURES = ('temps', 'operations')
# L'ordre est celui du menu (utilisateur, 31/10/2026 : « plutot que deux partie dans le menu, il
# faudrait : dernier comptage, dernier chronometrage, chrometrage de reference, comptage de
# reference »).
# En-tete : le plus long des libelles qui tient dans la largeur de la zone (cf. _texte_entete) ;
# le premier est aussi celui du menu. Les abreges gardent la nature sous forme du marqueur, Σ ou
# l'horloge (ICONE_HORLOGE, dessinee comme l'icone des boutons) - utilisateur, 31/10/2026 :
# « pourquoi "Dernier Comptage" est réduit à "Dern." et par "Dernier ∑" ? ou "Dernier (horloge)" ? ».
ICONE_HORLOGE = '⏲'
LIBELLES_ZONES = {
    ('derniere', 'operations'): ('Dernier comptage', 'Dern. comptage', 'Dernier Σ', 'Dern. Σ'),
    ('derniere', 'temps'): ('Dernier chronométrage', 'Dern. chrono.',
                           'Dernier ' + ICONE_HORLOGE, 'Dern. ' + ICONE_HORLOGE),
    ('reference', 'temps'): ('Chronométrage de référence', 'Réf. chrono.',
                            'Référence ' + ICONE_HORLOGE, 'Réf. ' + ICONE_HORLOGE),
    ('reference', 'operations'): ('Comptage de référence', 'Réf. comptage',
                                  'Référence Σ', 'Réf. Σ'),
}


def charger_choix_zone(rang):
    """(source, (nature,)) de la zone `rang` (0 a gauche, 1 a droite), defauts si rien n'est lu.
    Options `zoneN_choix` : celles d'avant (`zoneN_source`, `zoneN_natures`, deux natures par
    zone) sont ignorees."""
    source, nature = 'derniere', NATURES[rang]
    try:
        from spyder.config.manager import CONF
        source, nature = str(CONF.get(CONF_SECTION, f'zone{rang + 1}_choix',
                                      f'{source},{nature}')).split(',')
    except Exception:
        pass
    if source not in SOURCES_ZONES or nature not in NATURES:
        source, nature = 'derniere', NATURES[rang]
    return source, (nature,)


def sauver_choix_zone(rang, source, natures):
    """Enregistre le choix du menu de la zone `rang`. Silencieux hors Spyder."""
    try:
        from spyder.config.manager import CONF
        CONF.set(CONF_SECTION, f'zone{rang + 1}_choix', f'{source},{natures[0]}')
    except Exception:
        pass


def charger_zone_cachee(rang):
    """Vrai si l'utilisateur a decoche l'entree du menu de la zone `rang`. Faux par defaut."""
    try:
        from spyder.config.manager import CONF
        return bool(CONF.get(CONF_SECTION, f'zone{rang + 1}_cachee', False))
    except Exception:
        return False


def sauver_zone_cachee(rang, cachee):
    """Enregistre que la zone `rang` est cachee. Silencieux hors Spyder."""
    try:
        from spyder.config.manager import CONF
        CONF.set(CONF_SECTION, f'zone{rang + 1}_cachee', bool(cachee))
    except Exception:
        pass


def reference_temps_for(filename, texte):
    """Temps de REFERENCE du fichier, {ligne: LigneMesuree}, recales sur `texte`.

    Chaque fonction a SA reference (on ne chronometre pas toutes les fonctions en meme temps) :
    son run le plus ANCIEN de l'historique .profiler/ (profile_history). Les lignes modifiees
    depuis ce run n'ont plus de reference (remap_run_to_current les donne perimees). {} hors depot
    ou sans historique - jamais d'exception : la reference est un plus.
    """
    if not filename or texte is None:
        return {}
    try:
        from spyder_line_profiler.spyder.profile_history import (
            functions_with_history, load_function_runs, remap_run_to_current)
    except ImportError:
        return {}
    resultats = {}
    try:
        for nom in functions_with_history(filename):
            runs = load_function_runs(filename, nom)
            if not runs:
                continue
            run = runs[-1]       # load_function_runs : du plus recent au plus ancien
            unit = run.get('unit') or 1e-6
            mapped, _perimees = remap_run_to_current(run, texte)
            total = sum(temps for _rel, _hits, temps in mapped.values()) * unit
            for ligne, (_rel, hits, temps) in mapped.items():
                secondes = temps * unit
                resultats[ligne] = LigneMesuree(
                    hits=hits, seconds=secondes, per_hit=secondes / hits if hits else None,
                    percent=(secondes / total) if total else 0.0,
                    function=run.get('function'), kind='line')
    except Exception:
        return {}
    return resultats


# Registre en memoire : {chemin normalise: {ligne: LigneMesuree}}. Rempli par publish(), lu par
# chaque ProfileResultsManager. En memoire seulement : le pickle sur disque reste la source.
_RESULTS = {}

# Vrai des que le fichier de resultats du disque a ete lu (une fois par processus).
_CHARGE = False

# Mode « Σ » (demande de l'utilisateur du 30/10/2026) : comptes d'operations elementaires,
# {chemin normalise: dict de profile_operations.lire}. A cote des temps, jamais a leur place : les
# deux mesures sont des lancements separes, et chacune reste affichee tant que le code ne change
# pas (cf. _retirer_perimes).
_OPERATIONS = {}

# Texte de chaque fichier au moment ou ses TEMPS ont ete publies : sert a savoir si le code a
# change depuis (le JSON des operations, lui, porte sa propre source).
_SOURCES_TEMPS = {}

# Mesure LANCEE en dernier dans cette session, 'temps' ou 'operations', None avant la premiere :
# c'est elle qui choisit le marqueur de la marge (horloge ou Σ). Pas reprise du disque : sans
# lancement, le marqueur suit les boutons visibles (cf.
# mode_du_marqueur).
_LANCEE = None

# Comptage EPINGLE (demande de l'utilisateur du 30/10/2026 : « un simple epinglage ») : copie figee
# d'un comptage, affichee a cote du suivant pour comparer deux tailles d'entree. Contrairement aux
# deux mesures, il SURVIT aux modifications du code - on change justement n dans le script entre
# les deux comptages : ses lignes sont recalees sur le texte courant (cf. epingle_for). Dict de
# profile_operations.lire, ou None.
_EPINGLE = None


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


def _source_disque(chemin):
    """Texte actuel du fichier sur le disque, ou None s'il est illisible."""
    try:
        with open(chemin, 'r', encoding='utf-8') as flux:
            return flux.read()
    except (OSError, UnicodeDecodeError):
        return None


def _noter_sources_temps(resultats):
    """Retient le texte des fichiers dont on vient de publier les temps (cf. _retirer_perimes)."""
    _SOURCES_TEMPS.clear()
    for chemin in resultats:
        _SOURCES_TEMPS[chemin] = _source_disque(chemin)


def _retirer_perimes(garder):
    """A la publication d'une mesure, retire l'AUTRE mesure des fichiers modifies depuis.

    Regle de l'utilisateur : « chaque partie reste affichee tant que le code ne change pas ». Un
    temps et un compte pris sur deux versions differentes du code ne se lisent pas cote a cote.
    `garder` est la mesure qu'on vient de publier ('temps' ou 'operations').
    """
    if garder == 'temps':
        for chemin in list(_OPERATIONS):
            if _OPERATIONS[chemin].get('source') != _source_disque(chemin):
                del _OPERATIONS[chemin]
    else:
        for chemin in list(_RESULTS):
            if _SOURCES_TEMPS.get(chemin) != _source_disque(chemin):
                del _RESULTS[chemin]


def operations_path():
    """JSON ecrit par cx_launcher.py, relu au demarrage suivant."""
    from spyder.config.base import get_conf_path
    return get_conf_path('lineprofiler_operations.json')


def _lire_operations(path):
    try:
        from spyder_line_profiler.spyder.profile_operations import lire
    except ImportError:
        return None
    return lire(path)


def publish_operations(path):
    """Publie les comptes d'operations du JSON `path` (mode « Σ ») et previent les editeurs."""
    global _OPERATIONS
    # Les temps du disque d'abord : sinon ensure_loaded(), appele plus tard par un editeur, les
    # chargerait APRES le tri des perimes ci-dessous.
    ensure_loaded()
    donnees = _lire_operations(path)
    _OPERATIONS = {donnees['fichier']: donnees} if donnees else {}
    _retirer_perimes('operations')
    BUS.sig_results_changed.emit()
    BUS.sig_nouveaux_resultats.emit()
    return donnees


def operations_for(filename):
    """Comptes du fichier donne (dict de profile_operations.lire), ou None."""
    if not filename:
        return None
    ensure_loaded()
    return _OPERATIONS.get(osp.normcase(str(filename)))


def epingle_path():
    """Copie du JSON de cx_launcher faite a l'epinglage, relue au demarrage suivant."""
    from spyder.config.base import get_conf_path
    return get_conf_path('lineprofiler_operations_epingle.json')


def epingler():
    """Epingle le comptage courant (remplace l'epingle precedente). Renvoie le comptage
    epingle, ou None s'il n'y a aucun comptage a epingler."""
    global _EPINGLE
    ensure_loaded()
    if not _OPERATIONS:
        return None
    try:
        shutil.copyfile(operations_path(), epingle_path())
    except OSError:
        return None
    _EPINGLE = _lire_operations(epingle_path())
    BUS.sig_results_changed.emit()
    return _EPINGLE


def desepingler():
    """Retire l'epingle, du disque comme de l'affichage."""
    global _EPINGLE
    ensure_loaded()
    _EPINGLE = None
    try:
        os.remove(epingle_path())
    except OSError:
        pass
    BUS.sig_results_changed.emit()


def epingle_for(filename, texte=None):
    """Comptes epingles du fichier donne, {ligne: LigneComptee}, recales sur `texte`.

    `texte` est le texte COURANT de l'editeur : seules les lignes inchangees depuis l'epinglage
    gardent leur compte (profile_history.remap_lines) - la ligne `n = 100` devenue `n = 1000`
    n'a plus de compte epingle, le corps des fonctions, inchange, garde le sien.
    """
    if not filename:
        return {}
    ensure_loaded()
    if not _EPINGLE or _EPINGLE['fichier'] != osp.normcase(str(filename)):
        return {}
    lignes = _EPINGLE['lignes']
    if texte is None or texte == _EPINGLE.get('source'):
        return lignes
    try:
        from spyder_line_profiler.spyder.profile_history import remap_lines
    except ImportError:
        return {}
    mapped, _perimees = remap_lines(_EPINGLE.get('source') or '', texte, lignes)
    return mapped


# Punaises (demande de l'utilisateur, 31/10/2026 : « afficher une punaise a la place de l'unite
# quand on la survole, et punaiser avec un simple clic gauche, puis depunaiser de la meme facon ») :
# mesures figees LIGNE PAR LIGNE - ou fonction entiere, depuis son `def` - qui deviennent la
# reference de ces lignes, par-dessus la reference par defaut (plus ancien run, epingle).
# {fichier: [groupe]}, un groupe par clic : {'source': texte au moment du clic, 'nature': 'temps' |
# 'operations' | 'fonctions', 'lignes': {ligne: valeur JSON}}. Chaque groupe garde SA source : ses
# lignes sont recalees sur le texte courant comme l'epingle (remap_lines), une ligne modifiee
# depuis perd sa punaise a l'affichage. Charge a la premiere lecture, None avant.
_PUNAISES = None
NATURES_PUNAISE = ('temps', 'operations', 'fonctions')


def punaises_path():
    from spyder.config.base import get_conf_path
    return get_conf_path('lineprofiler_punaises.json')


def _punaises():
    global _PUNAISES
    if _PUNAISES is None:
        try:
            with open(punaises_path(), encoding='utf-8') as flux:
                _PUNAISES = json.load(flux)
        except (OSError, ValueError, ImportError):
            _PUNAISES = {}
    return _PUNAISES


def _sauver_punaises():
    try:
        with open(punaises_path(), 'w', encoding='utf-8') as flux:
            json.dump(_punaises(), flux)
    except (OSError, ImportError):
        pass


def _vers_json(nature, valeur):
    if nature == 'temps':
        return {nom: getattr(valeur, nom) for nom in LigneMesuree.__slots__ if nom != 'heat'}
    return {'passages': valeur.passages, 'operations': valeur.operations}


def _depuis_json(nature, valeur):
    if nature == 'temps':
        return LigneMesuree(**valeur)
    from spyder_line_profiler.spyder.profile_operations import LigneComptee
    return LigneComptee(valeur['passages'], valeur['operations'])


def _punaises_recalees(filename, texte):
    """{(nature, ligne courante): (groupe, cle d'origine, valeur JSON)} ; le plus recent l'emporte."""
    if not filename:
        return {}
    groupes = _punaises().get(osp.normcase(str(filename)), [])
    if not groupes:
        return {}
    from spyder_line_profiler.spyder.profile_history import remap_lines
    resultat = {}
    for groupe in groupes:
        cles = {int(cle): cle for cle in groupe['lignes']}
        if texte is None or texte == groupe['source']:
            recalees = cles
        else:
            recalees, _perimees = remap_lines(groupe['source'], texte, cles)
        for ligne, cle in recalees.items():
            resultat[(groupe['nature'], ligne)] = (groupe, cle, groupe['lignes'][cle])
    return resultat


def rechauffer(lignes, fonctions):
    """Comptes de reference ({ligne: LigneComptee}, lignes puis `def`) avec une chaleur recalculee
    sur la pire LIGNE de l'ensemble, comme profile_operations.lire le fait pour un comptage.

    Une punaise est relue sans chaleur (0, cf. _vers_json), et 0 est exactement la couleur du fond
    de l'editeur : la ligne etait peinte mais invisible (utilisateur, 31/10/2026 : « le comptage de
    reference sur la colonne de droite ne colore rien »). Recalculer aussi l'epingle met epingle et
    punaises sur une seule echelle. Ecarte : enregistrer la chaleur dans la punaise, qui resterait
    sur l'echelle du comptage d'origine, pas sur celle de ce qui est montre."""
    try:
        from spyder_line_profiler.spyder.profile_operations import LigneComptee, chaleur
    except ImportError:
        return dict(lignes), dict(fonctions)
    plus = max((ligne.operations for ligne in lignes.values()), default=0)

    def rechauffee(ligne):
        return LigneComptee(ligne.passages, ligne.operations, chaleur(ligne.operations, plus))
    return ({n: rechauffee(v) for n, v in lignes.items()},
            {n: rechauffee(v) for n, v in fonctions.items()})


def punaises_for(filename, texte):
    """{nature: {ligne: LigneMesuree | LigneComptee}} des punaises du fichier, recalees sur
    `texte`. Jamais d'exception : la punaise est un plus."""
    par_nature = {nature: {} for nature in NATURES_PUNAISE}
    try:
        for (nature, ligne), (_g, _c, valeur) in _punaises_recalees(filename, texte).items():
            par_nature[nature][ligne] = _depuis_json(nature, valeur)
    except Exception:
        pass
    return par_nature


def punaise_identique(filename, texte, valeurs):
    """Vrai si TOUTES les `valeurs` ({(nature, ligne): mesure}) sont deja punaisees, a l'identique :
    le clic les depunaiserait."""
    try:
        recalees = _punaises_recalees(filename, texte)
    except Exception:
        return False
    return bool(valeurs) and all(
        cle in recalees and recalees[cle][2] == _vers_json(cle[0], mesure)
        for cle, mesure in valeurs.items())


def basculer_punaise(filename, texte, valeurs, etendue=None):
    """Punaise les `valeurs` ({(nature, ligne): mesure}) ; si elles le sont deja a l'identique, les
    depunaise (meme geste, meme endroit). Renvoie True si punaise, False si depunaise.

    `etendue` (premiere, derniere) : ce que retire la depunaise - TOUTES les punaises de ces lignes,
    quelle que soit leur nature ou leur presence dans la mesure montree. Sans elle, depunaiser un
    `def` laissait les punaises de l'autre nature et celles des lignes absentes de cette mesure
    (utilisateur, 31/10/2026 : « quand je désélectionne la punaise sur une ligne def, elle ne
    décoche que certaines punaises de la fonction »)."""
    fichier = osp.normcase(str(filename))
    recalees = _punaises_recalees(filename, texte)
    retirees = set(valeurs)
    if etendue is None:
        deja = punaise_identique(filename, texte, valeurs)
    else:
        # Avec une etendue, une punaise QUELCONQUE dans ces lignes suffit a depunaiser : le clic
        # ne repunaise plus la mesure neuve quand elle differe de la punaisee (utilisateur,
        # 31/10/2026 : « un premier clic sur une punaise de la décoche pas »).
        premiere, derniere = etendue
        dans_etendue = {cle for cle in recalees if premiere <= cle[1] <= derniere}
        deja = bool(dans_etendue)
        retirees.update(dans_etendue)
    # Dans les deux cas, l'ancienne punaise de ces lignes s'en va.
    for cle in retirees:
        if cle in recalees:
            groupe, origine, _valeur = recalees[cle]
            groupe['lignes'].pop(origine, None)
    groupes = [groupe for groupe in _punaises().get(fichier, []) if groupe['lignes']]
    if not deja:
        for nature in NATURES_PUNAISE:
            lignes = {str(ligne): _vers_json(nature, mesure)
                      for (n, ligne), mesure in valeurs.items() if n == nature}
            if lignes:
                groupes.append({'source': texte, 'nature': nature, 'lignes': lignes})
    if groupes:
        _punaises()[fichier] = groupes
    else:
        _punaises().pop(fichier, None)
    _sauver_punaises()
    BUS.sig_results_changed.emit()
    return not deja


def etendue_fonction(texte, ligne_def):
    """(premiere, derniere) ligne de la fonction dont le `def` est en `ligne_def` ; la seule ligne
    du `def` si le texte ne se lit pas."""
    try:
        for noeud in ast.walk(ast.parse(texte)):
            if isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and noeud.lineno == ligne_def:
                return noeud.lineno, noeud.end_lineno
    except (SyntaxError, ValueError):
        pass
    return ligne_def, ligne_def


def noter_lancement(mode):
    """Retient la mesure qui vient d'etre lancee ('temps' ou 'operations') et repeint les marges."""
    global _LANCEE
    _LANCEE = mode
    BUS.sig_results_changed.emit()


def mode_du_marqueur(boutons_visibles=None):
    """'temps' (horloge) ou 'operations' (Σ) : le marqueur de la marge (decision de l'utilisateur,
    30/10/2026 : « le dernier profiler lance, et l'horloge par defaut si le bouton horloge est
    visible, et sinon somme par defaut si le bouton somme est visible »).

    `boutons_visibles` : (horloge, Σ) visibles dans la barre d'outils ; par defaut demande au
    greffon. Aucun des deux visible : l'horloge, comme avant le mode « Σ ».
    """
    if _LANCEE is not None:
        return _LANCEE
    if boutons_visibles is None:
        boutons_visibles = _boutons_visibles()
    horloge, sigma = boutons_visibles
    if not horloge and sigma:
        return 'operations'
    return 'temps'


def _boutons_visibles():
    """(horloge, Σ) visibles, demande au greffon par le registre de Spyder ; a defaut (greffon pas
    encore enregistre, au demarrage), lus dans la configuration (cf. boutons_visibles_conf)."""
    try:
        from spyder.api.plugin_registration.registry import PLUGIN_REGISTRY
        return PLUGIN_REGISTRY.get_plugin('spyder_line_profiler')._smartos_boutons_visibles()
    except Exception:
        return boutons_visibles_conf()


# Cles des deux boutons dans l'option « boutons_masques » de spyder_window_controls
# (« <id de barre>/<action_id> », cf. toolbar_buttons.cle_action).
CLE_BOUTON_HORLOGE = 'profile_toolbar/run file in profiler'
CLE_BOUTON_SIGMA = 'profile_toolbar/compter les operations elementaires'


def boutons_visibles_conf():
    """(horloge, Σ) visibles d'apres la configuration enregistree : barre Profile affichee, et
    bouton absent des masques de « Fenetre > Boutons des barres d'outils ». Sert AVANT que la
    fenetre soit montree : les barres ne sont pas encore rendues, le masquage pas encore applique,
    et l'etat des widgets donnait l'horloge, remplacee ensuite par le Σ (« au demarrage l'icone
    d'horloge apparait avant d'etre remplace par la somme », l'utilisateur, 30/10/2026)."""
    try:
        from spyder.config.manager import CONF
        barres = CONF.get('toolbar', 'toolbars_visible', True)
        profile = 'profile_toolbar' in (CONF.get('toolbar', 'last_visible_toolbars', None)
                                        or ['profile_toolbar'])
    except Exception:
        return (True, True)
    if not (barres and profile):
        return (False, False)
    return boutons_masques_conf()


def boutons_masques_conf():
    """(horloge, Σ) pas masques un par un d'apres « boutons_masques » de spyder_window_controls,
    ecrite a chaque bascule, donc toujours a jour (la barre ne l'est qu'apres son rendu)."""
    masques = _boutons_masques()
    return (CLE_BOUTON_HORLOGE not in masques, CLE_BOUTON_SIGMA not in masques)


def _boutons_masques():
    """L'option « boutons_masques » de spyder_window_controls. Tant que ce greffon n'est pas
    enregistre, CONF.get rend SANS ERREUR la valeur par defaut (liste vide), donc « horloge
    visible » : c'etait l'horloge du demarrage (mesure du 31/10/2026, greffon enregistre APRES
    celui-ci). D'ici la, on lit son fichier de configuration nous-memes."""
    try:
        from spyder.api.plugin_registration.registry import PLUGIN_REGISTRY
        from spyder.config.manager import CONF
        if 'window_controls' in PLUGIN_REGISTRY:
            return set(CONF.get('window_controls', 'boutons_masques', []) or [])
    except Exception:
        pass
    try:
        import ast
        import configparser
        from spyder.config.base import get_conf_path
        lecteur = configparser.ConfigParser(interpolation=None)
        lecteur.read(os.path.join(get_conf_path(), 'plugins', 'window_controls', 'spyder.ini'),
                     encoding='utf-8')
        return set(ast.literal_eval(lecteur.get('window_controls', 'boutons_masques',
                                                fallback='[]')) or [])
    except Exception:
        return set()


def publish(path):
    """Recharge les resultats depuis `path`, archive le run, et previent les editeurs ouverts."""
    global _RESULTS, _CHARGE
    ensure_loaded()     # comptes du disque charges AVANT le tri des perimes (cf. publish_operations)
    _RESULTS = load_results(path)
    _noter_sources_temps(_RESULTS)
    _retirer_perimes('temps')
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
    ensure_loaded()
    try:
        with open(_cprofile_path(), 'wb') as flux:
            flux.write(prof_buffer)
    except OSError:
        return None
    # os.devnull en guise de "resultats de lignes" : lstats reste None (cProfile seul), et
    # load_results lit le .prof ecrit ci-dessus pour la couche def-heatmap.
    _RESULTS = load_results(os.devnull)
    _noter_sources_temps(_RESULTS)
    _retirer_perimes('temps')
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
    global _CHARGE, _EPINGLE
    if _CHARGE:
        return
    _CHARGE = True
    resultats = load_results(default_results_path())
    if resultats:
        _RESULTS.update(resultats)
        _noter_sources_temps(_RESULTS)
    # Comptes du mode « Σ » : repris SEULEMENT si le code n'a pas change depuis le comptage.
    try:
        chemin_ops = operations_path()
    except Exception:
        return
    # L'epingle, elle, est reprise meme si le code a change (cf. _EPINGLE).
    _EPINGLE = _lire_operations(epingle_path())
    donnees = _lire_operations(chemin_ops)
    if donnees and donnees.get('source') == _source_disque(donnees['fichier']):
        _OPERATIONS[donnees['fichier']] = donnees


def results_for(filename):
    """Mesures du fichier donne, sous la forme {ligne: LigneMesuree}."""
    if not filename:
        return {}
    ensure_loaded()
    return _RESULTS.get(osp.normcase(str(filename)), {})


def clear(kind=None):
    """Oublie les resultats (bouton d'effacement du panneau : les deux mesures).
    L'epingle n'est pas touchee : elle a sa propre action (desepingler).

    `kind` = 'temps' ou 'operations' n'oublie que celle-la : c'est ce que fait le lancement d'une
    mesure, qui ne doit pas effacer l'autre (chacune reste tant que le code ne change pas).
    """
    global _RESULTS, _CHARGE, _OPERATIONS
    if kind is not None:
        # Le disque d'abord, sinon l'autre mesure serait rechargee... ou jamais (_CHARGE).
        ensure_loaded()
    if kind in (None, 'temps'):
        _RESULTS = {}
    if kind in (None, 'operations'):
        _OPERATIONS = {}
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
            QPainter(self).fillRect(event.rect(), fond_editeur(self.editor))


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
        # Mode « Σ » : {ligne: LigneComptee}, instructions cachees, et le compte entier (fichiers
        # marques hors script, erreur) - cf. profile_operations.
        self.operations = {}
        self.caches = []
        self.comptage = None
        # Totaux du mode « Σ » par fonction, {ligne du def: LigneComptee} (passages = appels).
        self.fonctions = {}
        self.epingle = {}
        # Temps de reference (cf. reference_temps_for), {ligne: LigneMesuree}.
        self.reference = {}
        # Punaises du fichier, {nature: {ligne: mesure}} (cf. punaises_for).
        self.punaises = {nature: {} for nature in NATURES_PUNAISE}
        # Comptes de reference (epingle + punaises), chaleur recalculee (cf. rechauffer).
        self.reference_operations = {}
        self.reference_fonctions = {}

        # DEUX zones (demande du 31/10/2026) : zones[0] a gauche, cote code ; zones[1] a droite.
        # `panel` designe la zone de droite, la « derniere mesure » par defaut.
        self.zones = (ProfileResultsPanel(self, rang=0), ProfileResultsPanel(self, rang=1))
        # Les boutons Σ / horloge des `def` sont a gauche de la zone de DROITE (utilisateur,
        # 31/10/2026 : « je veux les bouton Σ et horloge a gauche de la colonne de droite plutot
        # que celle de gauche »).
        self.zone_marqueurs = self.zones[1]
        self.panel = self.zones[1]
        # Les zones sont des CALQUES par-dessus le code (panneaux FLOATING), plus des marges de
        # droite : sur les lignes sans resultat, elles laissent voir le code (utilisateur,
        # 31/10/2026 : « sur les ligne sans aucune resultat, peut-on mettre le fond de la zone
        # d'affichage du profilage transparent pour laisser visible le code ? »). Une marge RIGHT
        # retrecit la vue : il n'y a AUCUN code sous elle, la transparence n'y montrerait que du
        # vide. Le calque ne retient de la place qu'aux lignes qui portent un resultat, par un
        # masque (cf. ProfileResultsPanel._ajuster_masque) : hors masque, rien n'est peint et la
        # souris atteint l'editeur. Placement : cf. ProfileResultsPanel.set_geometry, zone 1 a
        # gauche de la zone 2, la zone 2 contre le bord droit de la vue.
        for zone in self.zones:
            # register() range les panneaux par NOM (celui de la classe par defaut) : sous le
            # meme nom, la zone 2 ecrasait la zone 1, jamais placee (largeur 0, boutons absents).
            zone.name = f'{type(zone).__name__}{zone.rang + 1}'
            editor.panels.register(zone, Panel.Position.FLOATING)
            # Panel.on_install() ne fait que reparenter : sans setVisible, la marge existe mais
            # reste invisible (piege deja rencontre avec la marge des marqueurs).
            zone.setVisible(True)

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
        editor.sig_font_changed.connect(self.rafraichir_zones)
        # Au deplacement du curseur : reposer les decorations (l'eclairci de la ligne courante
        # dans la ZONE DE CODE doit suivre le curseur) ET repeindre la colonne (meme eclairci cote
        # timings). Sans reposer les decorations, seule la colonne suivait le curseur, pas le
        # code.
        editor.cursorPositionChanged.connect(self._suivre_curseur)
        # Le fond de l'editeur s'eclaircit au focus (patch SmartOS) : colonne, espaceur ET palier
        # froid des lignes de code le suivent. Signal absent de Spyder amont, d'ou le getattr.
        self._signal_focus = getattr(editor, 'focus_changed', None)
        if self._signal_focus is not None:
            self._signal_focus.connect(self._suivre_focus)
        # Infobulle des instructions cachees : Spyder n'affiche PAS decoration.tooltip au survol
        # (seul le pliage de code s'en sert) - on la sert nous-memes, comme l'editeur de QCM.
        editor.viewport().installEventFilter(self)

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
        for zone in self.zones:
            zone.update()

    def _suivre_focus(self):
        """Focus gagne ou perdu : repose tout (cf. _suivre_curseur), la ligne courante comprise."""
        self._derniere_ligne_courante = None
        self._suivre_curseur()
        self.espaceur_gauche.update()

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
                             (self.editor.sig_font_changed, self.rafraichir_zones),
                             (self.editor.cursorPositionChanged, self._suivre_curseur),
                             (self._signal_focus, self._suivre_focus)):
            if signal is None:
                continue
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        try:
            self.editor.viewport().removeEventFilter(self)
        except RuntimeError:
            pass

    def eventFilter(self, objet, event):
        """Infobulle d'une instruction cachee sous la souris ; tout le reste passe a l'editeur."""
        if event.type() == QEvent.Resize:
            # Les calques suivent le bord droit de la vue : CodeEditor.resizeEvent n'appelle que
            # PanelsManager.resize(), qui ne place pas les panneaux FLOATING.
            for zone in self.zones:
                zone.set_geometry(None)
        if event.type() == QEvent.ToolTip and self.caches and not self._horloges_seules():
            curseur = self.editor.cursorForPosition(event.pos())
            ligne = curseur.blockNumber() + 1
            colonne = curseur.positionInBlock()
            for cache in self.caches:
                if cache.contient(ligne, colonne):
                    QToolTip.showText(event.globalPos(), cache.infobulle(), self.editor)
                    return True
        return super().eventFilter(objet, event)

    def chaleur(self, ligne):
        """Chaleur du fond de `ligne`, ou None. Lue par les fonds du code ET par les zones, pour
        qu'ils ne divergent jamais."""
        mesure = self._mesures_du_fond().get(ligne)
        return None if mesure is None else mesure.heat

    def lignes_colorees(self):
        """Numeros des lignes qui recoivent un fond (cf. chaleur)."""
        return self._mesures_du_fond().keys()

    def _mesures_du_fond(self):
        """{ligne: mesure} qui donne le fond : exactement ce que montre la zone de DROITE, rien si
        elle est cachee ou vide (utilisateur, 31/10/2026 : « on ne devrait colorer que les lignes
        ayant des valeurs dans la colonne de droite », « suit toujours ce qu'il y a dans la colonne
        de droite » ; ecarte : la derniere mesure lancee, qui colorait des lignes sans chiffre en
        face, et le repli sur la zone de gauche quand la droite n'a rien)."""
        zone = getattr(self, 'zone_marqueurs', None)
        if zone is None or zone.cachee:
            return {}
        lignes, operations, fonctions = self.donnees(zone.source)
        if zone.natures[0] == 'operations':
            # Les lignes de `def` (totaux des fonctions) aussi, comme en mode horloge.
            return {**fonctions, **operations}
        return lignes

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
        repliee = next((zone for zone in getattr(self, 'zones', ()) if zone.horloges_seules()), None)
        if repliee is not None:
            repliee._basculer_vers('total')
            # ⚠ ENREGISTRER ICI, explicitement : la persistance du glissement se fait au RELACHEMENT
            # de la souris (sinon elle ecrirait a chaque pixel parcouru), et ce chemin-la ne passe pas
            # par la souris. Sans ces deux lignes, la session suivante relirait 'horloges' et
            # replierait la colonne, alors que l'utilisateur vient de la voir deployee.
            sauver_horloges_seules(False)
            sauver_mode_compact(True)

    def _horloges_seules(self):
        """Vrai si la colonne est repliee sur les horloges - donc aucune coloration de lignes.

        Interroge TOUTES les zones : l'etat est partage, mais une zone sans rien a montrer vaut
        'vide' et le masque - ne lire que la zone des marqueurs laissait colorees les lignes d'une
        zone repliee (relecture du 31/10/2026). getattr : le manager existe avant ses zones dans
        certains chemins (tests, construction), et une AttributeError ici effacerait silencieusement
        toute la coloration.
        """
        return any(zone.horloges_seules() for zone in getattr(self, 'zones', ()))

    def refresh(self):
        """Relit le registre et repeint fonds, marge et infobulles."""
        self.lignes = results_for(self.filename)
        self.comptage = operations_for(self.filename)
        self.operations = self.comptage['lignes'] if self.comptage else {}
        self.caches = self.comptage['caches'] if self.comptage else []
        self.fonctions = self.comptage['fonctions'] if self.comptage else {}
        texte = self.editor.toPlainText()
        self.epingle = epingle_for(self.filename, texte)
        self.reference = reference_temps_for(self.filename, texte)
        self.punaises = punaises_for(self.filename, texte)
        self.reference_operations, self.reference_fonctions = rechauffer(
            {**self.epingle, **self.punaises['operations']}, self.punaises['fonctions'])
        self._poser_marges()
        self._apply_decorations()
        self.rafraichir_zones()

    def rafraichir_zones(self):
        for zone in self.zones:
            zone.rafraichir()

    def donnees(self, source):
        """(temps, comptes, totaux des fonctions) de la source d'une zone. La reference des
        comptages est l'epingle ; celle des temps, le plus ancien run de chaque fonction."""
        if source == 'reference':
            # Les punaises passent par-dessus la reference par defaut, ligne par ligne.
            return ({**self.reference, **self.punaises['temps']},
                    self.reference_operations, self.reference_fonctions)
        return self.lignes, self.operations, self.fonctions

    def est_punaisee(self, numero):
        return any(numero in lignes for lignes in self.punaises.values())

    def punaise_identique(self, valeurs):
        return punaise_identique(self.filename, self.editor.toPlainText(), valeurs)

    def basculer_punaise(self, valeurs, etendue=None):
        if self.filename and valeurs:
            basculer_punaise(self.filename, self.editor.toPlainText(), valeurs, etendue)

    def a_des_resultats(self):
        """Vrai si une zone au moins a quelque chose a montrer pour ce fichier."""
        return bool(self.lignes or self.operations or self.fonctions or self.epingle
                    or self.reference or any(self.punaises.values()))

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
            self.editor.decorations.remove_key(CACHES_KEY)
            return
        self._apply_soulignements()

        document = self.editor.document()
        fond = self.couleur_de_fond_neutre()
        ligne_courante = self.ligne_courante()
        decorations = []
        for ligne in self.lignes_colorees():
            heat = self.chaleur(ligne)
            mesure = self.lignes.get(ligne)
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
                couleur = eclaircir(self.couleur_de_fond(heat, fond))
                ordre = DRAW_ORDERS['current_line'] + 1
            else:
                couleur = self.couleur_de_fond(heat, fond)
                ordre = 1
            decoration = TextDecoration(document, start_line=ligne - 1,
                                        end_line=ligne - 1, draw_order=ordre,
                                        full_width=True)
            decoration.set_background(QBrush(couleur))
            # Infobulle UNIQUEMENT sur les lignes de `def` (choix de l'utilisateur) : elle y porte
            # le "propre / cumule", seule info que la couleur (cumulee) ne peut pas donner. Sur une
            # ligne de corps, la marge affiche deja le temps, l'infobulle ferait doublon.
            if mesure is not None and mesure.kind == 'def':
                decoration.tooltip = self.infobulle(ligne, mesure)
            decorations.append(decoration)

        if decorations:
            self.editor.decorations.add_key(DECORATION_KEY, decorations)
        else:
            self.editor.decorations.remove_key(DECORATION_KEY)

    # Plancher de chaleur des soulignements : le froid de l'echelle est le fond de l'editeur, un
    # trait de cette couleur serait invisible. Une instruction cachee merite toujours d'etre vue.
    CHALEUR_MIN_SOULIGNEMENT = 0.35

    def _apply_soulignements(self):
        """Souligne, dans la couleur thermique de leur cout, les instructions qui cachent des
        operations (decision de l'utilisateur, « comme dans l'editeur de QCM »)."""
        document = self.editor.document()
        decorations = []
        for cache in self.caches:
            debut = document.findBlockByNumber(cache.ligne - 1)
            fin = document.findBlockByNumber(cache.fin_ligne - 1)
            if not (debut.isValid() and fin.isValid()):
                continue
            decoration = TextDecoration(
                document, start_pos=debut.position() + cache.colonne,
                end_pos=fin.position() + cache.fin_colonne, draw_order=2)
            decoration.set_as_underlined(couleur_thermique(
                max(self.CHALEUR_MIN_SOULIGNEMENT, cache.heat)))
            decorations.append(decoration)
        if decorations:
            self.editor.decorations.add_key(CACHES_KEY, decorations)
        else:
            self.editor.decorations.remove_key(CACHES_KEY)

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
        theme sans redemarrer Spyder, et l'echelle doit suivre - comme elle suit l'eclaircissement
        du focus (cf. fond_editeur).
        """
        return fond_editeur(self.editor)

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
    # Largeur de la case punaise (utilisateur, 31/10/2026 : « les punais doivent être plus
    # discrettes prendre moin de largeur »). L'icone, elle, garde TAILLE_ICONE et deborde sur les
    # marges vides du glyphe (« trop petites, il fallait surtout ne pas les remplir, diminuer les
    # marges et les griser légèrement ») : cf. _dessiner_punaise.
    TAILLE_PUNAISE = 10
    # Contour de la punaise, en pixels de l'ecran : un fin trait de 1 px, 7 de large, 10 de haut
    # (« trop agrandi les punaise et le trait de contour est trop epais », 31/10/2026 : le glyphe
    # mdi.pin-outline agrandi epaississait son trait avec lui).
    CONTOUR_PUNAISE = ((1, 0), (6, 0), (6, 1), (5, 1), (5, 4), (7, 6), (0, 6), (2, 4), (2, 1),
                       (1, 1))
    HAUTEUR_PUNAISE = 10
    # Opacite du texte des mesures et des punaises : « légèrement grisé » (31/10/2026).
    OPACITE_MESURES = 0.75
    # Marge de part et d'autre du texte de l'en-tete (utilisateur, 31/10/2026 : « il faut tout de
    # meme une petite marge » - PADDING seul collait le libelle au filet).
    MARGE_ENTETE = 6
    # Marqueur « mesurer cette fonction » : l'horloge du profileur de Spyder ou un Σ fuchsia
    # (« pourquoi ne pas utiliser le signe Σ en fuchsia, sans le mettre a gauche du coup »), selon
    # mode_du_marqueur. Le meme marqueur designe les fonctions chronometrees ET comptees : il
    # montre la mesure que le prochain lancement le plus probable fera. Ecartees : les cibles
    # mdi.crosshairs-gps et mdi.bullseye (apercu rendu, non retenues).
    COULEUR_MARQUEUR = QColor(255, 0, 255)

    # Largeur de la poignee de redimensionnement, sur le bord GAUCHE de la colonne (cote code).
    # Un clic dans cette bande fait glisser la largeur ; ailleurs, il pose un marqueur.
    POIGNEE = 4

    def __init__(self, results_manager, rang=1):
        Panel.__init__(self)
        self.results_manager = results_manager
        # Rang de la zone (0 a gauche, 1 a droite) et ce que montre son menu d'en-tete.
        self.rang = rang
        self.source, self.natures = charger_choix_zone(rang)
        # Entree du menu decochee : la zone ne montre plus rien (cf. cacher).
        self.cachee = charger_zone_cachee(rang)
        # Ligne dont la case punaise est survolee : une punaise s'y dessine (cf. _cible_punaise).
        self._survol_punaise = None
        # Ligne dont la punaise est enfoncee : la bascule se fait au RELACHEMENT, sur la meme case.
        self._presse_punaise = None
        # Donnees de la zone, filtrees par source et natures (cf. rafraichir).
        self._lignes = {}
        self._operations = {}
        self._fonctions = {}
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
        # Colonnes du mode compact : temps total, filet, operations totales (celles qui existent).
        self._cols_total = None


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

    # Filet entre la partie temps et la partie operations (mode « Σ ») : `31 ms │ 2·10⁸ op`.
    FILET = '│'

    def _parties(self):
        """Parties que la zone affiche, dans l'ordre : 'temps', 'operations'. Une partie jamais
        mesuree reste absente (decision de l'utilisateur) - ni filet ni colonnes vides. Le
        comptage epingle n'est plus une partie : c'est la reference, montree par l'autre zone
        (cf. ProfileResultsManager.donnees)."""
        parties = []
        if any(m.kind != 'def' for m in self._lignes.values()):
            parties.append('temps')
        if self._operations:
            parties.append('operations')
        return parties

    def _colonnes(self, mode):
        """(partie, alignement) de chaque colonne du mode ('complet' ou 'total'), dans l'ordre de
        _cellules ; partie None pour un filet entre deux parties."""
        alignements = (self._ALIGNEMENTS if mode == 'complet' else (Qt.AlignRight,))
        colonnes = []
        for i, partie in enumerate(self._parties()):
            if i:
                colonnes.append((None, Qt.AlignHCenter))
            colonnes += [(partie, alignement) for alignement in alignements]
        return colonnes

    def _morceaux(self, partie, numero, mode):
        """Cellules de la partie `partie` sur la ligne `numero`, ou None si elle n'y a rien."""
        if partie == 'temps':
            mesure = self._lignes.get(numero)
            if mesure is None or mesure.kind == 'def':
                return None
            if mode == 'complet':
                return list(self._segments(mesure))
            return [format_duree(mesure.seconds)]
        compte = self._operations.get(numero)
        if compte is None:
            return None
        return list(compte.segments()) if mode == 'complet' else [compte.total()]

    def _cellules(self, numero, mode):
        """Textes de la ligne `numero`, une cellule par colonne du mode ; '' ou la partie manque.

        Complet : `20000 × 1,6 µs = 31 ms  │  20000 × 10 000 op = 2·10⁸ op` ; total :
        `31 ms │ 2·10⁸ op`. Un filet n'est dessine qu'entre deux parties qui ont TOUTES DEUX une
        valeur sur la ligne.
        """
        taille = 5 if mode == 'complet' else 1
        cellules = []
        precedents = None
        for i, partie in enumerate(self._parties()):
            morceaux = self._morceaux(partie, numero, mode)
            if i:
                cellules.append(self.FILET if (precedents is not None and morceaux is not None)
                                else '')
            cellules += morceaux if morceaux is not None else [''] * taille
            precedents = morceaux
        return cellules

    def _largeurs_colonnes(self, metrics, mode='complet'):
        """Largeur de chaque colonne du mode, = plus large cellule de son rang."""
        colonnes = self._colonnes(mode)
        largeurs = [0] * len(colonnes)
        # Les lignes de `def` (cProfile) n'entrent pas dans ces colonnes (cf. _cellules) : leur Σ
        # cumule est dessine a part, et dimensionne par _largeur_defs.
        numeros = set(n for n, m in self._lignes.items() if m.kind != 'def')
        numeros.update(self._operations)
        for numero in numeros:
            for i, texte in enumerate(self._cellules(numero, mode)):
                largeurs[i] = max(largeurs[i], metrics.horizontalAdvance(texte))
        for i, (partie, _alignement) in enumerate(colonnes):
            if partie is None:
                # Le filet ne porte rien : il doit au moins tenir sa place, meme si aucune ligne
                # n'a les deux parties voisines a la fois.
                largeurs[i] = max(largeurs[i], metrics.horizontalAdvance(self.FILET))
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
        """Largeur minimale : de quoi montrer le seul temps total, au moins une icone, et
        l'en-tete abrege (« Dern. Σ ▾ », cf. _entete_minimal).

        En dessous, on ne pourrait plus lire la valeur ni poser de marqueur.
        """
        icone = self.TAILLE_ICONE + 2 * self.PADDING
        cols = self._cols_total
        if not cols:
            return icone
        return max(sum(cols) + self.ECART_COLONNES * (len(cols) - 1) + 2 * self.PADDING
                   + self._case_punaise(), icone, self._entete_minimal('total'))

    def _entete_minimal(self, mode):
        """Largeur du plus court en-tete admis : le ▾ seul dans une zone sans resultats, l'abrege
        le plus court sinon - la nature doit rester lisible en compact (utilisateur, 31/10/2026 :
        « pourquoi "Dernier Comptage" est réduit à "Dern." et par "Dernier ∑" ? »)."""
        minimal = ('▾' if mode == 'vide'
                   else LIBELLES_ZONES[self.source, self.natures[0]][-1] + ' ▾')
        return self._case_entete() + self._largeur_libelle(minimal) + 2 * self.MARGE_ENTETE

    def _largeur_complete(self):
        """Largeur necessaire a la formule entiere (cinq sous-colonnes)."""
        if not self._cols or not any(self._cols):
            return 0
        return (sum(self._cols) + self.ECART_COLONNES * (len(self._cols) - 1)
                + 2 * self.PADDING + self._case_punaise())

    def _texte_def(self, mesure, compte=None):
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

        `compte` : total de la fonction en mode « Σ » (LigneComptee, passages = appels), meme
        formule que le temps : `Σ 1 × 676307 op = 676307 op`, ou le total seul en compact. Les
        deux mesures presentes, elles se suivent separees du filet : `Σ 12 ms │ 676307 op`.
        """
        total = self._mode == 'total'
        parties = []
        if mesure is not None and mesure.kind == 'def' and mesure.cumtime is not None:
            parties.append(format_duree(mesure.cumtime) if total
                           else ' '.join(self._segments(mesure)))
        if compte is not None:
            parties.append(compte.total() if total else ' '.join(compte.segments()))
        return (' ' + self.FILET + ' ').join(parties)

    def _largeur_defs(self, metrics):
        """Largeur necessaire aux formules cumulees cProfile (Σ n × moy = cumule) des `def`, ou 0.

        La formule complete "Σ n × temps_par_appel = temps_cumule" (meme format que les lignes)
        est dessinee d'un bloc, calee a droite. Reserve en plus, a gauche, la case du bouton, ou se
        pose le Σ (marqueur, apercu, ou Σ de tete d'une fonction non marquee). Sans ca, la
        colonne serait dimensionnee sur les seuls temps par ligne du line profiler et tronquerait
        les Σ des fonctions non marquees.
        """
        pire = 0
        for numero in set(self._lignes) | set(self._fonctions):
            texte = self._texte_def(self._lignes.get(numero), self._fonctions.get(numero))
            if texte:
                pire = max(pire, metrics.horizontalAdvance(texte))
        if not pire:
            return 0
        # Cases de gauche : la punaise, puis le bouton dans la zone des marqueurs, le Σ de tete
        # dans l'autre.
        return int(pire + self.TAILLE_ICONE + self.PADDING + 2 * self.PADDING
                   + self._case_punaise())

    def porte_marqueurs(self):
        """Vrai pour la zone de droite, qui loge les boutons Σ / horloge des `def`."""
        return self.rang == 1

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
            if not self.porte_marqueurs():
                # Zone sans boutons : rien a cliquer. Repliee, elle disparait - meme vide, l'etat
                # 'horloges' etant partage (_horloges) mais masque par 'vide' dans _mode ; vide,
                # elle garde la place de son menu tant que l'editeur a des resultats, pour qu'on
                # puisse y choisir une autre source.
                manager = self.results_manager
                if self._horloges or self.cachee or not manager.a_des_resultats():
                    return 0
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
        lignes, operations, fonctions = self.results_manager.donnees(self.source)
        if self.cachee:
            lignes = operations = fonctions = {}
        self._lignes = lignes if 'temps' in self.natures else {}
        self._operations = operations if 'operations' in self.natures else {}
        self._fonctions = fonctions if 'operations' in self.natures else {}
        lignes = self._lignes if self.editor is not None else None
        metrics = QFontMetrics(self.editor.font()) if self.editor is not None else None
        a_montrer = lignes or self._operations
        self._cols = self._largeurs_colonnes(metrics) if a_montrer else None
        self._cols_total = self._largeurs_colonnes(metrics, 'total') if a_montrer else None
        self._largeur = self._calculer_largeur(metrics)

        if self.editor is not None:
            self.editor.panels.refresh()
        self.update()

    def _calculer_largeur(self, metrics):
        """Largeur de la zone dans l'etat COURANT (_horloges, _compact), sans rien modifier : lue
        par rafraichir() et, etat simule, par _mode_le_plus_proche()."""
        mode = self._mode
        largeur = self._largeur_du_mode(mode)
        if metrics is None:
            return largeur
        # La marge doit aussi loger les temps cumules "∑ ..." des lignes de `def` (cProfile), qui
        # ne passent pas par les sous-colonnes du line profiler.
        # ⚠ SAUF en mode 'horloges' : rien n'y est ecrit, et ce max() y maintiendrait la colonne
        # large - le repli ne replierait donc rien, exactement la regression du 23/07/2026 sur le
        # mode compact (la formule Σ gardait la colonne large).
        if (self._lignes or self._fonctions) and mode != 'horloges':
            largeur = max(largeur, self._largeur_defs(metrics))
        # L'en-tete le plus court doit tenir, meme apres la case d'un bouton (cf. _entete_minimal) ;
        # au-dela, le libelle se contente de la place des resultats (cf. _texte_entete).
        if largeur and self._a_un_entete():
            largeur = max(largeur, self._entete_minimal(mode))
        return largeur

    def _couleur_demarcation(self):
        """Fond de l'editeur A PEINE eclairci, pour la ligne de separation code / colonne.

        Volontairement TRES DISCRET (demande de l'utilisateur : le bleu/gris du fond de l'editeur
        "un tout petit peu plus clair", juste pour deviner la poignee de redimensionnement). Force
        d'eclaircissement basse (0.10) pour rester proche du fond et preserver sa teinte : un
        melange plus fort virerait vers le blanc et delaverait le bleu. Theme-aware par
        construction.
        """
        return eclaircir(fond_editeur(self.editor), 0.10)

    def sizeHint(self):
        return QSize(self._largeur, self._largeur)

    # ---- Calque par-dessus le code (cf. l'enregistrement dans ProfileResultsManager)
    # ------------------------------------------------------------------------
    def on_install(self, editor):
        super().on_install(editor)
        # Panel.on_install rend les panneaux FLOATING transparents a la souris : les notres
        # recoivent les clics la ou ils peignent, le masque laisse passer le reste.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)

    def geometry(self):
        # Panel.geometry() renvoie un quadruplet pour SON set_geometry, remplace ci-dessous.
        return QWidget.geometry(self)

    def set_geometry(self, crect):
        """Appele par PanelsManager a chaque mise a jour de l'editeur (defilement, frappe) : la
        zone se cale contre le bord droit de la vue, derriere les zones de rang superieur, et son
        masque suit les lignes visibles."""
        vue = self.editor.viewport().geometry()
        droite = vue.right() + 1 - sum(zone._largeur for zone in self._zones()
                                       if zone.rang > self.rang)
        masque = self._masque()
        # Masque vide = AUCUN masque pour Qt (zone opaque partout) : largeur nulle a la place.
        largeur = 0 if masque.isEmpty() else self._largeur
        cadre = QRect(droite - self._largeur, vue.top(), largeur, vue.height())
        if self.geometry() != cadre:
            self.setGeometry(cadre)
        if not masque.isEmpty() and masque != self.mask():
            self.setMask(masque)

    def _masque(self):
        """Ce qui recoit la souris : la partie peinte (_region_opaque) et, sur toute la hauteur,
        la poignee de redimensionnement, que le filet de demarcation signale (utilisateur,
        31/10/2026 : « la ligne de demarcation n'est plus sur tout le script »). Hors de la
        partie peinte, la poignee reste transparente a l'oeil : un enfant Qt sans
        autoFillBackground laisse voir ce qui est dessous la ou il ne peint pas."""
        masque = self._region_opaque()
        if self._largeur:
            masque += QRegion(0, 0, min(self.POIGNEE, self._largeur), self.height() or
                              self.editor.viewport().height())
        return masque

    def _region_opaque(self):
        """Ce que la zone recouvre : les lignes qui portent un resultat, en entier ; sur celles
        d'un `def` sans resultat, la seule case du bouton ; et l'en-tete. Le reste laisse voir le
        code. Les blocs sont parcourus ici, pas lus dans editor.visible_blocks : ce dernier n'est
        mis a jour qu'a la peinture de la vue, APRES l'appel de set_geometry au defilement."""
        masque = QRegion()
        if not self._largeur:
            return masque
        editeur = self.editor
        bloc = editeur.firstVisibleBlock()
        haut = int(editeur.blockBoundingGeometry(bloc).translated(editeur.contentOffset()).top())
        bas_vue = editeur.viewport().height()
        while bloc.isValid() and haut < bas_vue:
            if bloc.isVisible():
                hauteur = self._hauteur_ligne(bloc)
                largeur = self._largeur_opaque(bloc.blockNumber() + 1)
                if largeur:
                    masque += QRegion(0, haut, largeur, hauteur)
                haut += hauteur
            bloc = bloc.next()
        if self._texte_entete():
            masque += QRegion(0, 0, self._largeur, self._hauteur_entete())
        return masque

    def _largeur_opaque(self, numero):
        """Largeur couverte sur la ligne `numero` (0 : le code reste visible)."""
        if self._mode != 'horloges' and (numero in self._lignes or numero in self._operations
                                         or numero in self._fonctions):
            return self._largeur
        cibles = self.targets_manager
        if self.porte_marqueurs() and cibles is not None and cibles.def_line_for(numero) == numero:
            return min(self._largeur, self._x_bouton() + self.TAILLE_ICONE)
        return 0

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._largeur:
            return
        painter = QPainter(self)
        # Palier froid / fond par defaut de la colonne = fond de l'editeur de texte. Une seule
        # lecture, reutilisee pour le fond global et le fond de chaque ligne.
        fond = self.results_manager.couleur_de_fond_neutre()
        # Rien hors des lignes a resultat : le code dessous reste visible (cf. _region_opaque).
        painter.setClipRegion(self._region_opaque())
        painter.fillRect(event.rect(), fond)

        painter.setFont(self.editor.font())
        lignes = self._lignes
        # En mode 'complet', position gauche et largeur de chacune des cinq sous-colonnes.
        cols = self._cols
        mode = self._mode
        xs = self._x_colonnes(cols) if (cols and mode == 'complet') else None
        cols_total = self._cols_total
        xs_total = self._x_colonnes(cols_total) if (cols_total and mode == 'total') else None
        operations = self._operations
        fonctions = self._fonctions
        manager = self.results_manager
        marqueurs = self.porte_marqueurs()
        # Repli sur les horloges : ni temps ni fonds thermiques, seulement les icones (cf. la boucle).
        horloges_seules = (mode == 'horloges')

        # Le texte est en couleur de texte normale (et non thermique) : c'est le FOND de la case
        # qui porte la couleur de la ligne, le chiffre reste donc lisible a toute temperature.
        couleur_texte = self._couleur_grisee()
        # Reference : meme couleur, attenuee - la mesure courante reste la plus lisible.
        if self.source == 'reference':
            couleur_texte.setAlphaF(0.55)
        painter.setPen(couleur_texte)

        # Ligne courante : au lieu de la peindre d'un gris fixe, on ECLAIRCIT son propre fond
        # (demande de l'utilisateur : "gris fonce -> gris clair, colore par line profiler -> meme
        # couleur plus claire"). L'information thermique est ainsi preservee - une ligne profilee
        # sous le curseur reste de sa couleur, simplement eclaircie - et le surlignage s'applique
        # a TOUTES les lignes courantes, y compris timees et return (elles sont mesurees, et
        # l'ancien 'elif' apres le fond thermique ne les atteignait jamais).
        ligne_courante = self.results_manager.ligne_courante()

        couleur_pleine = self._couleur_grisee()
        for haut, numero, bloc in self.editor.visible_blocks:
            hauteur = self._hauteur_ligne(bloc)
            mesure = lignes.get(numero)
            compte = operations.get(numero)
            fonction = fonctions.get(numero)
            if self.source == 'reference':
                # Une mesure punaisee est choisie, pas deduite : pleine couleur.
                painter.setPen(couleur_pleine if manager.est_punaisee(numero) else couleur_texte)
            if horloges_seules:
                # Repli sur les horloges : on traite chaque ligne comme NON MESUREE. Les fonds
                # thermiques et tous les textes - formule Σ des `def` comme temps des lignes de
                # corps - disparaissent alors d'eux-memes, puisque leurs branches testent `mesure`.
                # Seules restent les branches des icones (marqueur, apercu), c'est-a-dire exactement
                # ce que l'utilisateur veut garder. Un seul test ici plutot qu'un `if` dans chaque
                # branche : rien ne peut etre oublie.
                mesure = compte = fonction = None
            heat = None if horloges_seules else self.results_manager.chaleur(numero)
            fond_ligne = (ProfileResultsManager.couleur_de_fond(heat, fond)
                          if heat is not None else None)
            if numero == ligne_courante:
                fond_ligne = eclaircir(fond_ligne if fond_ligne is not None else fond)
            if fond_ligne is not None:
                painter.fillRect(0, haut, self._largeur, hauteur, fond_ligne)

            donnees = bloc.userData()
            marque = (marqueurs and donnees is not None
                      and getattr(donnees, 'profile_target', False))
            apercu = marqueurs and self.line_number_hint == numero
            # Σ de tete dans la case du bouton de la zone de gauche, des qu'une zone QUELCONQUE
            # montre la formule de cette fonction (elle peut n'etre qu'a droite).
            def_mesure = (not horloges_seules and marqueurs
                          and ((manager.lignes.get(numero) is not None
                                and manager.lignes[numero].kind == 'def')
                               or numero in manager.fonctions))
            if (mesure is not None and mesure.kind == 'def') or fonction is not None \
                    or def_mesure:
                # Ligne du `def` : la mesure CUMULEE cProfile, prefixee d'un Σ pour la distinguer
                # du temps par ligne du line profiler. Le texte suit l'etat de la colonne comme
                # celui des lignes du corps - formule "n × temps_moyen = temps_cumule" deployee,
                # temps cumule seul en compact : cf. _texte_def. On utilise Σ (U+03A3, sigma
                # capitale GRECQUE, une lettre presente dans la police de code, calee sur la ligne
                # de base) et NON ∑ (U+2211, l'operateur "n-ary summation") : ce dernier, souvent
                # absent de la police de code, est rendu par une police de repli a la ligne de base
                # differente et tombe visiblement plus bas que les chiffres (defaut signale par
                # l'utilisateur). Une icone cohabite a GAUCHE de la colonne (marqueur si mesuree,
                # apercu au survol, cf. _dessiner_icone) ; la formule reste calee a droite.
                icone = 'marqueur' if marque else ('apercu' if apercu else None)
                # Formule calee a DROITE, en graisse normale. Le Σ de tete, lui, ne la suit plus :
                # il se pose dans la case du bouton, a GAUCHE, en gras et couleur de texte quand la
                # fonction n'est pas marquee (le marqueur fuchsia ou l'apercu y prend sa place
                # sinon). Sans cela, une fonction non marquee montrait son Σ a droite des que la
                # colonne s'elargissait (l'utilisateur, 31/10/2026 : « le Σ reste a droite »).
                formule = self._texte_def(mesure, fonction)
                police = self.editor.font()
                w_formule = QFontMetrics(police).horizontalAdvance(formule)
                painter.setFont(police)
                painter.drawText(self._largeur - self.PADDING - w_formule, haut, w_formule,
                                 hauteur, int(Qt.AlignLeft | Qt.AlignVCenter), formule)
                if icone:
                    self._dessiner_icone(painter, haut, hauteur, icone)
                elif marqueurs or formule:
                    # Σ de tete aussi dans la zone sans boutons, devant sa propre formule
                    # (utilisateur, 31/10/2026 : « il faut mettre le Σ sur les ligne de def à
                    # gauche de chacune des deux colonnes »).
                    police_gras = QFont(police)
                    police_gras.setBold(True)
                    painter.setFont(police_gras)
                    painter.drawText(QRect(self._x_bouton(), haut, self.TAILLE_ICONE, hauteur),
                                     int(Qt.AlignCenter), 'Σ')
                    painter.setFont(police)
            elif marque:
                # Marqueur sur une ligne du `def` sans mesure cProfile (pas de .prof) : ancien cas.
                self._dessiner_icone(painter, haut, hauteur, 'marqueur')
            elif apercu:
                self._dessiner_icone(painter, haut, hauteur, 'apercu')
            elif mesure is not None or compte is not None:
                # Ligne de corps : temps (line profiler) et/ou operations (mode « Σ »), formule
                # complete ou totaux seuls, une cellule par colonne (cf. _cellules).
                if xs is not None:
                    trace = (xs, cols, 'complet')
                elif xs_total is not None:
                    trace = (xs_total, cols_total, 'total')
                else:
                    trace = None
                if trace is not None:
                    xs_mode, cols_mode, nom_mode = trace
                    for texte, x, larg, (partie, alin) in zip(
                            self._cellules(numero, nom_mode), xs_mode, cols_mode,
                            self._colonnes(nom_mode)):
                        painter.drawText(x, haut, larg, hauteur,
                                         int(alin | Qt.AlignVCenter), texte)
            if self._presse_punaise == numero or self._survol_punaise == numero or (
                    self._a_punaiser(numero) and manager.est_punaisee(numero)):
                self._dessiner_punaise(painter, haut, hauteur, self._presse_punaise == numero)

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
        # Le filet, lui, court sur TOUTE la hauteur, lignes transparentes comprises.
        painter.setClipping(False)
        if (lignes or operations) and not horloges_seules:
            self._dessiner_demarcation(painter, event.rect())
        self._dessiner_entete(painter, fond)

    # ---- Punaise : sa case a GAUCHE de la zone, le bouton Σ juste apres
    # ------------------------------------------------------------------------
    # Sur l'unite jusqu'au 31/10/2026 ; l'utilisateur : « tu décale le bouton somme à droite, pour
    # pouvoir ajouter le bouton de punaise à gauche plutot que sur l'unité ». Ecarte : la garder
    # sur l'unite, une cible de la largeur d'un « s » qui cachait la valeur au survol.
    def _case_punaise(self):
        """Largeur de la case de la punaise, dans toute zone qui montre des valeurs."""
        return self.TAILLE_PUNAISE + self.PADDING

    def _x_bouton(self):
        """Bord gauche de la case du bouton (Σ, horloge, Σ de tete) : apres la case de la
        punaise quand la zone montre des valeurs, au bord sinon (rien a punaiser)."""
        decale = self._mode in ('complet', 'total')
        return self.PADDING + (self._case_punaise() if decale else 0)

    def _a_punaiser(self, numero):
        """Vrai si la ligne `numero` montre une valeur que sa case de punaise peut punaiser."""
        return self._mode in ('complet', 'total') and (
            numero in self._lignes or numero in self._operations or numero in self._fonctions)

    def _cible_punaise(self, event):
        """Ligne dont la case de punaise est sous la souris, ou None."""
        x = int(event.position().x())
        if not x <= self.PADDING + self.TAILLE_PUNAISE:
            return None
        numero = self.editor.get_linenumber_from_mouse_event(event)
        return numero if self._a_punaiser(numero) else None

    def _etendue_punaise(self, numero):
        """(premiere, derniere) lignes que depunaise le clic : la fonction entiere sur un `def`."""
        mesure = self._lignes.get(numero)
        if (mesure is not None and mesure.kind == 'def') or numero in self._fonctions:
            return etendue_fonction(self.editor.toPlainText(), numero)
        return numero, numero

    def _valeurs_punaise(self, numero):
        """{(nature, ligne): mesure} que punaise un clic dans la case : ce que la zone montre de la
        ligne, ou sur le `def` la fonction entiere (toutes ses lignes et son total)."""
        mesure = self._lignes.get(numero)
        if not ((mesure is not None and mesure.kind == 'def') or numero in self._fonctions):
            valeurs = {}
            if mesure is not None:
                valeurs[('temps', numero)] = mesure
            if numero in self._operations:
                valeurs[('operations', numero)] = self._operations[numero]
            return valeurs
        premiere, derniere = etendue_fonction(self.editor.toPlainText(), numero)
        valeurs = {}
        for ligne in range(premiere, derniere + 1):
            if ligne in self._lignes:
                valeurs[('temps', ligne)] = self._lignes[ligne]
            if ligne in self._operations:
                valeurs[('operations', ligne)] = self._operations[ligne]
        if numero in self._fonctions:
            valeurs[('fonctions', numero)] = self._fonctions[numero]
        return valeurs

    def _couleur_grisee(self):
        """Couleur du texte des mesures, legerement grisee, que la punaise reprend
        (utilisateur, 31/10/2026 : « comme le résultat des mesures, qui doit être légèrement
        grisé »). Par transparence et non par melange avec le fond de l'editeur : le chiffre
        reste ainsi sur le fond thermique de sa case."""
        couleur = QColor(self.editor.palette().windowText().color())
        couleur.setAlphaF(self.OPACITE_MESURES)
        return couleur

    def _dessiner_punaise(self, painter, haut, hauteur, presse):
        """La punaise dans sa case, toujours creuse (utilisateur, 31/10/2026 : « il fallait
        surtout ne pas les remplir »), grisee comme les mesures (_couleur_grisee) ; plus blanche
        pendant l'appui seulement (« la couleur doit devenir plus blanche pendant le clic, pas
        pendant le survol »)."""
        couleur = (eclaircir(QColor(self.editor.palette().windowText().color()), 0.5) if presse
                   else self._couleur_grisee())
        # Dessinee a la main et non par une icone : un glyphe ne garde pas son trait fin a cette
        # taille. Demi-pixel ajoute : le trait de 1 px tombe pile sur une colonne de l'ecran.
        x = self.PADDING + (self.TAILLE_PUNAISE - 7) // 2 + 0.5
        y = haut + (hauteur - self.HAUTEUR_PUNAISE) // 2 + 0.5
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(couleur)
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(QPolygonF([QPointF(x + px, y + py)
                                       for px, py in self.CONTOUR_PUNAISE]))
        painter.drawLine(QPointF(x + 3.5, y + 6), QPointF(x + 3.5, y + self.HAUTEUR_PUNAISE - 1))
        painter.restore()

    # ---- En-tete : le menu de la zone, sur la PREMIERE ligne visible
    # ------------------------------------------------------------------------
    # Pose sur la premiere ligne de l'editeur et non dans une bande au-dessus (decision de
    # l'utilisateur, 31/10/2026 : « sur la meme ligne que la premiere ligne de code, qui de toute
    # facon est cachee par les deux zones ») : rien ne decale le code. L'en-tete reste fixe au
    # defilement ; les resultats de la premiere ligne visible passent dessous.
    def _texte_entete(self):
        """Libelle du menu, puis ▾ : le plus long qui tient dans TOUTE la largeur de la zone,
        (« le text ne prend pas toute la largeur qu'il pourrait prendre ») ; le ▾ seul si aucun ne tient. Lu par
        rafraichir() AVANT l'elargissement pour l'en-tete : la largeur vaut alors celle des
        resultats, un en-tete n'elargit donc jamais la zone pour lui seul. Compacte ou non, la
        zone recoit le libelle le plus long qui tient."""
        if not self._a_un_entete():
            return ''
        place = self._largeur - 2 * self.MARGE_ENTETE
        for libelle in LIBELLES_ZONES[self.source, self.natures[0]]:
            if self._largeur_libelle(libelle + ' ▾') <= place:
                return libelle + ' ▾'
        return '▾'

    def _a_un_entete(self):
        """Source unique, lue par _texte_entete et _calculer_largeur. Replie, ou zone des
        marqueurs sans resultats : la ligne 1 reste aux boutons - meme si l'autre zone est
        cachee, qui se rouvre alors en tirant la poignee (cf. _zone_a_rouvrir ; utilisateur,
        31/10/2026 : « il ne faut pas laisser le ▾ mais juste permettre de réagrandir la zone »)."""
        mode = self._mode
        return not (mode == 'horloges' or (mode == 'vide' and self.porte_marqueurs()))

    def _zone_a_rouvrir(self):
        """L'autre zone, si elle est cachee - ou repliee sur les horloges, largeur nulle - alors que
        celle-ci n'a rien a redimensionner : tirer la poignee vers le code la rouvre, sans quoi
        plus rien ne la rouvrirait, cette zone vide n'ayant pas de menu (relecture du 31/10/2026 ;
        utilisateur, meme jour : « quand je n'affiche un resultat que dans la zone de gauche et que
        je reduis la zone, je n'arrive pas a la reagrandir »)."""
        autre = self._autre_zone()
        if (autre is not None and (autre.cachee or autre.horloges_seules())
                and not self._largeur_complete()):
            return autre
        return None

    def _largeur_libelle(self, texte):
        """Largeur de `texte` en police du code, ICONE_HORLOGE comptee a la taille de l'icone."""
        metrics = QFontMetrics(self.editor.font())
        return (metrics.horizontalAdvance(texte.replace(ICONE_HORLOGE, ''))
                + texte.count(ICONE_HORLOGE) * min(self.TAILLE_ICONE, metrics.height()))

    def _hauteur_entete(self):
        """Hauteur d'une ligne de l'editeur (celle de son premier bloc visible)."""
        bloc = self.editor.firstVisibleBlock()
        if bloc.isValid():
            return self._hauteur_ligne(bloc)
        return QFontMetrics(self.editor.font()).height()

    def _case_entete(self):
        """Place de la case des boutons, dans la zone qui les porte."""
        return self.TAILLE_ICONE + self.PADDING if self.porte_marqueurs() else 0

    def _sur_l_entete(self, event):
        # Tout l'en-tete, du bord gauche de la zone, meme au-dessus du bouton d'un `def` en
        # premiere ligne : il se decalait a droite de ce bouton au defilement (utilisateur,
        # 31/10/2026 : « le menu de choix du profilage à afficher se décale à droite des boutons
        # ∑ »). Ce `def` se marque en defilant d'une ligne.
        return bool(self._texte_entete()) and int(event.position().y()) < self._hauteur_entete()

    def _dessiner_entete(self, painter, fond):
        texte = self._texte_entete()
        if not texte:
            return
        hauteur = self._hauteur_entete()
        painter.fillRect(0, 0, self._largeur, hauteur, eclaircir(fond, 0.06))
        painter.setFont(self.editor.font())
        painter.setPen(self.editor.palette().windowText().color())
        # Libelle cale a GAUCHE (« pourquoi son contenu n'est pas aligné à gauche ? »), le ▾ au
        # bord DROIT (« le triangle n'est plus à droite »), utilisateur, 31/10/2026.
        cadre = QRect(self.MARGE_ENTETE, 0, self._largeur - 2 * self.MARGE_ENTETE, hauteur)
        libelle = texte[:-1].rstrip()
        painter.drawText(cadre, int(Qt.AlignLeft | Qt.AlignVCenter),
                         libelle.replace(ICONE_HORLOGE, ''))
        if libelle.endswith(ICONE_HORLOGE):
            # L'horloge des boutons (icone 'profiler'), pas un glyphe que la police n'a peut-etre pas.
            from spyder.utils.icon_manager import ima
            cote = min(self.TAILLE_ICONE, QFontMetrics(self.editor.font()).height())
            ima.icon('profiler').paint(painter, QRect(
                cadre.left() + self._largeur_libelle(libelle) - cote,
                (hauteur - cote) // 2, cote, cote))
        # Le ▾ grise comme les mesures (utilisateur, 31/10/2026 : « et la flèche du menu aussi un
        # peu grisée »).
        painter.setPen(self._couleur_grisee())
        painter.drawText(cadre, int(Qt.AlignRight | Qt.AlignVCenter), '▾')

    def _zones(self):
        return getattr(self.results_manager, 'zones', (self,))

    def _autre_zone(self):
        return next((zone for zone in self._zones() if zone is not self), None)

    def _ouvrir_menu(self, position_globale):
        """Menu de la zone : quatre entrees exclusives, une par couple (source, nature) ; DECOCHER
        l'entree cochee cache la zone (utilisateur, 31/10/2026 : « plutot qu'une entrée ∅ ne
        peut-on pas décoché l'entrée cochée ? »). Si l'autre zone est cachee, ses entrees suivent,
        apres un separateur :
        c'est par la qu'on la reaffiche (utilisateur, 31/10/2026 : « on ne peut pas fusionner les
        deux menu avec un séparateur ? » - une bande etroite pour la rouvrir aurait laisse deux
        filets serres, une entree « Reafficher l'autre zone » a ete ecartee pour ce menu-ci)."""
        menu = QMenu(self)
        autre = self._autre_zone()
        zones = [self] + ([autre] if autre is not None and autre.cachee else [])
        for numero, zone in enumerate(zones):
            if numero:
                menu.addSeparator()
            for (source, nature), (libelle, *_abreges) in LIBELLES_ZONES.items():
                action = menu.addAction(libelle)
                action.setCheckable(True)
                action.setChecked(not zone.cachee
                                  and (source, nature) == (zone.source, zone.natures[0]))
                action.setData((zone.rang, source, nature))
        choix = menu.exec(position_globale)
        if choix is not None and choix.data():
            rang, source, nature = choix.data()
            zone = next(zone for zone in zones if zone.rang == rang)
            # Jamais les deux zones cachees : il ne resterait plus de menu pour les rouvrir.
            if not choix.isChecked() and not (autre is not None and autre.cachee):
                zone.cacher_ou_ceder_la_place()
            else:
                zone.appliquer_choix(source, nature)

    def appliquer_choix(self, source, nature):
        """Montre dans la zone la mesure `nature` ('temps' | 'operations') de la `source`
        ('reference' | 'derniere'). Enregistre, puis repeint."""
        self.source, self.natures = source, (nature,)
        sauver_choix_zone(self.rang, self.source, self.natures)
        if self.cachee:
            self.cachee = False
            sauver_zone_cachee(self.rang, False)
        self._rafraichir_les_zones()

    def cacher(self):
        """Entree cochee decochee : la zone ne montre plus rien. Sans boutons, sa largeur tombe a zero, filet compris ;
        celle des boutons ne garde que leur case. Elle se rouvre par le menu de l'autre zone, ou, si
        celle-ci n'en a pas (vide), en tirant sa poignee vers le code."""
        self.cachee = True
        sauver_zone_cachee(self.rang, True)
        self._rafraichir_les_zones()

    def cacher_ou_ceder_la_place(self):
        """Decocher la zone de DROITE n'y laisse pas un trou contre le bord : elle prend le choix
        de la gauche, qui se cache en gardant l'ancien choix de la droite - la rouvrir rend donc ce
        qui vient d'etre cache, a gauche (utilisateur, 31/10/2026 : « quand on decoche la zone de
        droite, on ne devrait pas faire que celle de gauche passe a droite ? », puis « oui fais
        l'echange » ; ecarte : cacher la droite seule). Les boutons et le fond des lignes restent
        ainsi avec des resultats en face."""
        autre = self._autre_zone()
        if not self.porte_marqueurs() or autre is None or autre.cachee:
            self.cacher()
            return
        source, nature = self.source, self.natures[0]
        self.appliquer_choix(autre.source, autre.natures[0])
        autre.source, autre.natures = source, (nature,)
        sauver_choix_zone(autre.rang, autre.source, autre.natures)
        autre.cacher()

    def _rafraichir_les_zones(self):
        """Cacher ou rouvrir une zone change l'en-tete de l'AUTRE (cf. _a_un_entete) : les deux
        se recalculent - et les fonds du code, qui suivent la zone de droite (cf.
        _mesures_du_fond) : d'ou refresh() du manager, qui rafraichit aussi les zones."""
        self.results_manager.refresh()

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
        """Icone centree verticalement et calee a GAUCHE de la colonne, cote code.

        A droite jusqu'au 31/10/2026 (meme bord droit net que les durees) ; passee a gauche a la
        demande de l'utilisateur : « le bouton somme clicable [...] peut-il plutot etre a gauche
        qu'a droite ? [...] a gauche de sa zone, pas de l'editeur ». La largeur reservee par
        _largeur_defs ne change pas : seule la place de l'icone dans la colonne bouge.
        """
        painter.save()
        mode = mode_du_marqueur()
        if nom == 'apercu':
            # Apercu au survol : le meme marqueur, estompe. Le Σ, trait fin, l'est moins : a 0.3 il
            # etait « a peine visible » (l'utilisateur, 30/10/2026), l'icone pleine de l'horloge non.
            painter.setOpacity(0.3 if mode == 'temps' else 0.7)
        if mode == 'temps':
            # L'icone nommee 'profiler' de Spyder, celle du bouton « Profiler le fichier » : relue
            # par ima.icon() pour suivre le theme.
            from spyder.utils.icon_manager import ima
            cote = min(self.TAILLE_ICONE, hauteur)
            ima.icon('profiler').paint(painter, QRect(
                self._x_bouton(), haut + (hauteur - cote) // 2, cote, cote))
        else:
            # Σ (U+03A3) de la police de code, en gras : meme ligne de base que les chiffres (cf.
            # le Σ des `def` dans paintEvent, et pourquoi pas ∑ U+2211).
            police = QFont(self.editor.font())
            police.setBold(True)
            painter.setFont(police)
            painter.setPen(self.COULEUR_MARQUEUR)
            painter.drawText(QRect(self._x_bouton(), haut, self.TAILLE_ICONE, hauteur),
                             int(Qt.AlignCenter), 'Σ')
        painter.restore()

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
        # Etat PARTAGE par les deux zones : il pilote aussi la coloration des lignes, unique.
        for zone in self._zones():
            zone._horloges = horloges
            zone._compact = compact
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

        Largeur des DEUX zones ensemble, l'etat etant partage (cf. _basculer_vers), et telle que
        rafraichir() la poserait (formules des `def`, en-tete compris) : comparee a une largeur
        visee prise depuis le bord droit FIXE des zones (cf. mouseMoveEvent). Mesuree depuis le
        bord droit de la zone tiree et sur ses seules largeurs, la zone de gauche voyait ce bord
        bouger quand celle de droite changeait d'etat, et oscillait d'un etat a l'autre a chaque
        pixel (utilisateur, 31/10/2026 : « il y a beaucoup de clignotements, disparitions et
        réapartion des zones de resultat de profiling quand on glisse la ligne de séparation »).
        """
        zones = self._zones()
        etat = [(zone._horloges, zone._compact) for zone in zones]
        metrics = QFontMetrics(self.editor.font())
        candidats = []
        try:
            for mode in ('complet', 'total', 'horloges'):
                for zone in zones:
                    zone._horloges, zone._compact = mode == 'horloges', mode == 'total'
                candidats.append((mode, sum(zone._calculer_largeur(metrics) for zone in zones)))
        finally:
            for zone, (horloges, compact) in zip(zones, etat):
                zone._horloges, zone._compact = horloges, compact
        meilleur, distance_min = candidats[0][0], None
        for mode, largeur in candidats:
            distance = abs(largeur_visee - largeur)
            if distance_min is None or distance < distance_min:
                meilleur, distance_min = mode, distance
        return meilleur

    def _clic_sur_bouton_horloge(self, event, lineno):
        """Vrai si le clic tombe sur l'icone (le "bouton horloge") d'une ligne de definition.

        Le bouton n'existe que sur une ligne de `def` - la ou un marqueur se pose - et occupe la
        bande de l'icone, calee a gauche de la colonne. Ailleurs, un clic selectionne la ligne.
        """
        if self.targets_manager is None:
            return False
        if self.targets_manager.def_line_for(lineno) != lineno:
            return False
        x = event.position().x()
        return self._x_bouton() - self.PADDING <= x <= self._x_bouton() + self.TAILLE_ICONE

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
        if self._sur_l_entete(event):
            self._ouvrir_menu(event.globalPosition().toPoint())
            return
        if self._sur_la_poignee(event) and (self._largeur_complete() or self._zone_a_rouvrir()):
            self._redimensionne = True
            return
        cible = self._cible_punaise(event)
        if cible is not None:
            self._presse_punaise = cible
            self.update()
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
            # Bord droit des zones ensemble : fixe, quel que soit l'etat (cf.
            # _mode_le_plus_proche).
            vue = self.editor.viewport()
            bord_droit = vue.mapToGlobal(QPoint(vue.width(), 0)).x()
            largeur_visee = bord_droit - event.globalPosition().toPoint().x()
            autre = self._zone_a_rouvrir()
            if autre is not None:
                # Tiree d'une icone vers le code : la zone cachee revient, avec son choix ; la
                # repliee, a l'etat intermediaire (enregistre au relachement).
                if largeur_visee > sum(zone._largeur for zone in self._zones()) + self.TAILLE_ICONE:
                    if autre.cachee:
                        autre.appliquer_choix(autre.source, autre.natures[0])
                    else:
                        self._basculer_vers('total')
            elif self._largeur_complete():
                self._basculer_vers(self._mode_le_plus_proche(largeur_visee))
            return

        # Curseur en fleche horizontale au-dessus de la poignee : signale qu'on peut tirer.
        survol = None if self._sur_l_entete(event) else self._cible_punaise(event)
        if survol != self._survol_punaise:
            self._survol_punaise = survol
            self.update()
        if self._sur_la_poignee(event):
            self.setCursor(Qt.SplitHCursor)
        else:
            self.setCursor(Qt.PointingHandCursor if survol else Qt.ArrowCursor)

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
        presse, self._presse_punaise = self._presse_punaise, None
        if presse is not None:
            if self._cible_punaise(event) == presse:
                self.results_manager.basculer_punaise(self._valeurs_punaise(presse),
                                                      self._etendue_punaise(presse))
            self.update()

    def leaveEvent(self, event):
        self.setCursor(Qt.ArrowCursor)
        if self._survol_punaise is not None:
            self._survol_punaise = None
            self.update()
        if self.line_number_hint is not None:
            self.line_number_hint = None
            self.update()

    def wheelEvent(self, event):
        """Laisse la molette faire defiler l'editeur au lieu de s'arreter sur la marge."""
        self.editor.wheelEvent(event)
