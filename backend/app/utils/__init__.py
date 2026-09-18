"""Pure helpers: file naming, input validation, path containment.

Deliberately empty. `file_serving` imports FastAPI, and re-exporting it here
would drag the web framework into `file_naming` and `validators` — which are
plain-Python by design so they can be unit-tested without the full dependency
set installed.
"""
