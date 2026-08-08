# -*- coding: utf-8 -*-
"""Marqueurs "profiler cette fonction" : le clic, le survol, et la traduction en cibles kernprof.

CE QUE CE TEST COUVRE
Tout ce que je demandais jusqu'ici a l'utilisateur de verifier a la souris. Le panneau est
installe sur une VRAIE CodeEditor de Spyder, et les clics sont de vrais evenements Qt envoyes
par QTest a la position calculee de la ligne visee - pas des appels directs a toggle_target(),
qui court-circuiteraient justement la traduction position -> numero de ligne.

POURQUOI CES CAS-LA
Chacun correspond a un defaut reellement rencontre, ou a une regle que le code doit tenir :
  - le marqueur se pose sur la ligne du `def`, jamais sur la ligne cliquee : line_profiler ne
    mesure que des fonctions entieres (remarque de l'utilisateur, 21/07/2026) ;
  - rien ne se pose, et rien ne s'affiche au survol, sur une ligne qui n'accepte pas de
    marqueur (seconde remarque du meme jour) ;
  - le BlockUserData existant est preserve : un point d'arret et un marqueur cohabitent sur la
    meme ligne, et poser l'un ne doit pas effacer l'autre ;
  - les marqueurs suivent les insertions de lignes, propriete qui vient du portage de l'etat
    sur les blocs Qt et qui casserait si on stockait des numeros de ligne nus ;
  - un ancien marqueur enregistre au milieu d'un corps de fonction migre sur le `def` sans
    etre perdu.

CE QUE CE TEST NE COUVRE PAS
Le lancement reel de kernprof par le plugin (3 lignes injectees dans widgets.py), et le rendu
graphique des icones. La resolution des cibles, elle, est verifiee ici de bout en bout sur un
paquet temporaire ecrit sur le disque.

ISOLATION
CONF est remplace par un dictionnaire en memoire : ce test n'ouvre, ne lit et n'ecrit JAMAIS la
configuration reelle de Spyder. Sans cela il ecraserait les marqueurs de l'utilisateur - et la
regle du depot est qu'on n'ecrit pas dans la configuration d'une application qui tourne.

Lancement :
    QT_QPA_PLATFORM=offscreen python tests/test_profile_targets.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QPoint, Qt  # noqa: E402
from qtpy.QtTest import QTest  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from spyder.plugins.editor.widgets.codeeditor import CodeEditor  # noqa: E402
from spyder.plugins.editor.utils.editor import BlockUserData  # noqa: E402

from spyder_line_profiler.spyder import profile_targets as pt  # noqa: E402


SOURCE = '''def lente(n):
    total = 0
    for i in range(n):
        total += i * i
    return total


def rapide(n):
    return n * n


class Calc:
    def methode(self, n):
        s = 0
        for i in range(n):
            s += i
        return s
'''

DEF_LENTE = 1
DEF_RAPIDE = 8
DEF_METHODE = 13


class ConfEnMemoire:
    """Remplace spyder.config.manager.CONF le temps du test.

    Meme interface que celle utilisee par profile_targets : get(section, option, default) et
    set(section, option, value). Rien n'atteint le disque.
    """

    def __init__(self):
        self.valeurs = {}

    def get(self, section, option, default=None):
        # Copie : le vrai CONF renvoie une valeur deserialisee a chaque appel, donc muter le
        # retour ne doit pas modifier ce qui est stocke. Sans cette copie, un test pourrait
        # passer grace a un effet de bord que le vrai CONF n'a pas.
        valeur = self.valeurs.get((section, option), default)
        return dict(valeur) if isinstance(valeur, dict) else valeur

    def set(self, section, option, value):
        self.valeurs[(section, option)] = value


def nouvel_editeur(source=SOURCE, filename=None):
    """Une CodeEditor Python posee, dimensionnee et mise en page, avec notre marge installee."""
    editor = CodeEditor(None)
    editor.setup_editor(language='Python',
                        filename=filename or '/tmp/test_profile_targets.py')
    editor.set_text(source)
    editor.resize(700, 600)
    editor.show()
    # Sans ce tour de boucle, la mise en page des blocs n'est pas faite et toutes les lignes
    # se retrouvent a la meme ordonnee : les clics tomberaient tous sur la ligne 1.
    QApplication.processEvents()
    # attach_editor() plutot que ProfileTargetsManager(editor) : c'est le chemin reel, et depuis
    # la fusion des deux marges en une seule colonne, c'est lui qui cable le manager sur le
    # panneau. Le construire seul donnerait un manager sans marge, donc sans clic possible.
    pt.attach_editor(editor)
    manager = editor.profile_targets_manager
    QApplication.processEvents()
    return editor, manager


def y_de_la_ligne(editor, lineno):
    """Ordonnee du milieu de la ligne `lineno` dans le systeme de coordonnees de l'editeur."""
    block = editor.document().findBlockByNumber(lineno - 1)
    haut = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
    hauteur = editor.blockBoundingRect(block).height()
    return int(haut + hauteur / 2)


