# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""Line profiler run executor configurations."""

# Third-party imports
from qtpy.QtWidgets import (
    QGroupBox, QVBoxLayout, QGridLayout, QCheckBox, QLineEdit)

# Local imports
from spyder.api.translations import _
from spyder.plugins.run.api import (
    RunExecutorConfigurationGroup, Context, RunConfigurationMetadata)


class LineProfilerConfigurationGroup(RunExecutorConfigurationGroup):
    """Run configuration options for line profiler."""

    def __init__(self, parent, context: Context, input_extension: str,
                 input_metadata: RunConfigurationMetadata):
        super().__init__(parent, context, input_extension, input_metadata)

        self.dir = None

        # --- General settings ----
        common_group = QGroupBox(_("File settings"))
        common_layout = QGridLayout(common_group)

        self.clo_cb = QCheckBox(_("Command line options:"))
        common_layout.addWidget(self.clo_cb, 0, 0)
        self.clo_edit = QLineEdit(self)
        self.clo_edit.setMinimumWidth(300)
        self.clo_cb.toggled.connect(self.clo_edit.setEnabled)
        self.clo_edit.setEnabled(False)
        common_layout.addWidget(self.clo_edit, 0, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(common_group)
        layout.addStretch(100)

    @staticmethod
    def get_default_configuration() -> dict:
        return {
            'args_enabled': False,
            'args': ''
        }

    def set_configuration(self, config: dict):
        args_enabled = config['args_enabled']
        args = config['args']

        self.clo_cb.setChecked(args_enabled)
        self.clo_edit.setText(args)

    def get_configuration(self) -> dict:
        return {
            'args_enabled': self.clo_cb.isChecked(),
            'args': self.clo_edit.text(),
        }
