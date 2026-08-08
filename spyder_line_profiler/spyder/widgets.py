# -*- coding: utf-8 -*-
#
# -----------------------------------------------------------------------------
# Copyright (c) 2013- Spyder Project Contributors
#
# Released under the terms of the MIT License
# (see LICENSE.txt in the project root directory for details)
# -----------------------------------------------------------------------------
"""
Spyder Line Profiler Main Widget.
"""
# Standard library imports
import inspect
import linecache
import logging
import os
import os.path as osp
import pickle
import re
import time
from datetime import datetime

# Third party imports
from qtpy.QtGui import QBrush, QColor, QFont
from qtpy.QtCore import (QByteArray, QProcess, Qt,
                         QProcessEnvironment, Signal, QTimer)
from qtpy.QtWidgets import (QMessageBox, QVBoxLayout, QLabel,
                            QTreeWidget, QTreeWidgetItem, QApplication)
from qtpy.compat import getopenfilename, getsavefilename

# Spyder imports
from spyder.api.config.decorators import on_conf_change
from spyder.api.translations import get_translation
from spyder.api.widgets.main_widget import PluginMainWidget
from spyder.config.base import get_conf_path
from spyder.plugins.variableexplorer.widgets.texteditor import TextEditor
from spyder.utils import programs
from spyder.utils.misc import getcwd_or_home
from spyder.utils.palette import SpyderPalette
from spyder.widgets.comboboxes import PythonModulesComboBox

# Local imports
from spyder_line_profiler.spyder.config import CONF_SECTION

# Localization and logging
_ = get_translation("spyder")
logger = logging.getLogger(__name__)

COL_NO = 0
COL_HITS = 1
COL_TIME = 2
COL_PERHIT = 3
COL_PERCENT = 4
COL_LINE = 5
COL_POS = 0  # Position is not displayed but set as Qt.UserRole

CODE_NOT_RUN_COLOR = QBrush(QColor.fromRgb(128, 128, 128, 200))

# Cycle to use when coloring lines from different functions
COLOR_CYCLE = [
    SpyderPalette.GROUP_1,
    SpyderPalette.GROUP_4,
    SpyderPalette.GROUP_10,
    SpyderPalette.GROUP_12,
    SpyderPalette.GROUP_2,
    SpyderPalette.GROUP_8,
    SpyderPalette.GROUP_6]

WEBSITE_URL = 'http://pythonhosted.org/line_profiler/'


def is_lineprofiler_installed():
    """
    Check if the program and the library for line_profiler is installed.
    """
    return (programs.is_module_installed('line_profiler')
            and programs.is_module_installed('kernprof'))


class TreeWidgetItem(QTreeWidgetItem):
    """
    An extension of QTreeWidgetItem that replaces the sorting behaviour
    such that the sorting is not purely by ASCII index but by natural
    sorting, e.g. multi-digit numbers sorted based on their value instead
    of individual digits.

    Taken from
    https://stackoverflow.com/questions/21030719/sort-a-pyside-qtgui-
    qtreewidget-by-an-alpha-numeric-column/
    """
    def __lt__(self, other):
        """
        Compare a widget text entry to another entry.
        """
        column = self.treeWidget().sortColumn()
        key1 = self.text(column)
        key2 = other.text(column)
        return self.natural_sort_key(key1) < self.natural_sort_key(key2)

    @staticmethod
    def natural_sort_key(key):
        """
        Natural sorting for both numbers and strings containing numbers.
        """
        regex = r'(\d*\.\d+|\d+)'
        parts = re.split(regex, key)
        return tuple((e if i % 2 == 0 else float(e))
                     for i, e in enumerate(parts))


class SpyderLineProfilerWidgetActions:
    # Triggers
    Browse = 'browse_action'
    Clear = 'clear_action'
    Collapse = 'collapse_action'
    Expand = 'expand_action'
    LoadData = 'load_data_action'
    Run = 'run_action'
    SaveData = 'save_data_action'
    ShowOutput = 'show_output_action'
    Stop = 'stop_action'


