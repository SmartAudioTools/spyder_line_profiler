# -*- coding: utf-8 -*-
"""Resultats du profilage dans l'editeur : lecture du pickle, fonds colores, marge de droite.

CE QUE CE TEST COUVRE
La chaine complete, depuis un fichier de resultats ecrit comme kernprof les ecrit jusqu'aux
decorations posees sur une VRAIE CodeEditor de Spyder :
  - la lecture du pickle LineStats et le calcul des pourcentages, relatifs a la FONCTION ;
  - le fond des lignes, dont l'opacite suit le poids de la ligne, avec un plancher qui distingue
    "mesuree a 0 %" de "jamais executee" ;
  - la marge de droite : largeur nulle quand il n'y a rien, calculee sinon ;
  - la publication : ouvrir un editeur apres un profilage, ou profiler alors qu'il est deja
    ouvert, doivent donner le meme resultat ;
  - le nettoyage : effacer les resultats retire les decorations et replie la marge.

POURQUOI UN PICKLE FABRIQUE PLUTOT QUE LE VRAI FICHIER
Le fichier ~/.config/spyder-py3/lineprofiler.results appartient a l'utilisateur et est ecrase a
chaque profilage : s'en servir rendrait le test dependant du dernier lancement, donc instable.
On ecrit donc un LineStats dans un dossier temporaire, avec la meme structure exactement -
c'est line_profiler lui-meme qui fournit la classe, pas une imitation.

Lancement :
    QT_QPA_PLATFORM=offscreen python tests/test_profile_results.py
"""

import os
import pickle
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtGui import QColor  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from line_profiler.line_profiler import LineStats  # noqa: E402
from spyder.plugins.editor.widgets.codeeditor import CodeEditor  # noqa: E402

from spyder_line_profiler.spyder import profile_results as pr  # noqa: E402

# Ces tests verifient la couche LINE PROFILER seule (echelle LINEAIRE d'origine, heat =
# secondes / ligne la plus chere). Depuis l'ajout de la coloration cProfile, load_results lit
# aussi un .prof (get_conf_path('lineprofiler_cprofile.prof')) et bascule alors en echelle LOG -
# ce qui casserait ces assertions si un vrai .prof traine dans la config de l'utilisateur. On
# force donc _cprofile_path vers un chemin inexistant : total_tt = 0 -> repli lineaire, comme
# avant. La coloration cProfile (echelle log, lignes de `def`) est testee a part dans
# test_profile_cprofile.py.
pr._cprofile_path = lambda: '/nonexistent-smartos/pas-de-prof.prof'


SOURCE = '''def lente(n):
    total = 0
    for i in range(n):
        total += i * i
    return total
'''

# Temps en unites de 1e-9 s. La boucle (ligne 3) et le corps (ligne 4) se partagent presque tout
# le temps ; les lignes 2 et 5 sont mesurees mais negligeables. La ligne 1 (le `def`) n'est
# jamais "executee" au sens du profileur : elle ne doit donc PAS etre coloree.
TIMINGS = [(2, 1, 400), (3, 300001, 40000000), (4, 300000, 60000000), (5, 1, 600)]


def ecrire_resultats(dossier, chemin_source, timings=TIMINGS, unit=1e-9):
    """Ecrit un fichier de resultats a l'identique de ce que produit kernprof -o."""
    stats = LineStats({(chemin_source, 1, 'lente'): timings}, unit)
    chemin = Path(dossier) / "lineprofiler.results"
    with open(chemin, 'wb') as flux:
        pickle.dump(stats, flux)
    return str(chemin)


def luminance(couleur):
    """Luminance percue (Rec. 601), la seule mesure de "clarte" fiable sur une echelle coloree."""
    return (0.299 * couleur.red() + 0.587 * couleur.green() + 0.114 * couleur.blue())


def nouvel_editeur(chemin):
    editor = CodeEditor(None)
    editor.setup_editor(language='Python', filename=chemin)
    editor.set_text(SOURCE)
    editor.resize(700, 500)
    editor.show()
    QApplication.processEvents()
    manager = pr.ProfileResultsManager(editor)
    QApplication.processEvents()
    return editor, manager


# =============================================================================
# 1. Lecture du fichier de resultats
# =============================================================================

def test_le_pickle_de_kernprof_est_relu_ligne_par_ligne():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        resultats = pr.publish(ecrire_resultats(dossier, source))

        lignes = pr.results_for(source)
        assert sorted(lignes) == [2, 3, 4, 5], sorted(lignes)
        assert lignes[3].hits == 300001
        assert abs(lignes[3].seconds - 0.040) < 1e-9, lignes[3].seconds
        assert lignes[3].function == 'lente'
        assert source in str(list(resultats))


def test_le_pourcentage_est_relatif_a_la_fonction():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))

        lignes = pr.results_for(source)
        # 40 ms et 60 ms sur 100.001 ms au total.
        assert abs(lignes[3].percent - 0.40) < 0.001, lignes[3].percent
        assert abs(lignes[4].percent - 0.60) < 0.001, lignes[4].percent
        assert abs(sum(l.percent for l in lignes.values()) - 1.0) < 1e-6


def test_le_temps_par_passage_est_calcule():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        ligne = pr.results_for(source)[4]
        assert abs(ligne.per_hit - 0.060 / 300000) < 1e-12


def test_un_fichier_de_resultats_absent_ou_illisible_ne_casse_rien():
    assert pr.load_results("/chemin/qui/n/existe/pas.results") == {}
    with tempfile.TemporaryDirectory() as dossier:
        bidon = Path(dossier) / "pas_un_pickle"
        bidon.write_text("ceci n'est pas un pickle")
        assert pr.load_results(str(bidon)) == {}


def test_les_durees_sont_lisibles_et_dans_la_bonne_unite():
    assert pr.format_duree(2.5) == '2.50 s'
    assert pr.format_duree(0.0066) == '6.6 ms'
    assert pr.format_duree(1.37e-7) == '137 ns'
    assert pr.format_duree(None) == ''