def clic_dans_la_marge(editor, manager, lineno):
    """Clique le BOUTON HORLOGE de la fonction contenant `lineno` (basculer son marqueur).

    Depuis que la colonne selectionne la ligne au clic hors du bouton (demande de
    l'utilisateur), basculer un marqueur passe par un clic sur l'icone, calee a droite, sur la
    ligne du `def`. Le helper vise donc l'icone de def_line_for(lineno) - le `def` de la fonction
    contenant la ligne -, ou `lineno` lui-meme si la ligne n'appartient a aucune fonction (le
    clic ne posera alors rien, ce qui est justement ce que certains tests verifient).
    """
    panel = manager.panel
    ligne_def = manager.def_line_for(lineno) or lineno
    x = panel.width() - panel.PADDING - panel.TAILLE_ICONE // 2
    QTest.mouseClick(panel, Qt.LeftButton,
                     pos=QPoint(x, y_de_la_ligne(editor, ligne_def)))
    QApplication.processEvents()


def survol_de_la_marge(editor, manager, lineno):
    """Deplace la souris dans la marge a la hauteur de `lineno`, renvoie l'apercu affiche."""
    QTest.mouseMove(manager.panel,
                    QPoint(manager.panel.width() // 2, y_de_la_ligne(editor, lineno)))
    # QTest.mouseMove ne declenche pas toujours mouseMoveEvent sur un widget sans souris reelle
    # (aucun curseur physique en offscreen) : on appelle donc le gestionnaire avec un evenement
    # construit, ce qui teste bien NOTRE code et pas celui de Qt.
    from qtpy.QtGui import QMouseEvent
    from qtpy.QtCore import QPointF
    position = QPointF(manager.panel.width() // 2, y_de_la_ligne(editor, lineno))
    manager.panel.mouseMoveEvent(
        QMouseEvent(QMouseEvent.Type.MouseMove, position, Qt.NoButton, Qt.NoButton,
                    Qt.NoModifier))
    return manager.panel.line_number_hint


# =============================================================================
# 1. Le clic pose le marqueur sur la ligne du `def`
# =============================================================================

def test_un_clic_sur_le_def_pose_le_marqueur_sur_ce_def():
    editor, manager = nouvel_editeur()
    clic_dans_la_marge(editor, manager, DEF_LENTE)
    assert manager.get_targets() == [DEF_LENTE], manager.get_targets()


def test_un_clic_dans_le_corps_remonte_le_marqueur_sur_le_def():
    editor, manager = nouvel_editeur()
    # Ligne 4 = "total += i * i", au coeur de la boucle de `lente`.
    clic_dans_la_marge(editor, manager, 4)
    assert manager.get_targets() == [DEF_LENTE], manager.get_targets()


def test_un_clic_dans_une_methode_remonte_sur_le_def_de_la_methode():
    editor, manager = nouvel_editeur()
    # Ligne 16 = "s += i", dans Calc.methode. Ne doit PAS remonter jusqu'a la classe.
    clic_dans_la_marge(editor, manager, 16)
    assert manager.get_targets() == [DEF_METHODE], manager.get_targets()


def test_un_second_clic_retire_le_marqueur():
    editor, manager = nouvel_editeur()
    clic_dans_la_marge(editor, manager, 4)
    assert manager.get_targets() == [DEF_LENTE]
    # Clic sur une AUTRE ligne de la meme fonction : doit retirer, pas ajouter un doublon.
    clic_dans_la_marge(editor, manager, 2)
    assert manager.get_targets() == [], manager.get_targets()


def test_un_clic_hors_bouton_selectionne_la_ligne_et_place_le_curseur_a_la_fin():
    """Demande de l'utilisateur : un clic dans la colonne, hors du bouton horloge, selectionne la
    ligne dans l'editeur et met le curseur a la fin - il ne bascule PAS de marqueur."""
    import pickle
    from line_profiler.line_profiler import LineStats
    from spyder_line_profiler.spyder import profile_results as pr

    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "m.py")
        Path(chemin).write_text(SOURCE)
        # Des resultats pour que la colonne soit large : sinon elle se reduit a une icone et il
        # n'y a pas de zone "hors bouton" ou cliquer.
        stats = LineStats({(chemin, 1, 'lente'):
                           [(2, 1, 400), (3, 300001, 40000000), (4, 300000, 60000000)]}, 1e-9)
        cible = Path(dossier) / "r"
        with open(cible, 'wb') as flux:
            pickle.dump(stats, flux)
        pr._RESULTS = {}
        pr._CHARGE = False
        pr.publish(str(cible))

        editor, manager = nouvel_editeur(filename=chemin)
        panel = manager.panel
        x = panel.POIGNEE + 5  # hors de la poignee (gauche) ET de l'icone (droite)
        assert x < panel._largeur - panel.PADDING - panel.TAILLE_ICONE, "pas assez large"

        # Ligne 1 = le `def` lui-meme : le cas explicitement cite par l'utilisateur.
        QTest.mouseClick(panel, Qt.LeftButton,
                         pos=QPoint(x, y_de_la_ligne(editor, DEF_LENTE)))
        QApplication.processEvents()

        assert manager.get_targets() == [], "un clic hors bouton ne doit rien basculer"
        assert editor.textCursor().blockNumber() + 1 == DEF_LENTE
        assert editor.textCursor().atBlockEnd(), "le curseur doit etre a la fin de la ligne"


def test_un_clic_sur_le_bouton_horloge_bascule_bien_le_marqueur():
    """Complement du test precedent : sur l'icone, le clic bascule le marqueur (ne selectionne
    pas). C'est ce que verifient deja les autres tests via clic_dans_la_marge."""
    editor, manager = nouvel_editeur()
    clic_dans_la_marge(editor, manager, DEF_LENTE)
    assert manager.get_targets() == [DEF_LENTE]


def test_plusieurs_fonctions_marquees_coexistent():
    editor, manager = nouvel_editeur()
    clic_dans_la_marge(editor, manager, 3)
    clic_dans_la_marge(editor, manager, 15)
    assert manager.get_targets() == [DEF_LENTE, DEF_METHODE], manager.get_targets()


def test_le_clic_redessine_la_marge_immediatement():
    """Regression introduite en fusionnant les deux marges, puis corrigee.

    L'ancien ProfileTargetsPanel branchait sig_repaint_targets dans son on_state_changed(),
    methode disparue avec lui. Sans le report de cette connexion, poser un marqueur ne
    redessinait rien : l'icone n'apparaissait qu'au premier evenement repeignant la marge pour
    une autre raison - un mouvement de souris, typiquement. Le clic semblait sans effet.

    On verifie ici la consequence observable : la marge doit s'ouvrir des le clic, sans qu'il
    faille bouger la souris. Elle est fermee au depart puisque ce fichier n'a aucun resultat de
    profilage.
    """
    editor, manager = nouvel_editeur()
    # La marge est toujours presente et cliquable, meme sans resultat de profilage.
    assert manager.panel.sizeHint().width() >= manager.panel.TAILLE_ICONE

    peintures = []
    manager.sig_repaint_targets.connect(lambda: peintures.append(1))
    clic_dans_la_marge(editor, manager, DEF_LENTE)

    assert manager.get_targets() == [DEF_LENTE]
    assert peintures, \
        "aucun rafraichissement demande : il faudrait bouger la souris pour voir le marqueur"


def test_retirer_un_marqueur_demande_aussi_un_rafraichissement():
    editor, manager = nouvel_editeur()
    clic_dans_la_marge(editor, manager, DEF_LENTE)
    peintures = []
    manager.sig_repaint_targets.connect(lambda: peintures.append(1))
    clic_dans_la_marge(editor, manager, DEF_LENTE)
    assert manager.get_targets() == []
    assert peintures, "le retrait ne redessine pas : l'icone resterait affichee"


def test_clear_targets_persiste_bien_le_retrait_dans_la_configuration():
    """clear_targets() pre-assigne self.targets = [] AVANT d'appeler targets_changed(), qui ne
    sauvegarde QUE si self.targets a change par rapport a get_targets() fraichement recalcule.
    Les deux valent [] au moment de la comparaison : l'ecart est nul, save_targets() n'est donc
    JAMAIS appele - la CONF garde l'ancienne liste de marqueurs alors que l'editeur (et l'icone)
    les a bien retires. Trouve en validant TODO - Spyder - plugin Line profiler.txt (02/08/2026) :
    un F10 lance apres un "tout effacer" delegue quand meme au line profiler, sur des marqueurs
    que plus personne ne voit."""
    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "m.py")
        Path(chemin).write_text(SOURCE)

        editor, manager = nouvel_editeur(filename=chemin)
        clic_dans_la_marge(editor, manager, 3)
        assert pt.load_targets(chemin) == [DEF_LENTE]

        manager.clear_targets()
        assert manager.get_targets() == [], "l'etat en memoire n'est pas vide"
        assert pt.load_targets(chemin) == [], \
            "clear_targets() n'a pas persiste le retrait dans la configuration"


# =============================================================================
# 2. Les lignes qui n'acceptent pas de marqueur
# =============================================================================

def test_un_clic_hors_de_toute_fonction_ne_pose_rien():
    editor, manager = nouvel_editeur()
    for lineno, quoi in [(6, "ligne vide entre deux fonctions"),
                         (12, "ligne 'class Calc:'")]:
        clic_dans_la_marge(editor, manager, lineno)
        assert manager.get_targets() == [], f"{quoi} (ligne {lineno})"


def test_un_clic_sur_un_fichier_invalide_ne_pose_rien():
    # Frappe en cours : le fichier ne se parse pas, aucune fonction n'est identifiable.
    editor, manager = nouvel_editeur(source="def f(:\n    pass\n")
    clic_dans_la_marge(editor, manager, 2)
    assert manager.get_targets() == [], manager.get_targets()


# =============================================================================
# 3. L'apercu au survol
# =============================================================================

def test_l_apercu_se_place_sur_le_def_meme_en_survolant_le_corps():
    editor, manager = nouvel_editeur()
    for lineno in (1, 2, 3, 4, 5):
        assert survol_de_la_marge(editor, manager, lineno) == DEF_LENTE, lineno
    for lineno in (13, 14, 15, 16, 17):
        assert survol_de_la_marge(editor, manager, lineno) == DEF_METHODE, lineno


def test_aucun_apercu_sur_une_ligne_qui_n_accepte_rien():
    editor, manager = nouvel_editeur()
    for lineno in (6, 7, 10, 11, 12):
        assert survol_de_la_marge(editor, manager, lineno) is None, lineno


# =============================================================================
# 4. Cohabitation avec le reste de l'editeur
# =============================================================================

def test_un_point_d_arret_pose_sur_la_meme_ligne_survit():
    editor, manager = nouvel_editeur()
    # Le debogueur a deja pose son propre BlockUserData sur la ligne du def.
    block = editor.document().findBlockByNumber(DEF_LENTE - 1)
    data = BlockUserData(editor)
    data.breakpoint = True
    data.breakpoint_condition = "n > 10"
    block.setUserData(data)

    clic_dans_la_marge(editor, manager, 3)

    data = editor.document().findBlockByNumber(DEF_LENTE - 1).userData()
    assert data.profile_target is True
    assert data.breakpoint is True, "le point d'arret a ete efface"
    assert data.breakpoint_condition == "n > 10", "la condition a ete effacee"


def test_les_marqueurs_suivent_l_insertion_de_lignes():
    """Propriete qui vient du portage de l'etat sur les blocs Qt.

    Elle casserait si on stockait des numeros de ligne nus : editer au-dessus d'une fonction
    marquee deplacerait le marqueur sur une autre ligne.

    ⚠ Le marqueur vise ici est celui de Calc.methode, PAS celui de `lente`. Inserer a la
    position 0 alors qu'un marqueur est sur la ligne 1 insere DANS ce bloc-la : Qt laisse alors
    le userData sur la premiere moitie du bloc scinde, donc le marqueur reste ligne 1. Ce n'est
    pas un defaut de notre code - les points d'arret de Spyder se comportent exactement pareil,
    verifie en direct - mais un cas limite qui ne prouverait rien ici.
    """
    editor, manager = nouvel_editeur()
    clic_dans_la_marge(editor, manager, 15)
    assert manager.get_targets() == [DEF_METHODE]

    # Deux lignes inserees tout en haut : le marqueur doit descendre d'autant.
    curseur = editor.textCursor()
    curseur.setPosition(0)
    curseur.insertText("import os\nimport sys\n")
    QApplication.processEvents()

    assert manager.get_targets() == [DEF_METHODE + 2], manager.get_targets()


# =============================================================================
# 5. Configuration du lanceur (lp_launcher.py), de bout en bout sur un vrai paquet
# =============================================================================

def test_les_marqueurs_partent_tels_quels_dans_la_config_du_lanceur():
    # Depuis la refonte du 24/07/2026, plus de traduction en noms pointes : le lanceur recoit
    # {fichier: [lignes]} et resout lui-meme chaque ligne vers son `def` sur la source qu'il
    # compile. La config ne porte donc QUE des chemins et des lignes.
    with tempfile.TemporaryDirectory() as dossier:
        paquet = Path(dossier) / "pkg"
        paquet.mkdir()
        (paquet / "__init__.py").write_text("")
        fichier = paquet / "maths.py"
        fichier.write_text(SOURCE)

        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION,
                    {str(fichier): [DEF_LENTE, DEF_METHODE]})

        config = pt.config_lanceur(str(fichier))
        assert config['all_user'] is False
        assert config['racine'] is None
        assert config['targets'] == {str(fichier): sorted([DEF_LENTE, DEF_METHODE])}


