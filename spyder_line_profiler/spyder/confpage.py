# -*- coding: utf-8 -*-
#
# -----------------------------------------------------------------------------
# Copyright (c) 2013- Spyder Project Contributors
#
# Released under the terms of the MIT License
# (see LICENSE.txt in the project root directory for details)
# -----------------------------------------------------------------------------

"""
Spyder Line Profiler 5 Preferences Page.
"""

# Third party imports
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QGroupBox, QLabel, QVBoxLayout
from spyder.api.preferences import PluginConfigPage
from spyder.api.translations import get_translation

# Local imports
from .widgets import SpyderLineProfilerWidget

# Localization
_ = get_translation("spyder_line_profiler.spyder")


class SpyderLineProfilerConfigPage(PluginConfigPage):

    # --- PluginConfigPage API
    # ------------------------------------------------------------------------

    def setup_page(self):
        settings_group = QGroupBox(_("Settings"))
        use_color_box = self.create_checkbox(
            _("Use deterministic colors to differentiate functions"),
            'use_colors', default=True)
        # Ajout SmartOS : profiler TOUTES les fonctions des modules utilisateur du projet,
        # sans marquage (option, cf. profile_targets.config_lanceur). Cochee = run plus lent
        # (line-profiler ajoute un surcout par ligne sur chaque fonction).
        profile_all_box = self.create_checkbox(
            _("Profiler TOUTES les fonctions des modules utilisateur du projet "
              "(sans marquage) - ralentit le run"),
            'profile_all_user', default=False)

        results_group = QGroupBox(_("Results"))
        results_label1 = QLabel(_("Line profiler plugin results "
                                  "(the output of kernprof.py)\n"
                                  "are stored here:"))
        results_label1.setWordWrap(True)

        # Warning: do not try to regroup the following QLabel contents with
        # widgets above -- this string was isolated here in a single QLabel
        # on purpose: to fix Issue 863 of Profiler plugin
        results_label2 = QLabel(SpyderLineProfilerWidget.DATAPATH)

        results_label2.setTextInteractionFlags(Qt.TextSelectableByMouse)
        results_label2.setWordWrap(True)

        settings_layout = QVBoxLayout()
        settings_layout.addWidget(use_color_box)
        settings_layout.addWidget(profile_all_box)  # SmartOS
        settings_group.setLayout(settings_layout)

        results_layout = QVBoxLayout()
        results_layout.addWidget(results_label1)
        results_layout.addWidget(results_label2)
        results_group.setLayout(results_layout)

        vlayout = QVBoxLayout()
        vlayout.addWidget(settings_group)
        vlayout.addWidget(results_group)
        vlayout.addStretch(1)
        self.setLayout(vlayout)