# =============================================================================
# 2. Fonds colores dans l'editeur
# =============================================================================

def test_seules_les_lignes_mesurees_recoivent_un_fond():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))

        editor, _manager = nouvel_editeur(source)
        decorations = editor.decorations.get(pr.DECORATION_KEY, [])
        assert len(decorations) == 4, len(decorations)
        # La ligne 1 (le `def`) n'est pas mesuree : aucune decoration ne doit la couvrir.
        lignes_decorees = {d.cursor.blockNumber() + 1 for d in decorations}
        assert lignes_decorees == {2, 3, 4, 5}, lignes_decorees


def test_l_echelle_va_du_fond_au_rouge_en_passant_par_violet():
    """Le degrade des cameras thermiques, mais ARRETE AU ROUGE (demande de l'utilisateur).

    On ne monte ni a l'orange ni au jaune : ces teintes claires de l'ironbow, en aplat derriere du
    code, ecrasent la coloration syntaxique meme a opacite moderee.
    """
    froid = pr.couleur_thermique(0.0)
    violet = pr.couleur_thermique(0.5)
    chaud = pr.couleur_thermique(1.0)

    assert (froid.red(), froid.green(), froid.blue()) == (0, 0, 0)
    assert violet.blue() > violet.green(), "le violet a perdu son bleu"
    # Sommet = ROUGE (220, 20, 20) : rouge dominant, vert et bleu faibles ; pas d'orange ni de jaune.
    assert chaud.red() > 200 and chaud.green() < 60 and chaud.blue() < 60, "le sommet n'est pas rouge"

    # Strictement croissant en LUMINANCE PERCUE : c'est ce qui rend l'echelle lisible d'un coup
    # d'oeil, et ce qui la garde interpretable imprimee en niveaux de gris.
    #
    # ⚠ Et non QColor.lightness(), qui est la clarte au sens HSL - (max + min) / 2 sur les
    # canaux. Elle n'est PAS monotone ici : le violet (105, 0, 160) y sort a 80 et le rouge
    # (220, 20, 20) a 68, alors que le rouge est visiblement plus clair. Le premier jet de ce
    # test s'en servait et echouait pour cette raison ; c'etait le test qui avait tort, pas
    # l'echelle.
    luminances = [luminance(pr.couleur_thermique(p / 20)) for p in range(21)]
    assert luminances == sorted(luminances), luminances


def test_l_echelle_est_continue_et_bornee():
    # Pas de saut brutal entre deux paliers : au pire quelques points d'ecart pour 1 % de plus.
    precedent = pr.couleur_thermique(0.0)
    for pas in range(1, 101):
        courant = pr.couleur_thermique(pas / 100)
        ecart = max(abs(courant.red() - precedent.red()),
                    abs(courant.green() - precedent.green()),
                    abs(courant.blue() - precedent.blue()))
        assert ecart < 30, f"saut de {ecart} a {pas} %"
        precedent = courant
    # Hors bornes : on ne leve pas, on serre.
    assert pr.couleur_thermique(-5).name() == pr.couleur_thermique(0.0).name()
    assert pr.couleur_thermique(99).name() == pr.couleur_thermique(1.0).name()


def test_le_degrade_part_de_la_couleur_de_fond_neutre():
    """Demande de l'utilisateur : partir du fond de Spyder plutot que du noir.

    Une ligne mesuree mais negligeable se fond alors dans l'editeur au lieu de l'assombrir -
    et cela suit le theme, clair comme sombre, au lieu d'un noir fige.
    """
    fond = QColor(60, 70, 80)
    assert pr.couleur_thermique(0.0, fond).name() == fond.name()
    # Le haut de l'echelle, lui, ne depend pas du theme.
    assert pr.couleur_thermique(1.0, fond).name() == pr.couleur_thermique(1.0).name()
    # Et le palier suivant a bien quitte le fond.
    assert pr.couleur_thermique(0.25, fond).name() != fond.name()


def test_le_fond_prend_la_couleur_thermique_de_la_ligne():
    froid = pr.ProfileResultsManager.couleur_de_fond(0.0)
    chaud = pr.ProfileResultsManager.couleur_de_fond(1.0)
    # La TEINTE porte l'information, contrairement aux versions precedentes.
    assert (froid.red(), froid.green(), froid.blue()) != \
           (chaud.red(), chaud.green(), chaud.blue())
    assert chaud.red() > 200 and chaud.green() < 60 and chaud.blue() < 60, \
        "le haut de l'echelle n'est pas le rouge attendu"
    # Plancher d'opacite : le froid est du NOIR, invisible s'il est trop transparent sur un
    # fond d'editeur deja sombre - or il doit se distinguer d'une ligne jamais executee.
    assert froid.alpha() >= 90
    # Plafond sous 255 : le code est colore par la coloration syntaxique, qu'un fond opaque
    # rendrait illisible - d'autant que le haut de l'echelle est un jaune tres clair.
    assert chaud.alpha() < 255