def test_config_lanceur_ignore_un_fichier_marque_disparu():
    # Jamais bloquant : un marqueur dont le fichier a disparu (volume non monte...) est ecarte
    # de la config du run, mais reste dans la CONF (regle "ne rien perdre au chargement").
    absent = os.path.normcase("/DATA/dossier_inexistant_smartos_test/x.py")
    assert not os.path.isfile(absent)
    with tempfile.TemporaryDirectory() as dossier:
        fichier = Path(dossier) / "seul.py"
        fichier.write_text(SOURCE)
        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION, {
            os.path.normcase(str(fichier)): [DEF_LENTE], absent: [1]})

        config = pt.config_lanceur(str(fichier))
        assert list(config['targets']) == [os.path.normcase(str(fichier))]
        assert absent in pt.CONF.get(pt.CONF_SECTION, pt.CONF_OPTION, {})


def test_config_lanceur_mode_tout_le_code_utilisateur():
    # Option cochee : pas de cibles, un drapeau et la racine du PAQUET du script (le lanceur
    # decore paresseusement tout module importe sous cette racine).
    with tempfile.TemporaryDirectory() as dossier:
        paquet = Path(dossier) / "pkg"
        paquet.mkdir()
        (paquet / "__init__.py").write_text("")
        fichier = paquet / "maths.py"
        fichier.write_text(SOURCE)
        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION,
                    {str(fichier): [DEF_LENTE]})
        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION_ALL, True)

        config = pt.config_lanceur(str(fichier))
        assert config['all_user'] is True
        # La racine est le dossier PARENT de pkg/ : c'est le projet du script, pas le paquet.
        assert os.path.normcase(config['racine']) == os.path.normcase(dossier)
        assert config['targets'] == {}


