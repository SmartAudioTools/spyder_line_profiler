# -*- coding: utf-8 -*-
"""Mode « Σ » du Line Profiler : comptage des operations elementaires (demande du 30/10/2026).

CE QUE CE TEST COUVRE
  - profile_operations (module pur) : format des nombres repris de SmartTeacher, formule par
    ligne, lecture du JSON, infobulle et position des instructions cachees ;
  - profile_results sur une VRAIE CodeEditor : colonne temps seul / comptes seuls / les deux
    separes par le filet, fond donne par la DERNIERE mesure, retrait de l'autre mesure quand le
    code a change, soulignement et infobulle des instructions cachees ;
  - cx_launcher.py de bout en bout, en sous-processus : portee = corps des fonctions marquees,
    fonction marquee d'un module importe declaree hors script, comptes ecrits malgre une
    exception du script.

Le moteur (complexity_profiler) n'est PAS installe dans l'interpreteur de l'editeur : le test du
lanceur le prend dans son depot (/DATA/Python/complexity_profiler) par PYTHONPATH, et se declare
saute s'il n'y est pas.

Lancement :
    QT_QPA_PLATFORM=offscreen python smartos_tests/test_profile_operations.py
"""

import json
import os
import os.path as osp
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QEvent, QPoint  # noqa: E402
from qtpy.QtGui import QFontMetrics, QHelpEvent  # noqa: E402
from qtpy.QtWidgets import QApplication, QToolTip  # noqa: E402

_app = QApplication.instance() or QApplication([])

from line_profiler.line_profiler import LineStats  # noqa: E402
from spyder.plugins.editor.widgets.codeeditor import CodeEditor  # noqa: E402

from spyder_line_profiler.spyder import profile_operations as po  # noqa: E402
from spyder_line_profiler.spyder import profile_results as pr  # noqa: E402

# Le menu d'en-tete est modal : hors ecran, il bloquerait la serie.
pr.ProfileResultsPanel._ouvrir_menu = lambda self, pos: None

# Les choix de source des deux zones ne se lisent ni ne s'ecrivent dans une configuration :
# chaque test part des defauts (gauche = reference, droite = derniere mesure).
pr.charger_choix_zone = lambda rang: (pr.SOURCES_ZONES[rang], pr.NATURES)
pr.sauver_choix_zone = lambda rang, source, natures: None
# Ni l'etat de la colonne : un gestionnaire d'un test precedent, encore vivant, enregistrait
# « compact » a chaque publication (_redeployer_si_replie), et les tests suivants ne partaient
# plus de 'complet' - echecs intermittents, selon le ramasse-miettes.
pr.sauver_mode_compact = lambda compact: None
pr.sauver_horloges_seules = lambda horloges: None
# Ni les punaises de l'utilisateur : chemin inexistant, l'ecriture echoue en silence.
pr.punaises_path = lambda: '/nonexistent-smartos/pas-de-punaises.json'

# Ni le .prof ni le JSON des comptes de l'utilisateur : chemins inexistants (cf. test_profile_results).
pr._cprofile_path = lambda: '/nonexistent-smartos/pas-de-prof.prof'
pr.operations_path = lambda: '/nonexistent-smartos/pas-d-operations.json'
pr.epingle_path = lambda: '/nonexistent-smartos/pas-d-epingle.json'

MOTEUR = '/DATA/Python/complexity_profiler'
# Lance tel que widgets.py le lance (son dossier en tete de sys.path) : pas d'import ici.
LANCEUR = osp.join(osp.dirname(po.__file__), 'cx_launcher.py')

SOURCE = '''def lente(liste):
    total = 0
    while liste:
        total += liste.pop(0)
    return total
'''


def ecrire_temps(dossier, chemin_source):
    stats = LineStats({(chemin_source, 1, 'lente'): [(2, 1, 400), (3, 11, 4000), (4, 10, 9000)]},
                      1e-9)
    chemin = Path(dossier) / "lineprofiler.results"
    with open(chemin, 'wb') as flux:
        pickle.dump(stats, flux)
    return str(chemin)


