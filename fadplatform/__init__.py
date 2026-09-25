"""Rig di test per la piattaforma FAD ForMe.

Tre pezzi:
- ``moodle``: il layer browser/piattaforma (Chrome for Testing, login, corsi);
- ``state``: utenti, corsi e impostazioni, persistiti in JSON;
- ``engine``: l'orchestratore che tiene N corsi in simultanea;
- ``server``: la UI web locale.

Si avvia con ``python -m fadplatform``.
"""

__version__ = "1.0.0"