def test_un_orphelin_temporaire_est_purge_au_chargement():
    # tempfile.TemporaryDirectory() est sous /tmp : un fichier marque puis disparu de la ne
    # reviendra jamais -> son marqueur est purge, et ne bloque donc plus le profilage des autres.
    with tempfile.TemporaryDirectory() as dossier:
        fichier = Path(dossier) / "seul.py"
        fichier.write_text(SOURCE)
        disparu = str(Path(dossier) / "disparu.py")      # sous /tmp, jamais cree
        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION, {
            str(fichier): [DEF_LENTE], disparu: [1]})

        config = pt.config_lanceur(str(fichier))
        assert list(config['targets']) == [str(fichier)], config['targets']
        # l'orphelin temporaire a bien ete retire de la configuration (pas juste ignore)
        restants = pt.CONF.get(pt.CONF_SECTION, pt.CONF_OPTION, {})
        assert disparu not in restants, restants


def test_un_orphelin_hors_temp_est_conserve():
    # Regle deliberee "ne rien perdre au chargement" : un fichier de PROJET momentanement absent
    # (volume non monte) garde son marqueur. On ne purge que les orphelins des dossiers temporaires.
    absent = os.path.normcase("/DATA/dossier_inexistant_smartos_test/x.py")
    assert not os.path.isfile(absent)
    pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION, {absent: [1]})
    pt._load_all_targets()   # declenche l'eventuelle purge
    assert absent in pt.CONF.get(pt.CONF_SECTION, pt.CONF_OPTION, {})