def ecrire_operations(dossier, chemin_source, source=SOURCE):
    donnees = {
        'fichier': chemin_source, 'source': source, 'portee': [2, 3, 4, 5],
        'lignes': {'2': [1, 1], '3': [11, 11], '4': [10, 20000000], '5': [1, 1]},
        'caches': [[4, 17, 4, 29, 'liste.pop(0)', 10, 19999990, 'decalage de la liste']],
        'total': 20000013, 'erreur': None, 'hors_script': [],
        'fonctions': {'1': [1, 20000013]},
    }
    chemin = Path(dossier) / "ops.json"
    chemin.write_text(json.dumps(donnees, ensure_ascii=False), encoding='utf-8')
    return str(chemin)


def nouvel_editeur(chemin):
    editor = CodeEditor(None)
    editor.setup_editor(language='Python', filename=chemin)
    editor.set_text(SOURCE)
    editor.resize(900, 500)
    editor.show()
    QApplication.processEvents()
    manager = pr.ProfileResultsManager(editor)
    QApplication.processEvents()
    return editor, manager


def fichier_source(dossier):
    source = str(Path(dossier) / "m.py")
    Path(source).write_text(SOURCE, encoding='utf-8')
    return source


# =============================================================================
# 1. Module pur
# =============================================================================

def test_les_nombres_s_ecrivent_comme_dans_smartteacher():
    assert po.nombre(20000) == '20000'
    assert po.nombre(1.5) == '1,5'
    assert po.nombre(2 * 10 ** 8) == '2·10⁸'
    assert po.nombre(10 ** 6) == '10⁶'
    assert po.nombre(None) == ''


def test_la_formule_d_une_ligne_a_cinq_morceaux():
    ligne = po.LigneComptee(20000, 2 * 10 ** 8)
    assert ligne.segments() == ('20000', '×', '10000 op', '=', '2·10⁸ op'), ligne.segments()
    assert po.LigneComptee(0, 0).segments()[0] == '0'
    # Moyenne alignee sur l'editeur de QCM : exacte en 3 decimales, sinon « ~ ».
    assert po.LigneComptee(1000, 499501).segments()[2] == '499,501 op'
    assert po.LigneComptee(3, 1).segments()[2] == '~0,3 op', po.LigneComptee(3, 1).segments()
    assert po.LigneComptee(4, 2).segments()[2] == '0,5 op'
    assert po.LigneComptee(2, 3 * 10 ** 8).segments()[2] == '1,5·10⁸ op'


def test_la_chaleur_est_logarithmique_et_bornee():
    assert po.chaleur(1, 10 ** 6) == 0.0
    assert abs(po.chaleur(1000, 10 ** 6) - 0.5) < 1e-9
    assert po.chaleur(10 ** 6, 10 ** 6) == 1.0


def test_le_json_est_relu_et_la_chaleur_ramenee_a_la_pire_ligne():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        donnees = po.lire(ecrire_operations(dossier, source))
        assert donnees['fichier'] == osp.normcase(source)
        assert sorted(donnees['lignes']) == [2, 3, 4, 5]
        assert donnees['lignes'][4].heat == 1.0
        cache = donnees['caches'][0]
        assert cache.texte == 'liste.pop(0)'
        assert 'sur 10 passages' in cache.infobulle(), cache.infobulle()
        assert po.lire(str(Path(dossier) / 'absent.json')) is None


def test_une_instruction_cachee_sait_si_une_position_tombe_dedans():
    cache = po.InstructionCachee(4, 17, 4, 30, 'liste.pop(0)', 10, 100, 'x')
    assert cache.contient(4, 17) and cache.contient(4, 29)
    assert not cache.contient(4, 30) and not cache.contient(4, 16) and not cache.contient(3, 20)


# =============================================================================
# 2. Dans l'editeur
# =============================================================================

