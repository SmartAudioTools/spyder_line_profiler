# -*- coding: utf-8 -*-
#
# -----------------------------------------------------------------------------
# Copyright (c) 2013- Spyder Project Contributors
#
# Released under the terms of the MIT License
# (see LICENSE.txt in the project root directory for details)
# -----------------------------------------------------------------------------

"""
Spyder Line Profiler Plugin.
"""

# Standard library imports
import time

# Third-party imports
import qtawesome as qta
from qtpy.QtCore import Qt, Signal

# Spyder imports
from spyder.api.plugins import Plugins, SpyderDockablePlugin
from spyder.api.translations import get_translation
from spyder.api.plugin_registration.decorators import (
    on_plugin_available, on_plugin_teardown)
from spyder.plugins.mainmenu.api import ApplicationMenus, RunMenuSections
from spyder.plugins.run.api import RunContext, RunExecutor, run_execute
from spyder.plugins.toolbar.api import ApplicationToolbars
from spyder.utils.icon_manager import ima

# Local imports
from spyder_line_profiler.spyder.config import (
    CONF_SECTION, CONF_DEFAULTS, CONF_VERSION)
from spyder_line_profiler.spyder.confpage import SpyderLineProfilerConfigPage
from spyder_line_profiler.spyder.widgets import (
    SpyderLineProfilerWidget, is_lineprofiler_installed)
from spyder_line_profiler.spyder.run_conf import LineProfilerConfigurationGroup

# Localization
_ = get_translation("spyder_line_profiler.spyder")


