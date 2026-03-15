"""
Heartopia Painter — GLouTuny v4.1

Automated canvas painter for Heartopia with full GUI, in-app calibration,
and bucket-fill engine. Run with: python -m heartopia_painter
"""

__version__ = "4.1"

from .gloutuny_painter import launch_gui

__all__ = ["__version__", "launch_gui"]