def test_la_marge_porte_exactement_la_couleur_de_sa_ligne():
    """Remarque de l'utilisateur : "pourquoi la couleur du texte dans la marge n'est pas la
    meme que le fond de la ligne coloree ?".

    Elle ne l'etait pas parce que le CHIFFRE portait la couleur thermique, et qu'il fallait le
    remonter au-dessus du noir pour qu'il reste visible. C'est desormais le FOND de la case de
    marge qui porte la couleur - la meme exactement - et le chiffre est en couleur de texte
    normale, donc lisible a toute temperature.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)

        fond_editeur = manager.couleur_de_fond_neutre()
        for ligne, mesure in manager.lignes.items():
            attendue = pr.ProfileResultsManager.couleur_de_fond(mesure.heat, fond_editeur)
            # La marge peint ce meme appel : si l'un des deux derivait, ceci le dirait.
            assert attendue.alpha() > 0 and attendue.name() == pr.couleur_thermique(
                mesure.heat, fond_editeur).name(), ligne
        assert editor is not None


def test_les_decorations_du_profilage_precedent_sont_remplacees_pas_empilees():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, _manager = nouvel_editeur(source)

        # Deuxieme profilage, deux lignes mesurees seulement.
        pr.publish(ecrire_resultats(dossier, source, timings=[(3, 5, 100), (4, 5, 200)]))
        QApplication.processEvents()

        decorations = editor.decorations.get(pr.DECORATION_KEY, [])
        assert len(decorations) == 2, f"{len(decorations)} decorations : elles s'empilent"


def test_effacer_les_resultats_retire_les_fonds():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)
        assert editor.decorations.get(pr.DECORATION_KEY)

        pr.clear()
        QApplication.processEvents()
        assert not editor.decorations.get(pr.DECORATION_KEY, [])
        # La marge se retrecit a sa largeur minimale, mais reste cliquable.
        assert manager.panel.sizeHint().width() == \
            manager.panel.TAILLE_ICONE + 2 * manager.panel.PADDING


def test_une_mesure_au_dela_de_la_fin_du_fichier_est_ignoree():
    """Le fichier a ete raccourci depuis le profilage : ne pas peindre n'importe ou."""
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source,
                                    timings=[(3, 1, 100), (900, 1, 100)]))
        editor, _manager = nouvel_editeur(source)
        decorations = editor.decorations.get(pr.DECORATION_KEY, [])
        assert len(decorations) == 1, len(decorations)


def test_la_chaleur_est_normalisee_sur_TOUT_le_profilage():
    """Une seule echelle pour toutes les fonctions selectionnees, comme une camera thermique.

    Demande explicite de l'utilisateur : "la couleur doit etre relative au temps total, donc
    normalise par rapport au max de toutes les lignes de toutes les fonctions selectionnees".
    Une premiere version normalisait PAR FONCTION : une fonction bon marche affichait alors un
    faux point chaud sur sa ligne la moins insignifiante.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)

        # Deux fonctions dans le meme fichier de resultats : "chere" pese 100 fois "bon_marche".
        stats = LineStats({
            (source, 1, 'chere'): [(2, 1, 100000000), (3, 1, 50000000)],
            (source, 8, 'bon_marche'): [(9, 1, 1000000), (10, 1, 500000)],
        }, 1e-9)
        chemin = Path(dossier) / "lineprofiler.results"
        with open(chemin, 'wb') as flux:
            pickle.dump(stats, flux)
        pr.publish(str(chemin))
        lignes = pr.results_for(source)

        # Le sommet de l'echelle est la ligne la plus chere DU PROFILAGE.
        assert lignes[2].heat == 1.0
        assert abs(lignes[3].heat - 0.5) < 0.01
        # La fonction bon marche reste froide de bout en bout, malgre ses 100 % internes...
        assert lignes[9].heat < 0.02, lignes[9].heat
        # ... alors que sa part du temps de SA fonction, elle, reste bien de deux tiers.
        assert abs(lignes[9].percent - 2 / 3) < 0.01

        assert pr.ProfileResultsManager.couleur_de_fond(lignes[2].heat).name() == \
            pr.couleur_thermique(1.0).name(), "le haut de l'echelle n'est pas atteint"
        froide = pr.ProfileResultsManager.couleur_de_fond(lignes[9].heat)
        assert luminance(froide) < 40, "la fonction bon marche n'est pas restee sombre"


def test_la_ligne_la_plus_chere_atteint_le_haut_de_l_echelle():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        lignes = pr.results_for(source)
        # 60 ms sur la ligne 4, contre 40 sur la 3 : sommet de l'echelle.
        assert lignes[4].heat == 1.0
        assert abs(lignes[3].heat - 40 / 60) < 0.01
        assert lignes[2].heat < 0.001, "une ligne negligeable devrait rester froide"


def test_une_fonction_a_une_seule_ligne_mesuree_ne_divise_pas_par_zero():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source, timings=[(3, 1, 0)]))
        lignes = pr.results_for(source)
        assert lignes[3].heat == 0.0
        assert lignes[3].percent == 0.0


def test_coller_la_marge_ne_marque_pas_le_fichier_comme_modifie():
    """Un reglage d'affichage ne doit jamais faire croire que le fichier a change.

    Annuler la marge droite du document passe par la pile d'annulation du QTextDocument et leve
    son drapeau "modifie". Les onglets affichaient donc "maths.py*" sans qu'aucun caractere ait
    bouge, et fermer Spyder proposait d'enregistrer un fichier intact - avec le risque d'ecraser
    le vrai contenu par un tampon qu'on croit a jour. Constate a l'ecran.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        editor, _manager = nouvel_editeur(source)
        assert not editor.document().isModified(), \
            "le fichier est marque modifie alors que rien n'a change"


