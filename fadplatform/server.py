"""UI web locale del rig di test (Flask, una pagina, nessuna build step).

Pannello su http://127.0.0.1:8788: gestione utenti di test (con import CSV a
due colonne), impostazioni (corsi in simultanea, versione Chrome for Testing),
avvio/arresto del motore, progressi per utente e corso, eventi recenti.

La UI e' operativa, non e' un dashboard di benchmark: quelli stanno nel
backoffice interno.
"""

from __future__ import annotations

import csv
import io

from flask import Flask, Response, jsonify, render_template, request

from . import __version__
from .engine import Engine
from .state import RIAPRIBILI, STATUS_LABELS, STATUS_PENDING, STATUS_RUNNING, Store

HEADER_UTENTE = {"username", "user", "utente", "nome", "account", "login", "email"}
HEADER_PASSWORD = {"password", "pass", "pwd", "pw", "psw"}


def parse_users_csv(text: str) -> list[tuple[str, str, str]]:
    """Estrae (username, password, nome) da un CSV a due o tre colonne.

    Accetta virgola, punto e virgola o tabulazione come separatore e salta
    l'eventuale riga d'intestazione. La terza colonna, il nome, e' facoltativa:
    e' un'etichetta per la UI, non un dato per la piattaforma. Le password sono
    di test: nessun controllo di lunghezza o caratteri, come da requisiti.
    """
    righe = [r for r in text.splitlines() if r.strip()]
    if not righe:
        return []
    # Chi dei tre separatori compare piu' spesso nella prima riga (parita':
    # la virgola). "delimiter" di csv.reader vuole un carattere solo.
    separatore = max(",;\t", key=righe[0].count)
    lette = list(csv.reader(io.StringIO(text), delimiter=separatore))
    utenti: list[tuple[str, str, str]] = []
    for i, riga in enumerate(lette):
        if not riga or not any(campo.strip() for campo in riga):
            continue
        col0 = (riga[0] if riga else "").strip()
        col1 = (riga[1] if len(riga) > 1 else "").strip()
        col2 = (riga[2] if len(riga) > 2 else "").strip()
        if i == 0 and col0.lower() in HEADER_UTENTE and col1.lower() in HEADER_PASSWORD:
            continue  # intestazione
        if not col0:
            continue
        utenti.append((col0, col1, col2))
    return utenti