class SpyderLineProfilerWidgetMainToolbarSections:
    Main = 'main_section'
    ExpandCollaps = 'expand_collaps_section'
    ShowOutput = 'show_output_section'


class SpyderLineProfilerWidgetToolbars:
    Information = 'information_toolbar'


class SpyderLineProfilerWidgetMainToolbarItems:
    FileCombo = 'file_combo'


class SpyderLineProfilerWidgetInformationToolbarSections:
    Main = 'main_section'


class SpyderLineProfilerWidgetInformationToolbarItems:
    Stretcher1 = 'stretcher_1'
    Stretcher2 = 'stretcher_2'
    DateLabel = 'date_label'


class SpyderLineProfilerWidget(PluginMainWidget):

    # PluginMainWidget class constants
    CONF_SECTION = CONF_SECTION
    DATAPATH = get_conf_path('lineprofiler.results')
    VERSION = '0.0.1'

    redirect_stdio = Signal(bool)
    sig_finished = Signal()
    # Signals
    sig_edit_goto_requested = Signal(str, int, str)
    """
    This signal will request to open a file in a given row and column
    using a code editor.

    Parameters
    ----------
    path: str
        Path to file.
    row: int
        Cursor starting row position.
    word: str
        Word to select on given row.
    """

    def __init__(self, name=None, plugin=None, parent=None):
        super().__init__(name, plugin, parent)
        self.setWindowTitle("Line profiler")

        # Attributes
        self._last_wdir = None
        self._last_args = None
        self.pythonpath = None
        self.error_output = None
        self.output = None
        self.use_colors = True
        self.process = None
        self.started_time = None

        # Widgets
        self.filecombo = PythonModulesComboBox(
            self, id_=SpyderLineProfilerWidgetMainToolbarItems.FileCombo)
        self.datatree = LineProfilerDataTree(self)
        self.datelabel = QLabel(self)
        self.datelabel.ID = SpyderLineProfilerWidgetInformationToolbarItems.DateLabel
        self.datelabel.setText(_('Please select a file to profile, with '
                                 'added @profile decorators for functions'))
        self.timer = QTimer(self)

        layout = QVBoxLayout()
        layout.addWidget(self.datatree)
        self.setLayout(layout)

        # Signals
        self.datatree.sig_edit_goto_requested.connect(
            self.sig_edit_goto_requested)

    # --- PluginMainWidget API
    # ------------------------------------------------------------------------
    def get_title(self):
        return _("Line Profiler")

    def get_focus_widget(self):
        pass

    def setup(self):

        self.start_action = self.create_action(
            SpyderLineProfilerWidgetActions.Run,
            text=_("Profile by line"),
            tip=_("Run line profiler"),
            icon=self.create_icon('run'),
            triggered=self.start,
        )
        self.stop_action = self.create_action(
            SpyderLineProfilerWidgetActions.Stop,
            text=_("Stop"),
            tip=_("Stop current profiling"),
            icon=self.create_icon('stop'),
            triggered=self.kill_if_running,
        )
        self.browse_action = self.create_action(
            SpyderLineProfilerWidgetActions.Browse,
            text=_("Open Script"),
            tip=_('Select Python script'),
            icon=self.create_icon('fileopen'),
            triggered=self.select_file,
        )
        self.log_action = self.create_action(
            SpyderLineProfilerWidgetActions.ShowOutput,
            text=_("Show Result"),
            tip=_("Show program's output"),
            icon=self.create_icon('log'),
            triggered=self.show_log,
        )
        self.collapse_action = self.create_action(
            SpyderLineProfilerWidgetActions.Collapse,
            text=_("Collaps"),
            tip=_('Collapse all'),
            icon=self.create_icon('collapse'),
            triggered=lambda dD=-1: self.datatree.collapseAll(),
        )
        self.expand_action = self.create_action(
            SpyderLineProfilerWidgetActions.Expand,
            text=_("Expand"),
            tip=_('Expand all'),
            icon=self.create_icon('expand'),
            triggered=lambda dD=-1: self.datatree.expandAll(),
        )
        self.save_action = self.create_action(
            SpyderLineProfilerWidgetActions.SaveData,
            text=_("Save data"),
            tip=_('Save line profiling data'),
            icon=self.create_icon('filesave'),
            triggered=self.save_data,
        )
        self.clear_action = self.create_action(
            SpyderLineProfilerWidgetActions.Clear,
            text=_("Clear output"),
            tip=_('Clear'),
            icon=self.create_icon('editdelete'),
            triggered=self.clear_data,
        )

        self.set_running_state(False)
        self.start_action.setEnabled(False)
        self.clear_action.setEnabled(False)
        self.log_action.setEnabled(False)
        self.save_action.setEnabled(False)

        # SmartOS (patch_spyder_lineprofiler_toolbar.py) : le menu burger de ce dock etait vide
        # - il ne contenait que les quatre actions de dock ajoutees d'office par PluginMainWidget.
        # Il recoit ici le pliage de l'arbre, les actions de sortie et de donnees, et le bouton
        # "parcourir" ; les deux barres se reduisent d'autant.
        # SmartOS : libelles FRANCAIS explicites. Ce greffon n'a AUCUN catalogue de traduction
        # (Spyder le journalise au demarrage : "No translation file found for domain
        # 'spyder_line_profiler.spyder'"), et ses libelles ne se voyaient pas jusqu'ici - boutons a
        # icone seule, le texte ne servant qu'a l'infobulle. Dans un menu ils se LISENT, et ils
        # melangeaient anglais, francais et une coquille amont ("Collaps"). On ne passe pas par _()
        # : sans catalogue, il rendrait la chaine inchangee.
        for item, libelle in [
            (self.collapse_action, "Tout replier"),
            (self.expand_action, "Tout déplier"),
            (self.log_action, "Sortie complète"),
            (self.clear_action, "Effacer la sortie"),
            (self.browse_action, "Sélectionner un fichier Python"),
        ]:
            item.setText(libelle)

        options_menu = self.get_options_menu()
        for item in [self.collapse_action, self.expand_action]:
            self.add_item_to_menu(
                item, menu=options_menu, section="smartos_lp_tree"
            )
        for item in [self.log_action, self.save_action, self.clear_action,
                     self.browse_action]:
            self.add_item_to_menu(
                item, menu=options_menu, section="smartos_lp_data"
            )

        # Main Toolbar
        # SmartOS : la liste des fichiers et les deux commandes qui font le travail. "Open Script"
        # (parcourir) est dans le menu burger : on profile presque toujours le fichier courant, et
        # la liste deroulante garde de toute facon l'historique.
        toolbar = self.get_main_toolbar()
        for item in [self.filecombo, self.start_action,
                     self.stop_action]:
            self.add_item_to_toolbar(
                item,
                toolbar=toolbar,
                section=SpyderLineProfilerWidgetMainToolbarSections.Main,
            )

        # Secondary Toolbar
        # SmartOS : reduite a la date de la derniere mesure, centree entre ses deux etirements.
        secondary_toolbar = self.create_toolbar(
            SpyderLineProfilerWidgetToolbars.Information)
        for item in [self.create_stretcher(
                         id_=SpyderLineProfilerWidgetInformationToolbarItems.Stretcher1),
                     self.datelabel,
                     self.create_stretcher(
                         id_=SpyderLineProfilerWidgetInformationToolbarItems.Stretcher2)]:
            self.add_item_to_toolbar(
                item,
                toolbar=secondary_toolbar,
                section=SpyderLineProfilerWidgetInformationToolbarSections.Main,
            )

        if not is_lineprofiler_installed():
            for widget in (self.datatree, self.filecombo, self.log_action,
                           self.start_action, self.stop_action, self.browse_action,
                           self.collapse_action, self.expand_action):
                widget.setDisabled(True)
            text = _(
                '<b>Please install the <a href="%s">line_profiler module</a></b>'
                ) % WEBSITE_URL
            self.datelabel.setText(text)
            self.datelabel.setOpenExternalLinks(True)
        else:
            pass

    def analyze(self, filename=None, wdir=None, args=None, use_colors=True):
        self.use_colors = use_colors
        if not is_lineprofiler_installed():
            return
        self.kill_if_running()
        #index, _data = self.get_data(filename) # FIXME: storing data is not implemented yet
        if filename is not None:
            filename = osp.abspath(str(filename))
            index = self.filecombo.findText(filename)
            if index == -1:
                self.filecombo.addItem(filename)
                self.filecombo.setCurrentIndex(self.filecombo.count()-1)
            else:
                self.filecombo.setCurrentIndex(index)
            self.filecombo.selected()

        if self.filecombo.is_valid():
            filename = str(self.filecombo.currentText())
            if wdir is None:
                wdir = osp.dirname(filename)
            self.start(wdir, args)

    def select_file(self):
        self.redirect_stdio.emit(False)
        pwd = getcwd_or_home()

        filename, _selfilter = getopenfilename(
            self, _("Select Python script"), pwd,
            _("Python scripts")+" (*.py ; *.pyw)")
        self.redirect_stdio.emit(False)

        if filename:
            self.analyze(filename)

    def show_log(self):
        if self.output:
            editor = TextEditor(self.output, title=_("Line profiler output"),
                                readonly=True, parent=self)

            # Call .show() to dynamically resize editor;
            # see spyder-ide/spyder#12202
            editor.show()
            editor.exec_()

    def show_errorlog(self):
        if self.error_output:
            editor = TextEditor(self.error_output,
                                title=_("Line profiler output"),
                                readonly=True, parent=self)
            self.datelabel.setText(_('Profiling did not complete (error)'))
            # Call .show() to dynamically resize editor;
            # see spyder-ide/spyder#12202
            editor.show()
            editor.exec_()

    def update_timer(self):
        elapsed = str(datetime.now() - self.started_time).split(".")[0]
        self.datelabel.setText(_(f'Profiling, please wait... elapsed: {elapsed}'))

    def start(self, wdir=None, args=None):
        filename = str(self.filecombo.currentText())

        if wdir in [None, False]:
            wdir = self._last_wdir
            if wdir in [None, False]:
                wdir = osp.dirname(filename)

        if args is None:
            args = self._last_args
            if args is None:
                args = []

        self._last_wdir = wdir
        self._last_args = args

        self.datelabel.setText(_('Profiling starting up, please wait...'))
        self.started_time = datetime.now()

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.SeparateChannels)
        self.process.setWorkingDirectory(wdir)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(
            lambda: self.read_output(error=True))
        self.process.finished.connect(self.finished)

        proc_env = QProcessEnvironment()
        for k, v in os.environ.items():
            proc_env.insert(k, v)
        proc_env.remove('PYTHONPATH')
        if self.pythonpath is not None:
            logger.debug(f"Pass Pythonpath {self.pythonpath} to process")
            proc_env.insert('PYTHONPATH', os.pathsep.join(self.pythonpath))
        self.process.setProcessEnvironment(proc_env)

        self.clear_data()
        self.error_output = ''

        # Use UTF-8 mode so that profiler writes its output to DATAPATH using
        # UTF-8 encoding, instead of the ANSI code page on Windows.
        # See issue spyder-ide/spyder-line-profiler#90
        #
        # UTF-8 mode also changes the encoding of stdin/stdout/stdout which must
        # be taken into account when using stdandard I/O.
        # Lanceur SmartOS (cf. patch_spyder_line_profiler_targets.py et lp_launcher.py) :
        # remplace kernprof. Execution UNIQUE du script, decoration AST des seuls `def`
        # marques (ou de tout le code utilisateur si l'option est cochee), et cProfile
        # enveloppant la seule execution du script : les chiffres du panneau Profileur
        # sont ceux du code de l'utilisateur, pas de la machinerie du profileur.
        # logger.debug et NON warning : sans gestionnaire, le logging retombe sur stderr,
        # que la console interne de Spyder traite comme des erreurs.
        from spyder_line_profiler.spyder.profile_targets import ecrire_config_lanceur
        self._smartos_prof_path = get_conf_path('lineprofiler_cprofile.prof')
        _smartos_lp_launcher = os.path.join(os.path.dirname(__file__), 'lp_launcher.py')
        _smartos_lp_config = get_conf_path('lineprofiler_targets.json')
        ecrire_config_lanceur(filename, _smartos_lp_config, log=logger.debug)
        p_args = ['-X', 'utf8', _smartos_lp_launcher,
                  '--lprof', self.DATAPATH, '--prof', self._smartos_prof_path,
                  '--config', _smartos_lp_config]
        # Redirection Pyxel (ajout SmartOS, cf. patch_spyder_line_profiler_targets.py) :
        # si le script profile importe pyxel, son ecran doit apparaitre dans le panneau
        # Pyxel plutot que dans sa propre fenetre SDL - best-effort, sans consequence pour
        # un script ordinaire ni si le greffon Pyxel n'est pas installe.
        try:
            from spyder_pyxel.spyder.bridge_manager import get_bridge as _smartos_get_bridge
            import spyder_pyxel as _smartos_spyder_pyxel
            _smartos_pyxel_bridge_dir = os.path.dirname(
                os.path.dirname(_smartos_spyder_pyxel.__file__))
            _smartos_pyxel_shm = _smartos_get_bridge().attach_process(self)
        except Exception:
            _smartos_pyxel_shm = None
        if _smartos_pyxel_shm:
            p_args += ['--pyxel-shm', _smartos_pyxel_shm,
                       '--pyxel-bridge-path', _smartos_pyxel_bridge_dir]

        if os.name == 'nt':
            # On Windows, one has to replace backslashes by slashes to avoid
            # confusion with escape characters (otherwise, for example, '\t'
            # will be interpreted as a tabulation):
            p_args.append(osp.normpath(filename).replace(os.sep, '/'))
        else:
            p_args.append(filename)
        if args:
            p_args.extend(programs.shell_split(args))

        executable = self.get_conf('executable', section='main_interpreter')
        if executable.endswith('spyder.exe'):
            # py2exe distribution
            executable = 'python.exe'

        logger.debug(f'Starting process with {executable=} and {p_args=}')
        self.process.start(executable, p_args)

        running = self.process.waitForStarted()
        self.set_running_state(running)
        self.timer.timeout.connect(self.update_timer)
        self.timer.start(1000)

        if not running:
            QMessageBox.critical(self, _("Error"),
                                 _("Process failed to start"))

    def set_running_state(self, state=True):
        self.start_action.setEnabled(not state)
        self.stop_action.setEnabled(state)

    def read_output(self, error=False):
        if error:
            self.process.setReadChannel(QProcess.StandardError)
        else:
            self.process.setReadChannel(QProcess.StandardOutput)
        qba = QByteArray()
        while self.process.bytesAvailable():
            if error:
                qba += self.process.readAllStandardError()
            else:
                qba += self.process.readAllStandardOutput()
        # encoding: Python process is started with UTF-8 mode
        text = str(qba.data(), encoding="utf-8")
        if error:
            self.error_output += text
        else:
            self.output += text

    def finished(self):
        self.timer.stop()
        # Redirection Pyxel (ajout SmartOS) : libere le canal cree avant le lancement.
        try:
            from spyder_pyxel.spyder.bridge_manager import get_bridge as _smartos_get_bridge
            _smartos_get_bridge().detach_console(self)  # SmartOS
        except Exception:
            pass
        self.set_running_state(False)
        self.output = self.error_output + self.output
        if not self.output == 'aborted':
            elapsed = str(datetime.now() - self.started_time).split(".")[0]
            self.show_data(justanalyzed=True)
            self.datelabel.setText(_(f'Profiling finished after {elapsed}'))
        self.show_errorlog()  # If errors occurred, show them.
        self.sig_finished.emit()

    def kill_if_running(self):
        # Arret gracieux (ajout SmartOS, cf. patch_spyder_line_profiler_targets.py) :
        # SIGTERM d'abord (lp_launcher.py l'attrape et ecrit ses mesures partielles),
        # SIGKILL en repli si le processus ne repond pas dans le delai.
        if self.process is not None:
            if self.process.state() == QProcess.Running:
                self.process.terminate()
                if self.process.waitForFinished(5000):
                    self.datelabel.setText(_('Profiling interrupted.'))
                else:
                    self.process.kill()
                    self.output = 'aborted'
                    self.datelabel.setText(_('Profiling aborted.'))
                    self.process.waitForFinished()
            else:
                self.datelabel.setText(_('Profiling aborted.'))
        else:
            self.datelabel.setText(_('Profiling aborted.'))

    @on_conf_change(section='pythonpath_manager', option='spyder_pythonpath')
    def _update_pythonpath(self, value):
        self.pythonpath = value

    def clear_data(self):
        self.datatree.clear()
        # Le bouton d'effacement du panneau vide aussi l'editeur (ajout SmartOS) : sans cela
        # les lignes resteraient colorees par un profilage que l'utilisateur vient d'effacer.
        from spyder_line_profiler.spyder.profile_results import clear as _smartos_clear_results
        _smartos_clear_results()
        self.clear_action.setEnabled(False)
        self.log_action.setEnabled(False)
        self.save_action.setEnabled(False)
        self.output = ''

    def show_data(self, justanalyzed=False):
        if not justanalyzed:
            self.clear_data()
        output_exists = self.output is not None and len(self.output) > 0
        self.clear_action.setEnabled(output_exists)
        self.log_action.setEnabled(output_exists)
        self.save_action.setEnabled(output_exists)

        self.kill_if_running()
        filename = str(self.filecombo.currentText())
        if not filename:
            return

        self.datatree.load_data(self.DATAPATH)

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

        # Publication des resultats vers les editeurs ouverts (ajout SmartOS, cf.
        # Commun/scripts/patch_spyder_line_profiler_targets.py) : lignes colorees et temps dans
        # la marge de droite. On se greffe ICI, apres le chargement par l'arbre du panneau, pour
        # relire le MEME fichier au MEME moment - donc jamais de desynchronisation entre ce que
        # montre le panneau et ce que montre l'editeur.
        from spyder_line_profiler.spyder.profile_results import publish
        publish(self.DATAPATH)
        QApplication.processEvents()
        self.datatree.show_tree()

        text_style = "<span style=\'color: #444444\'><b>%s </b></span>"
        date_text = text_style % time.strftime("%d %b %Y %H:%M",
                                               time.localtime())
        self.datelabel.setText(date_text)

    def save_data(self):
        """Save data."""
        if not self.output:
            self.datelabel.setText(_("Nothing to save"))
            return

        title = _("Save line profiler result")
        curr_filename = self.filecombo.currentText()
        filename, _selfilter = getsavefilename(
            self,
            title,
            f'{curr_filename}_lineprof.txt',
            _("LineProfiler result") + " (*.txt)",
        )

        if filename:
            with open(filename, 'w') as f:
                # for some weird reason, everything is double spaced on Win
                results = self.output
                results = results.replace('\r', '')
                f.write(results)

            self.datelabel.setText(_(f"Saved results to {filename}"))

    def update_actions(self):
        pass