# =============================================================================
# 6. Persistance et migration des anciens marqueurs
# =============================================================================

def test_les_marqueurs_sont_relus_a_la_reouverture():
    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "m.py")
        Path(chemin).write_text(SOURCE)

        _editor, manager = nouvel_editeur(filename=chemin)
        clic_dans_la_marge(_editor, manager, 3)
        assert manager.get_targets() == [DEF_LENTE]

        # Nouvel editeur sur le meme fichier : les marqueurs viennent de la configuration.
        _editor2, manager2 = nouvel_editeur(filename=chemin)
        assert manager2.get_targets() == [DEF_LENTE], manager2.get_targets()


def test_un_ancien_marqueur_au_milieu_du_corps_migre_sur_le_def():
    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "m.py")
        Path(chemin).write_text(SOURCE)
        # Marqueur tel qu'enregistre AVANT le recalage sur le `def` : ligne 16, "s += i".
        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION, {chemin: [16]})

        _editor, manager = nouvel_editeur(filename=chemin)
        assert manager.get_targets() == [DEF_METHODE], manager.get_targets()


def test_un_marqueur_qui_ne_designe_plus_rien_est_conserve_et_non_efface():
    """Regle inverse de celle du clic, et deliberee : au chargement on ne perd rien.

    Si le tampon n'est pas encore rempli, ou si le fichier est temporairement invalide, refuser
    les marqueurs les ferait disparaitre - et targets_changed() les effacerait aussitot de la
    configuration, donc definitivement.
    """
    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "m.py")
        Path(chemin).write_text(SOURCE)
        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION, {chemin: [7]})  # ligne vide

        _editor, manager = nouvel_editeur(filename=chemin)
        assert manager.get_targets() == [7], manager.get_targets()