def create_app(store: Store, engine: Engine) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # il CSV non serve enorme

    # ---------------------------------------------------------------- UI

    @app.get("/")
    def index():
        return render_template("index.html", version=__version__)

    # ------------------------------------------------------------- stato

    @app.get("/api/state")
    def api_state():
        quadro = engine.snapshot()
        video = quadro.get("video") or {}
        utenti = []
        for u in store.users():
            corsi = []
            for c in u["courses"]:
                voce = dict(c)
                voce["status_label"] = STATUS_LABELS.get(c["status"], c["status"])
                voce["video"] = (video.get(u["username"]) or {}).get(c["id"])
                corsi.append(voce)
            utenti.append(
                {
                    "username": u["username"],
                    "name": u.get("name") or "",
                    "password": u["password"],
                    "slug": u["slug"],
                    "last_scan": u.get("last_scan"),
                    "login_error": u.get("login_error"),
                    "courses": corsi,
                    "totals": store.user_totals(u),
                    "browser": bool((quadro.get("browsers") or {}).get(u["username"])),
                }
            )
        return jsonify(
            {
                "version": __version__,
                "engine": quadro,
                "settings": store.settings(),
                "users": utenti,
            }
        )

    # ------------------------------------------------------------ utenti

    @app.post("/api/users")
    def api_add_user():
        corpo = request.get_json(silent=True) or {}
        username = (corpo.get("username") or "").strip()
        if not username:
            return jsonify({"error": "username obbligatorio"}), 400
        _, creato = store.upsert_user(
            username, corpo.get("password") or "", (corpo.get("name") or "").strip()
        )
        return jsonify({"created": creato})

    @app.put("/api/users/<username>")
    def api_edit_user(username: str):
        if store.get_user(username) is None:
            return jsonify({"error": "utente inesistente"}), 404
        corpo = request.get_json(silent=True) or {}
        nuovo = (corpo.get("username") or "").strip()
        if not nuovo:
            return jsonify({"error": "username obbligatorio"}), 400
        nome = (corpo.get("name") or "").strip()
        if len(nome) > 200:
            return jsonify({"error": "nome troppo lungo"}), 400
        password = corpo.get("password")
        if password is not None and not isinstance(password, str):
            return jsonify({"error": "password non valida"}), 400
        utente, errore = engine.rename_user(username, nuovo, nome, password)
        if errore:
            return jsonify({"error": errore}), 409
        return jsonify({"updated": True, "username": utente["username"]})

    @app.get("/api/users/export")
    def api_export_users():
        """I utenti in CSV, nello stesso formato che l'import si aspetta:
        username,password,nome (il nome puo' restare vuoto)."""
        buffer = io.StringIO()
        scrittore = csv.writer(buffer)
        scrittore.writerow(["username", "password", "name"])
        for u in store.users():
            scrittore.writerow([u["username"], u["password"], u.get("name") or ""])
        risposta = Response(buffer.getvalue(), mimetype="text/csv; charset=utf-8")
        risposta.headers["Content-Disposition"] = 'attachment; filename="utenti-fad.csv"'
        return risposta

    @app.delete("/api/users/<username>")
    def api_remove_user(username: str):
        if store.get_user(username) is None:
            return jsonify({"error": "utente inesistente"}), 404
        rimosso = engine.drop_user(username)
        return jsonify({"removed": rimosso})

    @app.delete("/api/users")
    def api_clear_users():
        quanti = engine.clear_users()
        return jsonify({"removed": quanti})

    @app.post("/api/users/import")
    def api_import_csv():
        testo = ""
        if "file" in request.files:
            file = request.files["file"]
            testo = (file.read() or b"").decode("utf-8-sig", "replace")
        elif request.is_json:
            testo = (request.get_json(silent=True) or {}).get("text") or ""
        else:
            testo = request.get_data(as_text=True)
        utenti = parse_users_csv(testo)
        if not utenti:
            return jsonify({"error": "nessuna riga valida (servono due colonne: username,password)"}), 400
        aggiunti = aggiornati = 0
        for username, password, name in utenti:
            _, creato = store.upsert_user(username, password, name)
            aggiunti += int(creato)
            aggiornati += int(not creato)
        return jsonify({"added": aggiunti, "updated": aggiornati, "total": len(utenti)})

    @app.post("/api/users/<username>/rescan")
    def api_rescan_user(username: str):
        utente = store.get_user(username)
        if utente is None:
            return jsonify({"error": "utente inesistente"}), 404
        if any(c["status"] == STATUS_RUNNING for c in utente["courses"]):
            return jsonify({"error": "l'utente ha corsi in riproduzione: fermali prima di riscansionare"}), 409
        engine.rescan_user(username)
        return jsonify({"queued": True})

    @app.post("/api/users/<username>/retry")
    def api_retry_user(username: str):
        if store.get_user(username) is None:
            return jsonify({"error": "utente inesistente"}), 404
        engine.retry_login(username)
        return jsonify({"queued": True})

    # ------------------------------------------------------------- corsi

    @app.post("/api/users/<username>/courses/<course_id>/reopen")
    def api_reopen_course(username: str, course_id: str):
        corso = store.get_course(username, course_id)
        if corso is None:
            return jsonify({"error": "corso inesistente"}), 404
        if corso["status"] == STATUS_RUNNING:
            return jsonify({"error": "il corso e' in riproduzione adesso"}), 409
        if corso["status"] not in RIAPRIBILI:
            return jsonify({"error": "il corso e' gia' in coda"}), 409
        store.set_course_status(username, course_id, STATUS_PENDING)
        return jsonify({"queued": True})

    # ------------------------------------------------------- impostazioni

    @app.post("/api/settings")
    def api_settings():
        corpo = request.get_json(silent=True) or {}
        cambi = {}
        if "max_simultaneous" in corpo:
            try:
                valore = int(corpo["max_simultaneous"])
            except (TypeError, ValueError):
                return jsonify({"error": "max_simultaneous deve essere un numero"}), 400
            if not 0 <= valore <= 500:
                return jsonify({"error": "max_simultaneous fuori intervallo (0-500)"}), 400
            cambi["max_simultaneous"] = valore
        if "pinned_chrome" in corpo:
            cambi["pinned_chrome"] = str(corpo["pinned_chrome"] or "").strip()
        if not cambi:
            return jsonify({"error": "niente da salvare"}), 400
        impostazioni = store.update_settings(cambi)
        engine.broadcast_settings(impostazioni)
        return jsonify({"settings": impostazioni})

    # ------------------------------------------------------------- motore

    @app.post("/api/engine/start")
    def api_engine_start():
        if not store.users():
            return jsonify({"error": "nessun utente configurato"}), 409
        avviato = engine.start()
        return jsonify({"started": avviato})

    @app.post("/api/engine/stop")
    def api_engine_stop():
        engine.stop()
        return jsonify({"stopping": True})

    return app
