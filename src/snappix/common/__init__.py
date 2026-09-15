"""Shared infrastructure layer for the snappix tools.

Keep this package free of viewer-only concerns: anything here must be
importable without pulling in viewer widgets (Qt imports are allowed only
in the ``ui`` subpackage and the dialog modules that declare them).
"""