def test_comptes_seuls_la_colonne_ne_montre_que_les_operations():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish_operations(ecrire_operations(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel
        assert panel._parties() == ['operations']
        assert panel._cellules(4, 'complet') == ['10', '×', '2·10⁶ op', '=', '2·10⁷ op'], \
            panel._cellules(4, 'complet')
        assert panel._cellules(4, 'total') == ['2·10⁷ op']
        assert panel.sizeHint().width() > 0


def test_la_ligne_du_def_porte_le_total_de_la_fonction():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish_operations(ecrire_operations(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel
        panel.natures = ('operations',)      # le fond suit ce que montre la zone de droite
        manager.refresh()
        fonction = manager.fonctions[1]
        assert (fonction.passages, fonction.operations) == (1, 20000013)
        texte = panel._texte_def(None, fonction)
        assert texte in ('1 × 2·10⁷ op = 2·10⁷ op', '2·10⁷ op'), texte
        metrics = QFontMetrics(_editor.font())
        assert panel._largeur_defs(metrics) > metrics.horizontalAdvance(texte)
        # Pas de doublon : la ligne du def reste hors des colonnes des lignes du corps.
        assert 1 not in manager.operations
        # Mais elle a son fond, comme le `def` en mode horloge : son total sature l'echelle.
        assert 1 in manager.lignes_colorees()
        assert manager.chaleur(1) == fonction.heat == 1.0, fonction.heat


def test_les_deux_mesures_sont_separees_par_le_filet():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish(ecrire_temps(dossier, source))
        pr.publish_operations(ecrire_operations(dossier, source))
        _editor, manager = nouvel_editeur(source)
        panel = manager.panel
        assert panel._parties() == ['temps', 'operations']
        cellules = panel._cellules(4, 'total')
        assert len(cellules) == 3 and cellules[1] == panel.FILET and cellules[2] == '2·10⁷ op', \
            cellules
        assert len(panel._cellules(4, 'complet')) == 11
        # Ligne 5 : comptee mais pas chronometree -> pas de filet, temps vide.
        assert panel._cellules(5, 'total') == ['', '', '1 op'], panel._cellules(5, 'total')


def test_glisser_la_poignee_de_gauche_ne_fait_pas_osciller_les_zones():
    """« il y a beaucoup de clignotements, disparitions et réapartion des zones [...] quand on
    glisse la ligne de séparation » (utilisateur, 31/10/2026) : curseur immobile, deux evenements
    de suite doivent donner le meme etat. Mesure depuis le bord droit de la zone tiree, celle de
    gauche voyait ce bord bouger avec la zone de droite et changeait d'etat a chaque evenement."""
    from qtpy.QtGui import QMouseEvent
    from qtpy.QtCore import QPointF, Qt as _Qt
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish(ecrire_temps(dossier, source))
        pr.publish_operations(ecrire_operations(dossier, source))
        _editor, manager = nouvel_editeur(source)
        gauche, droite = manager.zones
        # Attributs poses a la main : appliquer_choix sauverait le choix dans la configuration.
        gauche.source, gauche.natures = 'derniere', ('temps',)
        droite.source, droite.natures = 'derniere', ('operations',)
        manager.refresh()
        QApplication.processEvents()
        gauche._redimensionne = True
        bord = droite.mapToGlobal(droite.rect().topRight()).x()
        etats = set()
        for recul in range(0, 600, 7):
            x = bord - recul
            modes = []
            for _ in range(3):
                local = gauche.mapFromGlobal(gauche.mapToGlobal(gauche.rect().topLeft()))
                position = QPointF(x - gauche.mapToGlobal(local).x(), 5)
                gauche.mouseMoveEvent(QMouseEvent(
                    QMouseEvent.Type.MouseMove, position, QPointF(x, 5),
                    _Qt.LeftButton, _Qt.LeftButton, _Qt.NoModifier))
                QApplication.processEvents()
                modes.append(gauche._mode)
            assert len(set(modes)) == 1, (recul, modes)
            etats.add(modes[0])
        gauche._redimensionne = False
        assert {'complet', 'total', 'horloges'} <= etats, etats


def test_le_fond_suit_ce_que_montre_la_zone_de_droite():
    """Utilisateur, 31/10/2026 : « suit toujours ce qu'il y a dans la colonne de droite »."""
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish_operations(ecrire_operations(dossier, source))
        pr.publish(ecrire_temps(dossier, source))       # chronometrage lance en dernier
        _editor, manager = nouvel_editeur(source)
        gauche, droite = manager.zones
        # Attributs poses a la main : appliquer_choix sauverait le choix dans la configuration.
        gauche.source, gauche.natures = 'derniere', ('temps',)
        droite.source, droite.natures = 'derniere', ('operations',)
        manager.refresh()
        assert manager.chaleur(4) == manager.operations[4].heat
        assert 5 in manager.lignes_colorees()          # comptee, jamais chronometree

        droite.natures = ('temps',)
        manager.refresh()
        assert manager.chaleur(4) == manager.lignes[4].heat
        assert 5 not in manager.lignes_colorees()

        # Droite cachee : plus aucun fond, meme si la gauche montre des temps.
        droite.cachee = True
        manager.refresh()
        assert not manager.lignes_colorees()
        assert manager.chaleur(4) is None


def test_un_code_modifie_retire_l_autre_mesure():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish_operations(ecrire_operations(dossier, source))
        Path(source).write_text(SOURCE + "\n# modifie\n", encoding='utf-8')
        pr.publish(ecrire_temps(dossier, source))
        assert pr.operations_for(source) is None, "comptes d'une autre version du code gardes"
        assert pr.results_for(source)

        # Et dans l'autre sens : les temps pris avant la modification tombent au comptage.
        Path(source).write_text(SOURCE, encoding='utf-8')
        pr.publish_operations(ecrire_operations(dossier, source))
        assert pr.results_for(source) == {}, "temps d'une autre version du code gardes"


def test_l_instruction_cachee_est_soulignee_et_a_son_infobulle():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish_operations(ecrire_operations(dossier, source))
        editor, manager = nouvel_editeur(source)
        soulignes = editor.decorations.get(pr.CACHES_KEY, [])
        assert len(soulignes) == 1, soulignes
        curseur = soulignes[0].cursor
        assert curseur.selectedText() == 'liste.pop(0)', curseur.selectedText()

        # Survol au milieu de l'instruction : l'infobulle du filtre s'affiche.
        bloc = editor.document().findBlockByNumber(3)
        c = editor.textCursor()
        c.setPosition(bloc.position() + 22)
        rect = editor.cursorRect(c)
        pos = QPoint(rect.center().x(), rect.center().y())
        evenement = QHelpEvent(QEvent.ToolTip, pos, editor.viewport().mapToGlobal(pos))
        assert manager.eventFilter(editor.viewport(), evenement) is True
        assert 'liste.pop(0)' in QToolTip.text(), QToolTip.text()


def test_effacer_les_comptes_garde_les_temps():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        pr.publish(ecrire_temps(dossier, source))
        pr.publish_operations(ecrire_operations(dossier, source))
        pr.clear('operations')
        assert pr.operations_for(source) is None and pr.results_for(source)
        editor, _manager = nouvel_editeur(source)
        assert not editor.decorations.get(pr.CACHES_KEY, [])


def test_l_epingle_s_affiche_avant_le_comptage_suivant_et_survit_au_code_modifie():
    with tempfile.TemporaryDirectory() as dossier:
        source = fichier_source(dossier)
        chemin_ops = ecrire_operations(dossier, source)
        pr.operations_path = lambda: chemin_ops
        pr.epingle_path = lambda: str(Path(dossier) / 'epingle.json')
        try:
            assert pr.epingler() is None, "epingle posee sans aucun comptage"
            pr.publish_operations(chemin_ops)
            assert pr.epingler() is not None
            # Le code change (n change), puis un nouveau comptage, dix fois plus couteux en l. 4.
            nouvelle = "# n = 1000\n" + SOURCE
            Path(source).write_text(nouvelle, encoding='utf-8')
            donnees = json.loads(Path(chemin_ops).read_text(encoding='utf-8'))
            donnees.update(source=nouvelle, lignes={'5': [100, 2 * 10 ** 8]}, caches=[])
            Path(chemin_ops).write_text(json.dumps(donnees), encoding='utf-8')
            pr.publish_operations(chemin_ops)
            editor, manager = nouvel_editeur(source)
            editor.set_text(nouvelle)
            manager.refresh()
            # Deux zones (31/10/2026) : l'epingle dans la zone de reference, a gauche, le
            # comptage courant dans celle de droite - plus de « avant │ apres » dans une seule.
            reference, derniere = manager.zones
            assert reference.source == 'reference' and derniere.source == 'derniere'
            assert reference._parties() == ['operations'], reference._parties()
            assert derniere._parties() == ['operations'], derniere._parties()
            # L'ancienne ligne 4 est devenue la 5 : l'epingle y est recalee.
            assert reference._cellules(5, 'total') == ['2·10⁷ op'], reference._cellules(5, 'total')
            assert derniere._cellules(5, 'total') == ['2·10⁸ op'], derniere._cellules(5, 'total')
            assert len(reference._cellules(5, 'complet')) == 5
            # Ligne epinglee sans comptage courant : rien a droite.
            assert reference._cellules(3, 'total') == ['1 op'], reference._cellules(3, 'total')
            assert derniere._cellules(3, 'total') == [''], derniere._cellules(3, 'total')
            # L'epingle ne donne pas le fond : c'est le comptage courant.
            assert 3 not in manager.lignes_colorees(), manager.lignes_colorees()
            # Mais la zone de DROITE sur la reference : le fond suit l'epingle (utilisateur,
            # 31/10/2026 : « le comptage de reference sur la colonne de droite ne colore rien »).
            derniere.source, derniere.natures = 'reference', ('operations',)
            manager.refresh()
            assert {3, 5} <= set(manager.lignes_colorees()), manager.lignes_colorees()
            assert manager.chaleur(5) == manager.epingle[5].heat > 0, manager.chaleur(5)
            derniere.source = 'derniere'
            manager.refresh()
            reference.grab()                              # paintEvent, en-tete compris
            derniere.grab()
            # L'effacement des mesures garde l'epingle ; le retrait l'enleve, disque compris.
            pr.clear()
            manager.refresh()
            assert manager.epingle and reference.sizeHint().width() > 0
            pr.desepingler()
            manager.refresh()
            assert not manager.epingle and not osp.exists(pr.epingle_path())
        finally:
            pr.operations_path = lambda: '/nonexistent-smartos/pas-d-operations.json'
            pr.epingle_path = lambda: '/nonexistent-smartos/pas-d-epingle.json'


# =============================================================================
# 3. Lanceur de bout en bout
# =============================================================================

SCRIPT = '''import aide

def marquee(n):
    liste = list(range(n))
    total = 0
    while liste:
        total += liste.pop(0)
    return total

def ignoree(n):
    return sum(range(n))

marquee(200)
ignoree(200)
aide.f()
raise ValueError("fin")
'''


def test_la_punaise_du_def_punaise_la_fonction_entiere():
    pr._PUNAISES = {}
    try:
        with tempfile.TemporaryDirectory() as dossier:
            source = fichier_source(dossier)
            pr.publish_operations(ecrire_operations(dossier, source))
            _editor, manager = nouvel_editeur(source)
            derniere = manager.panel
            assert derniere._a_punaiser(1)
            valeurs = derniere._valeurs_punaise(1)
            assert sorted(valeurs) == [('fonctions', 1)] + [
                ('operations', n) for n in (2, 3, 4, 5)], sorted(valeurs)
            manager.basculer_punaise(valeurs)
            QApplication.processEvents()
            assert manager.punaise_identique(valeurs)
            assert all(manager.est_punaisee(n) for n in (1, 2, 3, 4, 5))
            # La zone de reference montre la fonction punaisee, sans historique de comptage.
            _temps, comptes, fonctions = manager.donnees('reference')
            assert set(comptes) == {2, 3, 4, 5} and fonctions[1].operations == 20000013
            # Et elle COLORE : relue sans chaleur, une punaise donnait la couleur du fond
            # (utilisateur, 31/10/2026 : « le comptage de reference sur la colonne de droite ne
            # colore rien »). Le `def` sature, comme pour un comptage.
            derniere.source, derniere.natures = 'reference', ('operations',)
            manager.refresh()
            assert manager.chaleur(4) == manager.operations[4].heat > 0, manager.chaleur(4)
            assert manager.chaleur(1) == 1.0, manager.chaleur(1)
            manager.basculer_punaise(derniere._valeurs_punaise(1))
            assert pr._PUNAISES == {}, pr._PUNAISES
    finally:
        pr._PUNAISES = {}


def test_le_lanceur_compte_le_corps_des_fonctions_marquees():
    if not osp.isdir(osp.join(MOTEUR, 'complexity_profiler')):
        print("       (saute : moteur absent)")
        return
    with tempfile.TemporaryDirectory() as dossier:
        script = osp.join(dossier, 'prog.py')
        Path(script).write_text(SCRIPT, encoding='utf-8')
        aide = osp.join(dossier, 'aide.py')
        Path(aide).write_text("def f():\n    return 1\n", encoding='utf-8')
        config = osp.join(dossier, 'config.json')
        with open(config, 'w', encoding='utf-8') as flux:
            json.dump({'targets': {script: [3], aide: [1]}}, flux)
        sortie = osp.join(dossier, 'ops.json')
        env = dict(os.environ, PYTHONPATH=MOTEUR)
        resultat = subprocess.run(
            [sys.executable, '-X', 'utf8', LANCEUR, '--ops', sortie,
             '--config', config, script], capture_output=True, text=True, timeout=120, env=env)
        assert resultat.returncode == 1, (resultat.returncode, resultat.stderr)
        assert 'ValueError' in resultat.stderr
        donnees = po.lire(sortie)
        assert donnees is not None, resultat.stderr
        assert set(donnees['lignes']) <= set(range(4, 9)), sorted(donnees['lignes'])
        assert 7 in donnees['lignes'] and donnees['lignes'][7].passages == 200
        assert any(c.texte == 'liste.pop(0)' for c in donnees['caches']), donnees['caches']
        # Colonnes du moteur = celles que l'editeur souligne : elles retombent sur le texte.
        lignes_script = SCRIPT.splitlines()
        for c in donnees['caches']:
            if c.ligne == c.fin_ligne:
                assert lignes_script[c.ligne - 1][c.colonne:c.fin_colonne] == c.texte, c.texte
        assert donnees['hors_script'] == [osp.normcase(aide)], donnees['hors_script']
        assert 'ValueError' in (donnees['erreur'] or ''), donnees['erreur']
        # Total de la fonction marquee, sur la ligne de son def : un appel, la somme de son corps.
        assert set(donnees['fonctions']) == {3}, donnees['fonctions']
        assert donnees['fonctions'][3].passages == 1
        assert donnees['fonctions'][3].operations == sum(
            c.operations for c in donnees['lignes'].values())


RECURSIF = '''def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

def externe(k):
    def interne(x):
        return x * 2
    return sum(interne(i) for i in range(k))

def gen(n):
    for i in range(n):
        yield i

fib(10)
externe(5)
list(gen(3))
'''


def test_le_total_compte_chaque_appel_recursif_et_pas_deux_fois_l_imbrique():
    if not osp.isdir(osp.join(MOTEUR, 'complexity_profiler')):
        print("       (saute : moteur absent)")
        return
    with tempfile.TemporaryDirectory() as dossier:
        script = osp.join(dossier, 'rec.py')
        Path(script).write_text(RECURSIF, encoding='utf-8')
        config = osp.join(dossier, 'config.json')
        with open(config, 'w', encoding='utf-8') as flux:
            json.dump({'targets': {script: [1, 6, 11]}}, flux)
        sortie = osp.join(dossier, 'ops.json')
        env = dict(os.environ, PYTHONPATH=MOTEUR)
        resultat = subprocess.run(
            [sys.executable, '-X', 'utf8', LANCEUR, '--ops', sortie,
             '--config', config, script], capture_output=True, text=True, timeout=120, env=env)
        assert resultat.returncode == 0, resultat.stderr
        donnees = po.lire(sortie)
        lignes, fonctions = donnees['lignes'], donnees['fonctions']
        # fib(10) : 177 appels (PY_START a chaque appel recursif), corps entier additionne.
        assert fonctions[1].passages == 177, fonctions[1].passages
        assert fonctions[1].operations == sum(lignes[n].operations for n in (2, 3, 4))
        # externe : la ligne 9 porte deja le cout d'interne (appel inclusif) -> la ligne 8, corps
        # d'interne, n'est pas ajoutee une seconde fois.
        assert fonctions[6].operations == lignes[7].operations + lignes[9].operations
        # Generateur : un seul appel, malgre ses reprises.
        assert fonctions[11].passages == 1, fonctions[11].passages


# =============================================================================

def main():
    tests = [(nom, objet) for nom, objet in sorted(globals().items())
             if nom.startswith("test_") and callable(objet)]
    echecs = []
    for nom, test in tests:
        pr.clear()
        pr._EPINGLE = None
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
