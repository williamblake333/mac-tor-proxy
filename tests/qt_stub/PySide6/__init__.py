"""Minimal stand-in for PySide6 used by the test suite.

It lets tests import mac_tor_proxy and drive the GUI logic on machines without
Qt (including Linux CI). It is never installed and never used by the real app.
"""
__version__ = "stub"