def test_le_code_ne_colle_pas_aux_numeros_de_ligne():
    """La marge GAUCHE du code doit exister, et c'est un panneau qui la porte.

    ⚠ CE QUE CE TEST NE FAIT PLUS : relire la valeur qu'on vient d'ecrire. Sa premiere version
    verifiait la marge gauche du CADRE RACINE du document ; elle passait alors que le code etait
    colle aux numeros de ligne, QPlainTextDocumentLayout ignorant les marges de cadre. On mesure
    donc un EFFET : la marge gauche du viewport, que Spyder calcule depuis la largeur des panneaux.
    Replier l'espaceur doit la faire diminuer d'autant.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)

        pr.clear()
        editor, manager = nouvel_editeur(source)
        assert editor.document().documentMargin() == 0, \
            "documentMargin non nul : la marge doit venir des panneaux, pas du document"

        avec = editor.viewportMargins().left()
        manager.espaceur_gauche.regler_largeur(0)
        editor.panels.refresh()
        sans = editor.viewportMargins().left()
        assert avec - sans == manager.ESPACE_CODE, (
            f"l'espaceur gauche ne cree pas de marge : {avec} avec, {sans} sans, "
            f"attendu un ecart de {manager.ESPACE_CODE}")


def test_la_colonne_des_temps_est_collee_au_code():
    """AUCUNE marge entre le code et la colonne des temps, avec ou sans temps affiches.

    Arbitrage de l'utilisateur du 26/07/2026, qui revient sur un choix plus ancien : un espaceur de
    4 px avait ete pose a droite pour separer le code de la colonne, "l'approche espacement plutot
    que trait colle au bord". Il l'a fait retirer - "sur un fichier avec temps, et je n'en veux plus".
    Ce test empeche de le remettre par inadvertance en croyant bien faire.

    On verifie que le panneau de droite le plus proche du code EST la colonne des temps. C'est la
    definition de "colle" : les panneaux de droite se placent du bord vers l'interieur par
    order_in_zone decroissant, donc le rang le plus BAS touche le code.
    """
    from spyder.plugins.editor.api.panel import Panel

    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)

        pr.clear()
        editor, manager = nouvel_editeur(source)
        pr.publish(ecrire_resultats(dossier, source))
        manager.refresh()

        droite = list(editor.panels.panels_for_zone(Panel.Position.RIGHT))
        assert droite, "aucun panneau a droite"
        colle = min(droite, key=lambda p: p.order_in_zone)
        assert colle is manager.panel, (
            f"un {type(colle).__name__} (rang {colle.order_in_zone}) s'est glisse entre le code et "
            f"la colonne des temps (rang {manager.panel.order_in_zone})")


def test_la_colonne_des_temps_revient_a_son_minimum_cliquable():
    """La largeur de la colonne suit les temps, et retombe a son MINIMUM CLIQUABLE, pas a zero.

    Premiere version de ce test : elle attendait zero, et echouait. C'etait la bonne surprise -
    replier la colonne a zero condamnerait la fonction au demarrage, faute de zone ou poser le
    premier marqueur (exigence de l'utilisateur, cf.
    test_la_marge_reste_cliquable_sur_un_fichier_jamais_profile). L'ecrire ici evite de "corriger"
    un jour ce minimum en croyant reparer une regression.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)

        pr.clear()
        editor, manager = nouvel_editeur(source)
        minimum = manager.panel.sizeHint().width()
        assert minimum >= manager.panel.TAILLE_ICONE, \
            "la colonne n'est plus cliquable alors qu'aucun temps n'est affiche"

        pr.publish(ecrire_resultats(dossier, source))
        manager.refresh()
        assert manager.panel.sizeHint().width() > minimum, \
            "la colonne ne s'elargit pas alors qu'il y a des temps"

        pr.clear()
        manager.refresh()
        assert manager.panel.sizeHint().width() == minimum, \
            "la colonne ne revient pas a son minimum quand les temps disparaissent"


# =============================================================================
# 3. Marge de droite
# =============================================================================

def test_la_marge_reste_cliquable_sur_un_fichier_jamais_profile():
    """Sinon on ne pourrait jamais poser le PREMIER marqueur.

    Une premiere version repliait la marge a zero quand il n'y avait rien a montrer, "pour ne
    pas manger de place". L'utilisateur a releve que cela condamnait la fonction au demarrage :
    pas de zone cliquable, donc aucun moyen d'activer une horloge quand il n'y en a pas encore.
    """
    pr.clear()
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "jamais_profile.py")
        Path(source).write_text(SOURCE)
        _editor, manager = nouvel_editeur(source)
        assert manager.panel.sizeHint().width() >= manager.panel.TAILLE_ICONE, \
            "sans zone cliquable, impossible de poser le premier marqueur"


def test_la_marge_s_elargit_pour_accueillir_les_durees():
    """La marge est toujours la ; c'est sa LARGEUR qui s'adapte a ce qu'elle doit afficher."""
    pr.clear()
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)

        _editor, vide = nouvel_editeur(source)
        largeur_sans_temps = vide.panel.sizeHint().width()

        pr.publish(ecrire_resultats(dossier, source))
        _editor2, manager = nouvel_editeur(source)
        assert manager.panel.sizeHint().width() > largeur_sans_temps


# =============================================================================
# 4. Publication vers les editeurs deja ouverts
# =============================================================================

def test_un_editeur_deja_ouvert_se_met_a_jour_au_profilage_suivant():
    pr.clear()
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)

        # Editeur ouvert AVANT tout profilage : la marge est la, mais vide.
        editor, manager = nouvel_editeur(source)
        largeur_avant = manager.panel.sizeHint().width()
        assert manager.lignes == {}

        pr.publish(ecrire_resultats(dossier, source))
        QApplication.processEvents()

        assert manager.panel.sizeHint().width() > largeur_avant
        assert len(editor.decorations.get(pr.DECORATION_KEY, [])) == 4


def test_un_editeur_detache_ne_reagit_plus():
    """Le bus est un objet de MODULE : sans detachement il survivrait a l'editeur ferme."""
    pr.clear()
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        _editor, manager = nouvel_editeur(source)
        manager.detach()

        pr.publish(ecrire_resultats(dossier, source))
        QApplication.processEvents()
        assert manager.lignes == {}, "le manager detache a quand meme ete rafraichi"


def test_les_resultats_d_un_autre_fichier_ne_debordent_pas():
    with tempfile.TemporaryDirectory() as dossier:
        mesure = str(Path(dossier) / "mesure.py")
        autre = str(Path(dossier) / "autre.py")
        Path(mesure).write_text(SOURCE)
        Path(autre).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, mesure))

        editor, manager = nouvel_editeur(autre)
        assert manager.lignes == {}
        assert not editor.decorations.get(pr.DECORATION_KEY, [])


# =============================================================================
# 5. Chargement paresseux du fichier de resultats au demarrage de Spyder
# =============================================================================