class SpyderLineProfiler(SpyderDockablePlugin, RunExecutor):
    """
    Spyder Line Profiler plugin for Spyder 5.
    """

    NAME = "spyder_line_profiler"
    REQUIRES = [Plugins.Preferences, Plugins.Editor, Plugins.Run]
    # SmartOS : profilage combine (point 2) ; menu Executer et barre d'outils du haut pour le
    # mode « Σ ».
    OPTIONAL = [Plugins.Profiler, Plugins.MainMenu, Plugins.Toolbar]
    TABIFY = [Plugins.Help]
    WIDGET_CLASS = SpyderLineProfilerWidget
    CONF_SECTION = CONF_SECTION
    CONF_DEFAULTS = CONF_DEFAULTS
    CONF_VERSION = CONF_VERSION
    CONF_WIDGET_CLASS = SpyderLineProfilerConfigPage
    CONF_FILE = True

    # --- Signals
    sig_finished = Signal()
    """This signal is emitted to inform the profile profiling has finished."""

    # --- SpyderDockablePlugin API
    # ------------------------------------------------------------------------
    @staticmethod
    def get_name():
        return _("Line Profiler")
    
    @staticmethod
    def get_description():
        return _("Line profiler display for Spyder")
    
    @classmethod
    def get_icon(cls):
        return qta.icon('mdi.speedometer', color=ima.MAIN_FG_COLOR)

    def on_initialize(self):
        self.widget = self.get_widget()
        self._smartos_mode_suivant = None
        self._smartos_action_operations = None
        self.widget.sig_finished.connect(self.sig_finished)
        self.widget.sig_compter_requested.connect(self._smartos_compter)

        self.executor_configuration = [
            {
                'input_extension': 'py',
                'context': {
                    'name': 'File'
                },
                'output_formats': [],
                'configuration_widget': LineProfilerConfigurationGroup,
                'requires_cwd': True,
                'priority': 7
            }
        ]

    @on_plugin_available(plugin=Plugins.Run)
    def on_run_available(self):
        run = self.get_plugin(Plugins.Run)
        run.register_executor_configuration(self, self.executor_configuration)

        if is_lineprofiler_installed():
            run.create_run_in_executor_button(
                RunContext.File,
                self.NAME,
                text=_('Run line profiler'),
                tip=_('Run line profiler'),
                icon=self.get_icon(),
                shortcut_context='spyder_line_profiler',
                register_shortcut=True,
                add_to_menu={
                    "menu": ApplicationMenus.Run,
                    "section": RunMenuSections.RunInExecutors
                },
                shortcut_widget_context=Qt.ApplicationShortcut,
            )
            self._smartos_creer_action_operations()

    @on_plugin_available(plugin=Plugins.MainMenu)
    def on_main_menu_available(self):
        action = self._smartos_creer_action_operations()
        if action is not None:
            self.get_plugin(Plugins.MainMenu).add_item_to_application_menu(
                action, ApplicationMenus.Run, RunMenuSections.RunInExecutors)

    @on_plugin_teardown(plugin=Plugins.MainMenu)
    def on_main_menu_teardown(self):
        if self._smartos_action_operations is not None:
            self.get_plugin(Plugins.MainMenu).remove_item_from_application_menu(
                self.ACTION_OPERATIONS, menu_id=ApplicationMenus.Run)

    # Le Σ a cote de « Profiler le fichier », dans la barre Profile du HAUT (demande de
    # l'utilisateur, 30/10/2026). La barre est creee par le greffon Profiler : si elle n'existe
    # pas encore, le Toolbar met l'action en file et l'y pose a sa creation.
    @on_plugin_available(plugin=Plugins.Toolbar)
    def on_toolbar_available(self):
        action = self._smartos_creer_action_operations()
        if action is not None:
            self.get_plugin(Plugins.Toolbar).add_item_to_application_toolbar(
                action, toolbar_id=ApplicationToolbars.Profile)

    @on_plugin_teardown(plugin=Plugins.Toolbar)
    def on_toolbar_teardown(self):
        if self._smartos_action_operations is not None:
            self.get_plugin(Plugins.Toolbar).remove_item_from_application_toolbar(
                self.ACTION_OPERATIONS, toolbar_id=ApplicationToolbars.Profile)

    @on_plugin_available(plugin=Plugins.Editor)
    def on_editor_available(self):
        widget = self.get_widget()
        editor = self.get_plugin(Plugins.Editor)
        widget.sig_edit_goto_requested.connect(editor.load)

        # Marqueurs "profiler cette fonction" dans la marge (ajout SmartOS, cf.
        # Commun/scripts/patch_spyder_line_profiler_targets.py). sig_codeeditor_created /
        # _deleted sont l'API publique par laquelle le debogueur de Spyder installe sa propre
        # marge de points d'arret : rien a patcher dans le coeur de Spyder.
        editor.sig_codeeditor_created.connect(self._smartos_add_codeeditor)
        editor.sig_codeeditor_deleted.connect(self._smartos_remove_codeeditor)
        # Purge des marqueurs des fichiers non reouverts, une fois la session RESTAUREE
        # (sig_open_files_finished) : "un marqueur = un fichier ouvert".
        editor.sig_open_files_finished.connect(self._smartos_purge_fichiers_fermes)

    @on_plugin_available(plugin=Plugins.Preferences)
    def on_preferences_available(self):
        preferences = self.get_plugin(Plugins.Preferences)
        preferences.register_plugin_preferences(self)

    @on_plugin_teardown(plugin=Plugins.Run)
    def on_run_teardown(self):
        run = self.get_plugin(Plugins.Run)
        run.deregister_executor_configuration(
            self, self.executor_configuration)
        run.destroy_run_in_executor_button(
            RunContext.File, self.NAME)

    @on_plugin_teardown(plugin=Plugins.Preferences)
    def on_preferences_teardown(self):
        preferences = self.get_plugin(Plugins.Preferences)
        preferences.deregister_plugin_preferences(self)

    @on_plugin_teardown(plugin=Plugins.Editor)
    def on_editor_teardown(self):
        widget = self.get_widget()
        editor = self.get_plugin(Plugins.Editor)
        widget.sig_edit_goto_requested.disconnect(editor.load)

        # Ajout SmartOS (marqueurs de profilage dans la marge).
        editor.sig_codeeditor_created.disconnect(self._smartos_add_codeeditor)
        editor.sig_codeeditor_deleted.disconnect(self._smartos_remove_codeeditor)
        editor.sig_open_files_finished.disconnect(self._smartos_purge_fichiers_fermes)

    def check_compatibility(self):
        valid = True
        message = ""  # Note: Remember to use _("") to localize the string
        return valid, message

    def on_close(self, cancellable=True):
        return True

    # --- Public API
    # ------------------------------------------------------------------------

    @run_execute(context=RunContext.File)
    def run_file(self, input, conf):
        # SmartOS : on ne ROUVRE pas un panneau que l'utilisateur a ferme (« il se reouvre a
        # chaque fois », 30/10/2026) - les resultats sont dans la marge de l'editeur. Ouvert, il
        # passe au premier plan comme avant.
        if self.get_widget().toggle_view_action.isChecked():
            self.switch_to_plugin()

        exec_params = conf['params']
        cwd_opts = exec_params['working_dir']
        params = exec_params['executor_params']

        run_input = input['run_input']
        filename = run_input['path']

        wdir = cwd_opts['path']
        args = params['args']

        # Mode « Σ » : demande posee par l'action, consommee ici (cf. _smartos_compter).
        mode = 'temps'
        demande, self._smartos_mode_suivant = self._smartos_mode_suivant, None
        if demande is not None and time.monotonic() - demande < 2.0:
            mode = 'operations'
        from spyder_line_profiler.spyder.profile_results import noter_lancement
        noter_lancement(mode)
        self.get_widget().analyze(filename, wdir=wdir, args=args, mode=mode)
    # <<< SmartOS line profiler : debut du bloc injecte >>>
    # Marges du line profiler (marqueurs a gauche, temps a droite). Cf.
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

    # Mode « Σ » (demande du 30/10/2026) : compter les operations elementaires au lieu de
    # chronometrer. Un second bouton, et non une option du premier : les deux mesures se lancent
    # separement (le comptage ralentit le programme de x4 a x9, cf. cx_launcher). Le Run de
    # Spyder n'a qu'un bouton par (contexte, executeur) : celui-ci passe donc par run_file du
    # conteneur du Run, avec la meme configuration d'execution (dossier, arguments), et pose un
    # drapeau date que run_file ci-dessus consomme.
    ACTION_OPERATIONS = 'compter les operations elementaires'

    def _smartos_creer_action_operations(self):
        if not is_lineprofiler_installed():
            return None
        if self._smartos_action_operations is None:
            self._smartos_action_operations = self.create_action(
                self.ACTION_OPERATIONS,
                text=_('Compter les opérations élémentaires'),
                tip=_('Compter les opérations élémentaires des fonctions marquées'),
                icon=qta.icon('mdi.sigma', color='#ff00ff'),
                triggered=self._smartos_compter,
                register_shortcut=True,
                shortcut_context=self.NAME,
                context=Qt.ApplicationShortcut,
            )
        return self._smartos_action_operations

    def _smartos_boutons_visibles(self):
        """(horloge, Σ) : le bouton « Profiler le fichier » et le Σ sont-ils visibles dans la
        barre Profile ? Les masques un par un (Fenetre > Boutons des barres d'outils) se lisent
        TOUJOURS dans la configuration, ecrite a chaque bascule : la barre, elle, les reprend a
        chaque rendu, et entre deux rendus ses actions disaient l'horloge visible (« j'ai toujours
        l'horloge au demarrage », l'utilisateur, 31/10/2026). Barre affichee : son etat vivant ;
        pas encore affichee (demarrage) : la configuration."""
        from spyder_line_profiler.spyder.profile_results import (
            boutons_visibles_conf, boutons_masques_conf)
        toolbar = self.get_plugin(Plugins.Toolbar, error=False)
        try:
            barre = toolbar.get_application_toolbar(ApplicationToolbars.Profile)
        except Exception:
            barre = None
        if barre is None or not barre.isVisible():
            return boutons_visibles_conf()
        return boutons_masques_conf()

    def _smartos_compter(self):
        self._smartos_mode_suivant = time.monotonic()
        run = self.get_plugin(Plugins.Run)
        run.get_container().run_file(selected_executor=self.NAME)
    # <<< SmartOS line profiler : fin du bloc injecte >>>
