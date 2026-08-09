# Testing

After the README test install, run `.venv/bin/python -m pytest -q`. Deterministic tests cover validation and transition guards; duplicate registration/send/callback; accepted, rejected, returned and executed flows; SQLite reopen; timeout recording; envelope tampering; ambiguous and confident reconciliation; repeated statement protection; and complete audit ordering.

No test opens a network connection. BSL receives a static sanity check at portfolio packaging time and is not runtime-tested without 1C.