def test_les_resultats_du_disque_sont_relus_au_premier_editeur_ouvert():
    """Rouvrir Spyder doit remontrer le dernier profilage, sans avoir a le relancer."""
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        chemin_resultats = ecrire_resultats(dossier, source)

        # Etat d'un Spyder qui vient de demarrer : rien en memoire, un fichier sur le disque.
        pr._RESULTS = {}
        pr._CHARGE = False
        pr.default_results_path = lambda: chemin_resultats

        _editor, manager = nouvel_editeur(source)
        assert len(manager.lignes) == 4, manager.lignes
        assert manager.panel.sizeHint().width() > 0


def test_l_effacement_n_est_pas_annule_par_le_rechargement():
    """Piege : clear() vide la memoire, mais le fichier du disque, lui, existe toujours."""
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        chemin_resultats = ecrire_resultats(dossier, source)
        pr.default_results_path = lambda: chemin_resultats
        pr._RESULTS = {}
        pr._CHARGE = False

        pr.publish(chemin_resultats)
        pr.clear()

        _editor, manager = nouvel_editeur(source)
        assert manager.lignes == {}, "l'effacement a ete annule par un rechargement du disque"


def test_la_hauteur_des_fonds_suit_la_geometrie_du_bloc_pas_la_police():
    """Regression : le retard de hauteur des fonds au changement de taille des caracteres.

    La hauteur d'une case doit venir de la MEME source que sa position (la geometrie du bloc),
    et non d'un QFontMetrics global recalcule a un autre moment - sinon les deux se
    desynchronisent le temps d'une image quand l'editeur relayoute a une nouvelle taille.

    On verifie que la hauteur rendue par le panneau EGALE la hauteur reelle du bloc dans
    l'editeur, a deux tailles de police differentes.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)
        panel = manager.panel

        for taille in (9, 18):
            police = editor.font()
            police.setPointSize(taille)
            editor.setFont(police)
            QApplication.processEvents()

            bloc = editor.document().findBlockByNumber(2)  # une ligne mesuree
            attendue = round(editor.blockBoundingRect(bloc).height())
            assert panel._hauteur_ligne(bloc) == attendue, (taille, attendue)
            # Et cette hauteur suit bien la taille : sinon le test ne prouverait rien.
        assert panel._hauteur_ligne(
            editor.document().findBlockByNumber(2)) > 0


def test_la_largeur_suit_le_zoom():
    """La colonne s'elargit quand on grossit les caracteres, sinon les durees debordent.

    rafraichir() n'etait appele qu'au changement de resultats : en zoomant, la largeur restait
    figee et le texte, devenu plus grand, ne tenait plus (remarque de l'utilisateur). Le panneau
    est desormais branche sur editor.sig_font_changed.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)

        police = editor.font(); police.setPointSize(9); editor.set_font(police)
        QApplication.processEvents()
        petite = manager.panel._largeur

        police = editor.font(); police.setPointSize(20); editor.set_font(police)
        QApplication.processEvents()
        grande = manager.panel._largeur

        assert grande > petite, (petite, grande)


def test_la_ligne_courante_est_repercutee_sur_la_colonne():
    """Le surlignage gris de la ligne courante doit se prolonger dans la colonne des timings.

    On ne peut pas asserter des pixels peints ici, mais on verifie le contrat observable : le
    panneau se REPEINT quand le curseur bouge (sinon le gris ne suivrait pas la ligne courante),
    et il connait la couleur de surlignage de l'editeur.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)

        repeints = []
        manager.panel.update = lambda *a: repeints.append(1)
        # Deplacer le curseur doit declencher un repeint de la colonne.
        curseur = editor.textCursor()
        curseur.setPosition(0)
        curseur.movePosition(curseur.MoveOperation.Down)
        editor.setTextCursor(curseur)
        QApplication.processEvents()
        assert repeints, "la colonne ne se repeint pas au deplacement du curseur"


def test_la_formule_a_cinq_segments_hits_op_perhit_op_total():
    """Format demande : hits × temps_par_passage = temps_total, en cinq segments.

    Cinq colonnes plutot que trois pour que × et = s'alignent verticalement dans leurs propres
    colonnes (choix valide avec l'utilisateur).
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)

        ligne = pr.results_for(source)[3]  # 300001 hits, total 40 ms
        segs = manager.panel._segments(ligne)
        assert segs == ("300001", "×", pr.format_duree(ligne.per_hit),
                        "=", pr.format_duree(ligne.seconds)), segs


def test_la_formule_du_def_a_le_meme_format_avec_ncalls_et_cumtime():
    """Ligne de `def` : meme format a cinq segments que les lignes, mais avec les grandeurs de la
    fonction entiere - n appels × cumtime/n = cumtime (prefixe d'un Σ au dessin).

    Demande de l'utilisateur (23/07/2026) : "le meme type d'affichage pour les fonctions que
    pour les lignes", soit "Σ n × temps_une_execution = temps_total unite".
    """
    import cProfile
    import runpy
    ancien = pr._cprofile_path
    try:
        with tempfile.TemporaryDirectory() as dossier:
            source = str(Path(dossier) / "prog.py")
            Path(source).write_text(
                "def calc(n):\n    s = 0\n    for i in range(n):\n        s += i\n    return s\n"
                "calc(2000)\n")
            profil = cProfile.Profile()
            profil.enable()
            try:
                runpy.run_path(source, run_name="__main__")
            except SystemExit:
                pass
            profil.disable()
            cible = str(Path(dossier) / "cprofile.prof")
            pr._cprofile_path = lambda: cible
            profil.dump_stats(str(Path(dossier) / "run.prof"))
            pr.publish_cprofile(open(str(Path(dossier) / "run.prof"), "rb").read())

            _editor, manager = nouvel_editeur(source)
            mesure = pr.results_for(source)[1]      # la ligne du `def calc` (ligne 1)
            assert mesure.kind == 'def', mesure.kind
            attendu = (str(mesure.ncalls), "×",
                       pr.format_duree(mesure.cumtime / mesure.ncalls),
                       "=", pr.format_duree(mesure.cumtime))
            assert manager.panel._segments(mesure) == attendu, manager.panel._segments(mesure)
    finally:
        pr._cprofile_path = ancien


