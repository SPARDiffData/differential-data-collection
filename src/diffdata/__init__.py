"""diffdata: finds, collects, scrubs and stores the working data behind AI safety research."""

from importlib.metadata import version

# The version lives in pyproject.toml only; this reads it from the installed package.
__version__ = version("diffdata")
