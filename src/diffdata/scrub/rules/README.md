# Secret rules

`gitleaks.toml` is gitleaks' default rule file, copied unchanged from release v8.30.1:
https://github.com/gitleaks/gitleaks/blob/v8.30.1/config/gitleaks.toml

It's MIT-licensed; the licence is in `gitleaks-LICENSE`. `diffdata/scrub/secrets.py` reads it
with Python and checks its SHA-256 against the pinned value, so the scrub stops if the file
is missing, changed or broken. To move to a newer release, replace the file, update
`RULES_RELEASE` and `RULES_SHA256` in `secrets.py`, and run the tests: every rule must still
compile and pass the slow-rule test.