# =============================================================================
# 7. "Un marqueur = un fichier ouvert" : purge a la fermeture d'onglet
# =============================================================================

def test_fermer_un_onglet_oublie_ses_marqueurs():
    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "prog.py")
        Path(chemin).write_text(SOURCE)
        editor, manager = nouvel_editeur(filename=chemin)
        clic_dans_la_marge(editor, manager, 3)   # marque lente
        assert pt.load_targets(chemin) == [DEF_LENTE]

        pt.detach_editor(editor)   # fermer l'onglet
        assert pt.load_targets(chemin) == [], "le marqueur n'a pas ete oublie a la fermeture"


def test_un_fichier_ouvert_dans_un_autre_split_garde_ses_marqueurs():
    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "prog.py")
        Path(chemin).write_text(SOURCE)
        ed1, m1 = nouvel_editeur(filename=chemin)
        ed2, _m2 = nouvel_editeur(filename=chemin)   # MEME fichier, 2e editeur (vue scindee)
        clic_dans_la_marge(ed1, m1, 3)
        assert pt.load_targets(chemin) == [DEF_LENTE]

        pt.detach_editor(ed1)   # fermer UN split : le fichier reste ouvert dans ed2
        assert pt.load_targets(chemin) == [DEF_LENTE], "marqueur perdu alors que le fichier reste ouvert"

        pt.detach_editor(ed2)   # fermer le dernier -> oubli
        assert pt.load_targets(chemin) == []


