"""Snappix Viewer — high-performance local image library viewer.

Two sibling subpackages live under ``src/snappix``: ``common`` (shared
infrastructure: paths / i18n / UI design system / terms) and ``viewer`` (the
GUI application).  The AI tag scanner lives outside this package, inside the
paid AI plugin at ``plugins/snappix_ai/tagger/``, and is built separately.

``__version__`` is READ from the installed distribution's metadata, which is
built from ``pyproject.toml``'s ``project.version`` — the single source of
truth, with no second copy here to leave stale.  In development that metadata
comes from the editable install (``uv run`` reinstalls the project whenever
the version changes); in the frozen build it comes from the minimal
``*.dist-info`` ``build_portable.py`` generates into ``_internal``, which is
the frozen ``sys.path`` (nothing is written at runtime).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

#: Reported when the distribution metadata is absent — a source checkout used
#: without installing the project.  Never in a build: check_dist_complete
#: requires the generated metadata in the shipped tree.
UNKNOWN_VERSION = "0.0.0+unknown"

#: ``PackageNotFoundError`` is only HALF the degraded seam: it is raised when
#: no ``*.dist-info`` is found at all, but a dist-info whose ``METADATA`` is
#: missing or unreadable makes ``importlib.metadata`` swallow the error and
#: return ``None`` instead (``PathDistribution.read_text`` suppresses OSError,
#: so ``metadata["Version"]`` is None).  typeshed declares ``version() -> str``,
#: so neither pyright nor the try/except catches that half; ``or`` does.
#: ``app_version`` is a public plugin-API string (plugin_host/context.py), so a
#: None there would reach third-party plugin code as an AttributeError.
try:
    __version__ = _distribution_version("snappix-viewer") or UNKNOWN_VERSION
except PackageNotFoundError:
    __version__ = UNKNOWN_VERSION