def test_toutes_les_mesures_du_pickle_s_affichent_sans_filtre():
    """Depuis la refonte du lanceur (24/07/2026), le pickle ne contient QUE les fonctions
    effectivement visees (lp_launcher ne decore que les `def` marques, ou tout le code
    utilisateur si l'option est cochee) : l'affichage n'a plus a filtrer, tout ce qui est
    mesure est legitime. L'ancien filtre compensait le ciblage par chemin de kernprof, qui
    profilait TOUTES les fonctions du fichier lance (rev. 375, retiree)."""
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        stats = LineStats({(source, 1, 'premiere'): TIMINGS,
                           (source, 20, 'seconde'): [(21, 5, 500)]}, 1e-9)
        chemin = Path(dossier) / "r.results"
        with open(chemin, 'wb') as flux:
            pickle.dump(stats, flux)

        lignes = pr.load_results(str(chemin)).get(os.path.normcase(source), {})
        noms = {m.function for m in lignes.values() if m.kind == 'line'}
        assert noms == {'premiere', 'seconde'}, f"fonctions affichees : {noms}"


def test_le_trait_de_demarcation_fait_toujours_une_colonne_physique():
    """A echelle FRACTIONNAIRE, le filet doit etre visible et faire 1 px physique, TOUJOURS.

    Defaut signale par l'utilisateur le 23/07/2026 : "c'est quand je redimensionne l'editeur que
    le trait disparait/reapparait". Dessine a x=0, donc SUR le bord du panneau, le trait tombait
    entre deux colonnes physiques a l'echelle 1,3 : Qt le supprimait dans un cas sur deux.

    On mesure les PIXELS (regle du depot : un defaut visuel ne se prouve pas en lisant le code),
    en rendant l'editeur dans une QImage a devicePixelRatio 1,3 pour une serie de largeurs, donc
    de positions fractionnaires du bord du panneau. Exactement une colonne doit etre peinte a
    chaque fois : zero = trait invisible, deux = trait qui double d'epaisseur.
    """
    from qtpy.QtGui import QImage
    from qtpy.QtCore import QPoint

    dpr = 1.3
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)
        panel = manager.panel
        fond = manager.couleur_de_fond_neutre()
        fond_rvb = (fond.red(), fond.green(), fond.blue())

        for largeur in range(700, 716):
            editor.resize(largeur, 400)
            QApplication.processEvents()
            image = QImage(int(largeur * dpr), int(400 * dpr), QImage.Format_ARGB32)
            image.setDevicePixelRatio(dpr)
            image.fill(0)
            editor.render(image)

            bord = panel.mapTo(editor, QPoint(0, 0)).x() * dpr
            y = int(200 * dpr)
            peintes = []
            for colonne in range(int(bord) - 2, int(bord) + 4):
                if not (0 <= colonne < image.width()):
                    continue
                pixel = image.pixelColor(colonne, y)
                if max(abs(a - b) for a, b in
                       zip((pixel.red(), pixel.green(), pixel.blue()), fond_rvb)):
                    peintes.append(colonne)
            assert len(peintes) == 1, (
                f"editeur large de {largeur} px (bord du panneau a {bord:.1f} px physiques) : "
                f"{len(peintes)} colonne(s) peinte(s) au lieu d'une seule -> "
                + ("trait invisible" if not peintes else f"colonnes {peintes}"))


def _mesure_def(cumtime=9.5e-5, ncalls=3):
    """Une ligne de `def` mesuree par cProfile (ce que produit un run combine)."""
    return pr.LigneMesuree(
        hits=None, seconds=cumtime, per_hit=None, percent=None, function='rapide',
        heat=0.5, cumtime=cumtime, tottime=cumtime * 0.8, ncalls=ncalls, kind='def')


def test_le_sigma_des_defs_se_replie_aussi_en_mode_compact():
    """La formule Σ ne doit pas empecher la colonne de se replier.

    Regression du 23/07/2026 relevee par l'utilisateur ("ou est passee la barre de separation
    qu'on pouvait redimensionner ?") : en affichant TOUJOURS la formule entiere sur les lignes de
    `def`, _largeur_defs() maintenait la colonne a 145 px en mode compact au lieu de 72, pour
    160 px en deploye. Les deux etats devenaient quasi identiques et tirer la poignee ne
    repliait plus rien.

    Le `def` doit suivre le meme etat que les lignes du corps : formule deployee, temps cumule
    seul en compact.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)
        panel = manager.panel

        mesure = _mesure_def()
        manager.lignes[1] = mesure          # un `def` mesure EN PLUS des lignes du corps

        panel._compact = False
        panel.rafraichir()
        largeur_deployee = panel._largeur
        assert panel._texte_def(mesure) == ' '.join(panel._segments(mesure)), \
            "colonne deployee : le `def` doit montrer la formule entiere"

        panel._compact = True
        panel.rafraichir()
        assert panel._texte_def(mesure) == pr.format_duree(mesure.cumtime), \
            "colonne repliee : le `def` doit montrer le seul temps cumule"
        assert panel._largeur < largeur_deployee, (
            f"la colonne ne se replie plus : {panel._largeur} px en compact contre "
            f"{largeur_deployee} px deployee")


def test_la_colonne_s_ouvre_a_la_formule_complete_par_defaut():
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel
        assert panel._mode == 'complet'
        assert panel._largeur == panel._largeur_complete()


def test_l_etat_compact_reajuste_la_largeur_au_temps_total_seul():
    """Deux etats, chacun dimensionne EXACTEMENT a son contenu.

    Passer en compact reajuste la largeur au temps total seul, sans vide residuel (defaut
    signale par l'utilisateur : "quand on passe d'un affichage complet a juste temps+unite, la
    largeur n'est pas reajustee"). Repasser en complet restaure la largeur de la formule.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel

        largeur_complete = panel._largeur
        assert panel._mode == 'complet'

        panel._basculer_vers('total')
        assert panel._mode == 'total'
        assert panel._largeur == panel._plancher()
        assert panel._largeur < largeur_complete, "la largeur n'a pas ete reajustee"

        panel._basculer_vers('complet')
        assert panel._mode == 'complet'
        assert panel._largeur == panel._largeur_complete()


def test_la_poignee_de_redimensionnement_est_sur_le_bord_gauche():
    """Meme sans separation visible, les premiers pixels a gauche sont la poignee."""
    from qtpy.QtGui import QMouseEvent
    from qtpy.QtCore import QPointF, Qt as _Qt
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel

        def ev(x):
            return QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(x, 5),
                               _Qt.NoButton, _Qt.NoButton, _Qt.NoModifier)
        assert panel._sur_la_poignee(ev(0)) is True
        assert panel._sur_la_poignee(ev(panel.POIGNEE)) is True
        assert panel._sur_la_poignee(ev(panel.POIGNEE + 5)) is False