def test_l_arret_de_spyder_ne_purge_pas(monkeypatch=None):
    # Pendant l'arret (closingDown() vrai), fermer les onglets ne doit PAS purger.
    with tempfile.TemporaryDirectory() as dossier:
        chemin = str(Path(dossier) / "prog.py")
        Path(chemin).write_text(SOURCE)
        editor, manager = nouvel_editeur(filename=chemin)
        clic_dans_la_marge(editor, manager, 3)
        assert pt.load_targets(chemin) == [DEF_LENTE]

        ancien = pt._spyder_se_ferme
        pt._spyder_se_ferme = lambda: True   # simule l'arret de Spyder
        try:
            pt.detach_editor(editor)
        finally:
            pt._spyder_se_ferme = ancien
        assert pt.load_targets(chemin) == [DEF_LENTE], "l'arret de Spyder a purge (il ne devrait pas)"


def test_purge_a_l_ouverture_des_fichiers_non_reouverts():
    # A l'ouverture : purge STRICTE des marqueurs de TOUS les fichiers non ouverts (aucun garde-fou
    # d'existence, choix delibere de l'utilisateur). Seuls les fichiers OUVERTS sont conserves.
    with tempfile.TemporaryDirectory() as dossier:
        ouvert = str(Path(dossier) / "ouvert.py")
        ferme = str(Path(dossier) / "ferme.py")
        for chemin in (ouvert, ferme):
            Path(chemin).write_text(SOURCE)
        volume_absent = os.path.normcase("/DATA/volume_non_monte_smartos/x.py")  # parent inexistant

        pt.CONF.set(pt.CONF_SECTION, pt.CONF_OPTION, {
            os.path.normcase(ouvert): [DEF_LENTE],
            os.path.normcase(ferme): [DEF_LENTE],
            volume_absent: [DEF_LENTE]})
        # seul "ouvert" a un editeur
        nouvel_editeur(filename=ouvert)

        pt.purger_marqueurs_des_fichiers_fermes()
        restants = pt.CONF.get(pt.CONF_SECTION, pt.CONF_OPTION, {})
        assert os.path.normcase(ouvert) in restants, "fichier ouvert purge a tort"
        assert os.path.normcase(ferme) not in restants, "fichier ferme non purge"
        # Purge stricte : meme un fichier dont le volume est absent est purge s'il n'est pas ouvert.
        assert volume_absent not in restants, "purge stricte : le non-ouvert doit etre purge"


# =============================================================================

def main():
    # Isolation : aucun acces a la configuration reelle de Spyder.
    pt.CONF = ConfEnMemoire()

    tests = [(nom, objet) for nom, objet in sorted(globals().items())
             if nom.startswith("test_") and callable(objet)]
    echecs = []
    for nom, test in tests:
        pt.CONF = ConfEnMemoire()       # chaque test part d'une configuration vierge
        try:
            test()
        except Exception as erreur:
            echecs.append((nom, erreur))
            print(f"ECHEC  {nom}\n       {type(erreur).__name__}: {erreur}")
        else:
            print(f"ok     {nom}")

    print(f"\n{len(tests) - len(echecs)}/{len(tests)} tests passent")
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
