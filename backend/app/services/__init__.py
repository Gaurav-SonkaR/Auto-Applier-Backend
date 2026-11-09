"""Business-logic services.

Deliberately empty: every caller imports the concrete submodule it needs
(`from backend.app.services.llm_client import ...`). Re-exporting here would
pull groq, selenium and smtplib into *any* service import and
slow every startup for no benefit.
"""