class LineProfilerDataTree(QTreeWidget):
    """
    Convenience tree widget (with built-in model)
    to store and view line profiler data.
    """
    sig_edit_goto_requested = Signal(str, int, str)

    def __init__(self, parent=None):
        QTreeWidget.__init__(self, parent)
        self.header_list = [
            _('Line #'), _('Hits'), _('Time (ms)'), _('Per hit (ms)'),
            _('% Time'), _('Line contents')]
        self.stats = None      # To be filled by self.load_data()
        self.max_time = 0      # To be filled by self.load_data()
        self.header().setDefaultAlignment(Qt.AlignCenter)
        self.setColumnCount(len(self.header_list))
        self.setHeaderLabels(self.header_list)
        self.clear()
        self.itemClicked.connect(self.on_item_clicked)

    def show_tree(self):
        """Populate the tree with line profiler data and display it."""
        self.clear()  # Clear before re-populating
        self.setItemsExpandable(True)
        self.setSortingEnabled(False)
        self.populate_tree()
        self.expandAll()
        for col in range(self.columnCount()-1):
            self.resizeColumnToContents(col)
        if self.topLevelItemCount() > 1:
            self.collapseAll()
        self.setSortingEnabled(True)
        self.sortItems(COL_POS, Qt.AscendingOrder)

    def load_data(self, profdatafile):
        """Load line profiler data saved by kernprof module"""
        # lstats has the following layout :
        # lstats.timings =
        #     {(filename1, line_no1, function_name1):
        #         [(line_no1, hits1, total_time1),
        #          (line_no2, hits2, total_time2)],
        #      (filename2, line_no2, function_name2):
        #         [(line_no1, hits1, total_time1),
        #          (line_no2, hits2, total_time2),
        #          (line_no3, hits3, total_time3)]}
        # lstats.unit = time_factor
        with open(profdatafile, 'rb') as fid:
            lstats = pickle.load(fid)

        # First pass to group by filename
        self.stats = dict()
        linecache.checkcache()
        for func_info, stats in lstats.timings.items():
            # func_info is a tuple containing (filename, line, function anme)
            filename, start_line_no = func_info[:2]

            # Read code
            start_line_no -= 1  # include the @profile decorator
            all_lines = linecache.getlines(filename)
            block_lines = inspect.getblock(all_lines[start_line_no:])

            # Loop on each line of code
            func_stats = []
            func_total_time = 0.0
            next_stat_line = 0
            for line_no, code_line in enumerate(block_lines):
                line_no += start_line_no + 1  # Lines start at 1
                code_line = code_line.rstrip('\n')
                if (next_stat_line >= len(stats)
                        or line_no != stats[next_stat_line][0]):
                    # Line didn't run
                    hits, line_total_time, time_per_hit = None, None, None
                else:
                    # Compute line times
                    hits, line_total_time = stats[next_stat_line][1:]
                    line_total_time *= lstats.unit
                    time_per_hit = line_total_time / hits
                    func_total_time += line_total_time
                    next_stat_line += 1
                func_stats.append(
                    [line_no, code_line, line_total_time, time_per_hit,
                     hits])

            # Compute percent time
            for line in func_stats:
                line_total_time = line[2]
                if line_total_time is None:
                    line.append(None)
                else:
                    line.append(line_total_time / func_total_time)

            # Fill dict
            self.stats[func_info] = [func_stats, func_total_time]

    def fill_item(self, item, filename, line_no, code, time, percent, perhit,
                  hits):
        item.setData(COL_POS, Qt.UserRole, (osp.normpath(filename), line_no))

        item.setData(COL_NO, Qt.DisplayRole, line_no)

        item.setData(COL_LINE, Qt.DisplayRole, code)

        if percent is None:
            percent = ''
        else:
            percent = '%.1f' % (100 * percent)
        item.setData(COL_PERCENT, Qt.DisplayRole, percent)
        item.setTextAlignment(COL_PERCENT, Qt.AlignCenter)

        if time is None:
            time = ''
        else:
            time = '%.3f' % (time * 1e3)
        item.setData(COL_TIME, Qt.DisplayRole, time)
        item.setTextAlignment(COL_TIME, Qt.AlignCenter)

        if perhit is None:
            perhit = ''
        else:
            perhit = '%.3f' % (perhit * 1e3)
        item.setData(COL_PERHIT, Qt.DisplayRole, perhit)
        item.setTextAlignment(COL_PERHIT, Qt.AlignCenter)

        if hits is None:
            hits = ''
        else:
            hits = '%d' % hits
        item.setData(COL_HITS, Qt.DisplayRole, hits)
        item.setTextAlignment(COL_HITS, Qt.AlignCenter)

    def populate_tree(self):
        """Create each item (and associated data) in the tree"""
        if not self.stats:
            warn_item = TreeWidgetItem(self)
            warn_item.setData(
                0, Qt.DisplayRole,
                _('No timings to display. '
                  'Did you forget to add @profile decorators ?')
                .format(url=WEBSITE_URL))
            warn_item.setFirstColumnSpanned(True)
            warn_item.setTextAlignment(0, Qt.AlignCenter)
            font = warn_item.font(0)
            font.setStyle(QFont.StyleItalic)
            warn_item.setFont(0, font)
            return

        try:
            monospace_font = self.window().editor.get_plugin_font()
        except AttributeError:  # If run standalone for testing
            monospace_font = QFont("Courier New")
            monospace_font.setPointSize(10)

        for func_index, stat_item in enumerate(self.stats.items()):
            # Function name and position
            func_info, func_data = stat_item
            filename, start_line_no, func_name = func_info
            func_stats, func_total_time = func_data
            func_item = TreeWidgetItem(self)
            func_item.setData(
                0, Qt.DisplayRole,
                _('{func_name} ({time_ms:.3f}ms) in file "{filename}", '
                  'line {line_no}').format(
                    filename=filename,
                    line_no=start_line_no,
                    func_name=func_name,
                    time_ms=func_total_time * 1e3))
            func_item.setFirstColumnSpanned(True)
            func_item.setData(COL_POS, Qt.UserRole,
                              (osp.normpath(filename), start_line_no))

            # For sorting by time
            func_item.setData(COL_TIME, Qt.DisplayRole, func_total_time * 1e3)
            func_item.setData(COL_PERCENT, Qt.DisplayRole,
                              func_total_time * 1e3)

            if self.parent().use_colors:
                color_index = func_index % len(COLOR_CYCLE)
            else:
                color_index = 0
            func_color = COLOR_CYCLE[color_index]

            # Lines of code
            for line_info in func_stats:
                line_item = TreeWidgetItem(func_item)
                (line_no, code_line, line_total_time, time_per_hit,
                 hits, percent) = line_info
                self.fill_item(
                    line_item, filename, line_no, code_line,
                    line_total_time, percent, time_per_hit, hits)

                # Color background
                if line_total_time is not None:
                    alpha = percent
                    color = QColor(func_color)
                    color.setAlphaF(alpha)  # Returns None
                    color = QBrush(color)
                    for col in range(self.columnCount()):
                        line_item.setBackground(col, color)
                else:

                    for col in range(self.columnCount()):
                        line_item.setForeground(col, CODE_NOT_RUN_COLOR)

                # Monospace font for code
                line_item.setFont(COL_LINE, monospace_font)

    def on_item_clicked(self, item):
        data = item.data(COL_POS, Qt.UserRole)
        if data is None or len(data) < 2:
            return
        filename, line_no = data
        self.sig_edit_goto_requested.emit(filename, line_no, '')