def test_eclaircir_rend_la_couleur_plus_claire():
    """La ligne courante eclaircit son fond : gris fonce -> gris clair, colore -> colore clair."""
    from qtpy.QtGui import QColor
    gris = QColor(43, 43, 67)
    clair = pr.eclaircir(gris)
    assert clair.red() > gris.red() and clair.green() > gris.green() and clair.blue() > gris.blue()
    # La teinte est preservee (on melange avec du blanc, on ne vire pas au gris) : pour une
    # couleur thermique, l'ordre des canaux reste le meme.
    orange = pr.couleur_thermique(1.0)
    orange_clair = pr.eclaircir(orange)
    assert orange_clair.red() >= orange_clair.green() >= orange_clair.blue()
    assert orange_clair.green() > orange.green(), "l'orange n'a pas ete eclairci"
    # force nulle = aucune modification ; force 1 = blanc.
    assert pr.eclaircir(gris, 0.0).name() == gris.name()
    assert pr.eclaircir(gris, 1.0).name() == QColor(255, 255, 255).name()


def test_la_ligne_courante_mesuree_est_eclaircie_dans_le_code_et_suit_le_curseur():
    """Dans la ZONE DE CODE, la ligne courante mesuree recoit une decoration eclaircie OPAQUE
    AU-DESSUS de la surbrillance de ligne courante de l'editeur, et elle suit le curseur.

    La surbrillance de Spyder a draw_order = DRAW_ORDERS['current_line'] (= 3), dans le meme
    gestionnaire de decorations, empile par draw_order croissant. Notre eclairci doit donc etre
    STRICTEMENT au-dessus, sans quoi le gris opaque de l'editeur le recouvre et la ligne profilee
    active reste grise (remarque de l'utilisateur).
    """
    from spyder.plugins.editor.api.decoration import DRAW_ORDERS
    seuil = DRAW_ORDERS['current_line']

    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)

        def deco_de(ligne):
            for d in editor.decorations.get(pr.DECORATION_KEY, []):
                if d.cursor.blockNumber() + 1 == ligne:
                    return d
            return None

        # Curseur sur la ligne 3 (mesuree).
        curseur = editor.textCursor()
        curseur.setPosition(editor.document().findBlockByNumber(2).position())
        editor.setTextCursor(curseur)
        manager._suivre_curseur()

        d3 = deco_de(3)
        assert d3 is not None and d3.draw_order > seuil, \
            "la ligne courante n'est pas au-dessus de la surbrillance de l'editeur"
        assert d3.format.background().color().alpha() == 255, "la ligne courante n'est pas opaque"
        # Une autre ligne mesuree (4) reste au fond thermique normal, semi-transparent et sous la
        # surbrillance.
        d4 = deco_de(4)
        assert d4.draw_order <= seuil and d4.format.background().color().alpha() < 255

        # Le curseur descend d'une ligne : l'eclairci suit.
        curseur.setPosition(editor.document().findBlockByNumber(3).position())
        editor.setTextCursor(curseur)
        manager._suivre_curseur()
        assert deco_de(4).draw_order > seuil, "l'eclairci n'a pas suivi le curseur"
        assert deco_de(3).draw_order <= seuil


def test_publish_cprofile_colore_les_def_sans_marqueur():
    """Sans AUCUN marqueur, un cProfile seul (F10) colore quand meme la ligne de chaque `def`.

    C'est la couche def-heatmap, alimentee par publish_cprofile depuis les octets .prof recus par le
    panneau Profileur (show_profile_buffer). Pas de temps par ligne (cProfile seul n'en a pas), juste
    un cumtime par fonction a sa ligne de `def`.
    """
    import cProfile
    import runpy
    ancien = pr._cprofile_path
    try:
        with tempfile.TemporaryDirectory() as dossier:
            chemin = str(Path(dossier) / "prog.py")
            Path(chemin).write_text(
                "def calc(n):\n    s = 0\n    for i in range(n):\n        s += i\n    return s\n"
                "calc(2000)\n")
            profil = cProfile.Profile()
            profil.enable()
            try:
                runpy.run_path(chemin, run_name="__main__")
            except SystemExit:
                pass
            profil.disable()
            proffile = str(Path(dossier) / "run.prof")
            profil.dump_stats(proffile)
            octets = open(proffile, "rb").read()

            # publish_cprofile ecrit le .prof la ou _cprofile_path pointe : dossier jetable.
            cible = str(Path(dossier) / "cprofile.prof")
            pr._cprofile_path = lambda: cible

            res = pr.publish_cprofile(octets)
            lignes = res.get(os.path.normcase(chemin), {})
            defs = {ligne: m for ligne, m in lignes.items()
                    if getattr(m, "kind", None) == "def"}
            assert defs, "publish_cprofile n'a colore aucune ligne de def"
            assert 1 in defs, f"la ligne du def calc (1) n'est pas coloree : {sorted(defs)}"
            assert defs[1].seconds > 0
            # cProfile seul : aucune ligne de CORPS (pas de temps par ligne).
            assert not any(getattr(m, "kind", None) == "line" for m in lignes.values())
    finally:
        pr._cprofile_path = ancien



