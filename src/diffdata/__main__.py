"""Lets `python -m diffdata` work the same as the `diffdata` command."""

from diffdata.cli import main

raise SystemExit(main())