# =============================================================================
# Tests
# =============================================================================

profile = lambda x: x  # dummy profile wrapper to make script load externally


@profile
def primes(n):
    """
    Simple test function
    Taken from http://www.huyng.com/posts/python-performance-analysis/
    """
    if n==2:
        return [2]
    elif n<2:
        return []
    s=list(range(3,n+1,2))
    mroot = n ** 0.5
    half=(n+1)//2-1
    i=0
    m=3
    while m <= mroot:
        if s[i]:
            j=(m*m-3)//2
            s[j]=0
            while j<half:
                s[j]=0
                j+=m
        i=i+1
        m=2*i+3
    return [2]+[x for x in s if x]


def test():
    """Run widget test"""
    from spyder.utils.qthelpers import qapplication
    import inspect
    import tempfile
    import sys
    from unittest.mock import MagicMock

    primes_sc = inspect.getsource(primes)
    fd, script = tempfile.mkstemp(suffix='.py')
    with os.fdopen(fd, 'w') as f:
        f.write("# -*- coding: utf-8 -*-" + "\n\n")
        f.write(primes_sc + "\n\n")
        f.write("primes(100000)")

    plugin_mock = MagicMock()
    plugin_mock.CONF_SECTION = 'profiler'

    app = qapplication(test_time=5)
    widget = SpyderLineProfilerWidget('test', plugin=plugin_mock)
    widget._setup()
    widget.setup()
    widget.resize(800, 600)
    widget.show()
    widget.analyze(script)
    sys.exit(app.exec_())


if __name__ == '__main__':
    test()