# =============================================================================
# Troisieme etat de la colonne : repli sur les horloges (26/07/2026)
# =============================================================================

def test_le_repli_sur_les_horloges_retire_la_coloration_des_lignes():
    """LE test de cet etat, et il ECHOUE sans le correctif.

    Demande de l'utilisateur : « cacher resultats et colorations de lignes si on glisse la limite
    jusqu'aux horloges ». La coloration des lignes de l'editeur est la moitie visible de la demande,
    et c'est la seule qui ne se lise pas dans la largeur de la colonne : on la mesure donc par les
    decorations reellement posees sur l'editeur, pas par un drapeau interne.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)
        panel = manager.panel

        assert editor.decorations.get(pr.DECORATION_KEY), "rien n'etait colore au depart"

        panel._basculer_vers('horloges')
        assert panel._mode == 'horloges'
        assert not editor.decorations.get(pr.DECORATION_KEY, []), \
            "la coloration des lignes subsiste apres le repli sur les horloges"

        # Et elle revient quand on redeploie : le repli cache, il ne detruit pas.
        panel._basculer_vers('complet')
        assert editor.decorations.get(pr.DECORATION_KEY), \
            "la coloration n'est pas revenue au redeploiement"


def test_le_repli_sur_les_horloges_reduit_la_colonne_a_une_icone():
    """La largeur tombe a celle d'une icone : ni temps, ni formule cumulee des `def`.

    ⚠ C'est ce max() avec _largeur_defs() qui avait deja produit une regression sur l'etat compact
    le 23/07/2026 - la formule Σ maintenait la colonne large et la poignee paraissait morte. Le test
    verifie donc explicitement que le repli descend SOUS l'etat compact.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel

        panel._basculer_vers('total')
        largeur_compacte = panel._largeur

        panel._basculer_vers('horloges')
        attendue = panel.TAILLE_ICONE + 2 * panel.PADDING
        assert panel._largeur == attendue, f"{panel._largeur} au lieu de {attendue}"
        assert panel._largeur <= largeur_compacte, \
            "le repli sur les horloges n'est pas plus etroit que l'etat compact"


def test_le_glissement_choisit_l_etat_le_plus_proche_des_trois():
    """Tirer la poignee choisit la largeur la PLUS PROCHE, sans seuils ecrits a la main.

    On interroge la regle de choix directement, avec les trois largeurs de l'instance : le test
    reste juste si les largeurs changent avec la police ou le zoom.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel

        largeurs = {mode: panel._largeur_du_mode(mode)
                    for mode in ('complet', 'total', 'horloges')}
        # Tire exactement sur chaque largeur : on doit retrouver son etat.
        for mode, largeur in largeurs.items():
            assert panel._mode_le_plus_proche(largeur) == mode, \
                f"{largeur} px devrait donner {mode}"
        # Bien au-dela de la formule complete : on reste sur l'etat le plus large.
        assert panel._mode_le_plus_proche(largeurs['complet'] * 3) == 'complet'
        # Largeur nulle (poignee tiree jusqu'au bord) : l'etat le plus etroit.
        assert panel._mode_le_plus_proche(0) == 'horloges'


def test_le_repli_sur_les_horloges_survit_a_un_rafraichissement():
    """Un nouveau profilage ne doit pas redeployer la colonne dans le dos de l'utilisateur."""
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)
        panel = manager.panel

        panel._basculer_vers('horloges')
        manager.refresh()

        assert panel._mode == 'horloges', "le repli a ete perdu au rafraichissement"
        assert not editor.decorations.get(pr.DECORATION_KEY, []), \
            "la coloration est revenue au rafraichissement"



def test_un_nouveau_profilage_sort_du_repli_sur_les_horloges():
    """Lancer un profilage redeploie la colonne repliee - sur la position INTERMEDIAIRE.

    Demande de l'utilisateur : on vient de demander des temps, les cacher serait absurde. Mais on ne
    va PAS au-dela de l'intermediaire, et un etat deja complet n'est pas touche (test suivant).
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        editor, manager = nouvel_editeur(source)
        panel = manager.panel

        panel._basculer_vers('horloges')
        assert panel._mode == 'horloges'

        # Nouveau profilage du meme fichier.
        pr.publish(ecrire_resultats(dossier, source))

        assert panel._mode == 'total', f"reste en {panel._mode} au lieu de se redeployer"
        assert editor.decorations.get(pr.DECORATION_KEY), \
            "la coloration des lignes n'est pas revenue avec le redeploiement"


def test_un_nouveau_profilage_ne_touche_pas_un_etat_deja_deploye():
    """« On complète si on était déjà sur la vue complète » : l'etat choisi est respecte.

    C'est le pendant indispensable du test precedent : un redeploiement systematique ecraserait le
    choix de l'utilisateur a chaque profilage.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel

        for etat in ('complet', 'total'):
            panel._basculer_vers(etat)
            pr.publish(ecrire_resultats(dossier, source))
            assert panel._mode == etat, \
                f"l'etat {etat} a ete change en {panel._mode} par un nouveau profilage"


def test_le_repli_ne_se_redeploie_pas_tout_seul():
    """Replier ne doit pas declencher son propre redeploiement.

    Garde-fou contre le piege evite a la conception : refresh() du manager est appele PAR le
    changement d'etat ; l'avoir branche au redeploiement aurait rendu tout repli impossible.
    """
    with tempfile.TemporaryDirectory() as dossier:
        source = str(Path(dossier) / "m.py")
        Path(source).write_text(SOURCE)
        pr.publish(ecrire_resultats(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel

        panel._basculer_vers('horloges')
        manager.refresh()
        assert panel._mode == 'horloges', "le repli s'est redeploye tout seul"


# =============================================================================

def main():
    tests = [(nom, objet) for nom, objet in sorted(globals().items())
             if nom.startswith("test_") and callable(objet)]
    echecs = []
    for nom, test in tests:
        pr.clear()
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
