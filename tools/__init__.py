"""Support modules for the checks this repository ships with.

Not a packaged module: ``pyproject.toml`` lists the ``hermes`` packages explicitly, so
nothing here is installed.  It is a package so that ``tests`` and the standalone tools
can import the shared schema snapshot by the same path.
"""
