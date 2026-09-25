"""Stato persistente del rig: utenti di test, loro corsi, impostazioni.

Tutto vive in ~/.courseopener/state.json (permessi 600): chiuso il software e
riaperto, utenti, progressi e coda riprendono da dove erano. Contiene anche le
password di test: e' voluto, sono credenziali di collaudo.

Lo stato e' un dict plain (niente classi modello): viene salvato e ricaricato
com'e', e chi lo usa lo fa sempre sotto lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .moodle import DEFAULT_PINNED_CFT

DEFAULT_STATE_PATH = Path.home() / ".courseopener" / "state.json"

# Stati di un corso. "pending" = in coda (il primo incompleto e' un video),
# "running" = ha una finestra aperta adesso.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_AI_TEST = "ai_test"
STATUS_ERROR = "error"
STATUS_NO_ACTIVITY = "no_activity"

STATUS_LABELS = {
    STATUS_PENDING: "in coda",
    STATUS_RUNNING: "in riproduzione",
    STATUS_COMPLETED: "completato",
    STATUS_AI_TEST: "fermo ai test",
    STATUS_ERROR: "errore",
    STATUS_NO_ACTIVITY: "senza attività",
}

# Gli stati da cui un corso puo' tornare in coda a mano dalla UI.
RIAPRIBILI = (STATUS_COMPLETED, STATUS_AI_TEST, STATUS_ERROR, STATUS_NO_ACTIVITY)

DEFAULT_SETTINGS: dict = {
    "max_simultaneous": 8,
    "pinned_chrome": DEFAULT_PINNED_CFT,
    # Soglie del listener anti-blocco, in secondi. Tante finestre = cicli di
    # polling piu' lenti: le grate sono larghe apposta.
    "paused_grace_s": 45,  # 1) il video e' in pausa da un po' -> refresh
    "ended_grace_s": 45,  # 3) finito ma non avanzato -> refresh
    "stall_grace_s": 150,  # 2) in riproduzione ma fermo -> refresh
    "missing_grace_s": 120,  # il player non si e' proprio visto -> refresh
    "max_refresh_streak": 6,  # refresh di fila senza progressi -> si valuta la sidebar
}


def profile_slug(username: str) -> str:
    """Nome della cartella profilo di un utente, stabile e senza collisioni.

    L'hash finale evita due problemi: nomi diversi che collassano sullo stesso
    slug (MarcoRossi vs marco.rossi) e prefissi che si contengono a vicenda,
    che farebbero male il pkill-by-profilo di close_dedicated_chrome.
    """
    base = re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-") or "user"
    digest = hashlib.sha1(username.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"


def new_state() -> dict:
    return {"settings": dict(DEFAULT_SETTINGS), "users": [], "categories": []}


class Store:
    """Accesso thread-safe allo stato, con salvataggio atomico su disco."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    # ------------------------------------------------------------- disco

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Le impostazioni nuove (aggiunte in una versione successiva) entrano
        # coi loro default senza buttare via quelle scritte dall'utente.
        settings = dict(DEFAULT_SETTINGS)
        settings.update(data.get("settings") or {})
        users = [u for u in (data.get("users") or []) if isinstance(u, dict) and u.get("username")]
        categorie = [c for c in (data.get("categories") or []) if isinstance(c, str) and c.strip()]
        return {"settings": settings, "users": users, "categories": categorie}

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            testo = json.dumps(self.data, ensure_ascii=False, indent=2)
            tmp.write_text(testo, encoding="utf-8")
            os.chmod(tmp, 0o600)  # contiene le password di test
            tmp.replace(self.path)

    # -------------------------------------------------------- impostazioni

    def settings(self) -> dict:
        with self.lock:
            return dict(self.data["settings"])

    def update_settings(self, changes: dict) -> dict:
        with self.lock:
            for chiave in DEFAULT_SETTINGS:
                if chiave in changes and changes[chiave] is not None:
                    self.data["settings"][chiave] = changes[chiave]
            self.save()
            return dict(self.data["settings"])

    # -------------------------------------------------------------- utenti

    def users(self) -> list[dict]:
        with self.lock:
            return [dict(u) for u in self.data["users"]]

    def get_user(self, username: str) -> dict | None:
        with self.lock:
            for u in self.data["users"]:
                if u["username"] == username:
                    return u
            return None

    def upsert_user(
        self, username: str, password: str, name: str | None = None, category: str | None = None
    ) -> tuple[dict, bool]:
        """Aggiunge l'utente o, se esiste, gli aggiorna i dati di collaudo.

        Nessun controllo su lunghezza o caratteri: sono credenziali di test.
        Ritorna (utente, creato_adesso). ``name`` e ``category`` a None
        significano "non toccare"; la categoria assegnata viene registrata
        nell'elenco delle categorie conosciute.
        """
        username = (username or "").strip()
        password = password or ""
        with self.lock:
            for u in self.data["users"]:
                if u["username"] == username:
                    if u.get("password") != password:
                        u["password"] = password
                        u["login_error"] = None
                    if name is not None:
                        u["name"] = name
                    if category is not None:
                        self._register_category_locked(category)
                        u["category"] = category
                    self.save()
                    return u, False
            u = {
                "username": username,
                "password": password,
                "name": name or "",
                "category": category or "",
                "slug": profile_slug(username),
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_scan": None,
                "login_error": None,
                "courses": [],
            }
            self.data["users"].append(u)
            if category:
                self._register_category_locked(category)
            self.save()
            return u, True

    def update_user(
        self,
        username: str,
        new_username: str | None = None,
        name: str | None = None,
        password: str | None = None,
    ) -> dict | None:
        """Modifica un utente esistente; None se non c'e'.

        Cambiare username cambia anche lo slug (quindi il profilo browser che
        lo usa): la coerenza la garantisce chi chiama, non lo store.
        ``name``/``password`` a None significano "non toccare".
        """
        new_username = (new_username or "").strip() or None
        with self.lock:
            for u in self.data["users"]:
                if u["username"] != username:
                    continue
                if new_username and new_username != username:
                    u["username"] = new_username
                    u["slug"] = profile_slug(new_username)
                if name is not None:
                    u["name"] = name
                if password is not None and u.get("password") != password:
                    u["password"] = password
                    u["login_error"] = None
                self.save()
                return u
            return None

    # ----------------------------------------------------------- categorie

    def _register_category_locked(self, name: str) -> None:
        """Registra una categoria nell'elenco (chiamare sotto lock)."""
        name = (name or "").strip()
        if name and name not in self.data["categories"]:
            self.data["categories"].append(name)

    def categories(self) -> list[str]:
        with self.lock:
            return list(self.data["categories"])

    def add_category(self, name: str) -> bool:
        """Crea una categoria; False se il nome e' vuoto o gia' presente."""
        name = (name or "").strip()
        with self.lock:
            if not name or name in self.data["categories"]:
                return False
            self.data["categories"].append(name)
            self.save()
            return True

    def remove_category(self, name: str) -> int:
        """Elimina la categoria e la toglie agli utenti; ritorna i toccati."""
        with self.lock:
            if name not in self.data["categories"]:
                return -1
            self.data["categories"].remove(name)
            tocatti = 0
            for u in self.data["users"]:
                if u.get("category") == name:
                    u["category"] = ""
                    tocatti += 1
            self.save()
            return tocatti

    def set_user_category(self, username: str, category: str) -> bool:
        """Assegna (o con "" toglie) la categoria a un utente."""
        category = (category or "").strip()
        with self.lock:
            for u in self.data["users"]:
                if u["username"] == username:
                    self._register_category_locked(category)
                    u["category"] = category
                    self.save()
                    return True
            return False

    def remove_user(self, username: str) -> bool:
        with self.lock:
            prima = len(self.data["users"])
            self.data["users"] = [u for u in self.data["users"] if u["username"] != username]
            if len(self.data["users"]) != prima:
                self.save()
                return True
            return False

    def clear_users(self) -> int:
        """Svuota l'elenco utenti — e con loro i corsi scoperti.

        Le impostazioni restano. Ritorna quanti utenti ha rimosso.
        """
        with self.lock:
            quanti = len(self.data["users"])
            self.data["users"] = []
            self.save()
            return quanti

    def set_login_error(self, username: str, message: str | None) -> None:
        with self.lock:
            u = self.get_user(username)
            if u is not None:
                u["login_error"] = message
                self.save()

    def set_scanned(self, username: str, quando: str | None = None) -> None:
        with self.lock:
            u = self.get_user(username)
            if u is not None:
                u["last_scan"] = quando or time.strftime("%Y-%m-%d %H:%M:%S")
                self.save()

    # ---------------------------------------------------------------- corsi

    def _find_course(self, user: dict, course_id: str) -> dict | None:
        for c in user["courses"]:
            if c["id"] == course_id:
                return c
        return None

    def get_course(self, username: str, course_id: str) -> dict | None:
        with self.lock:
            u = self.get_user(username)
            if u is None:
                return None
            c = self._find_course(u, course_id)
            return dict(c) if c else None

    def merge_course(
        self,
        username: str,
        course: dict,
        status: str | None = None,
        progress: int | None = None,
    ) -> None:
        """Risultato di una scansione: aggiorna titolo/url/progresso, mantiene
        lo stato quando il corso c'e' gia' (a meno che non sia richiesto)."""
        with self.lock:
            u = self.get_user(username)
            if u is None:
                return
            c = self._find_course(u, course["id"])
            if c is None:
                u["courses"].append(
                    {
                        "id": course["id"],
                        "title": course["title"],
                        "url": course["url"],
                        "status": status or STATUS_PENDING,
                        "progress": progress or 0,
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            else:
                c["title"] = course["title"]
                c["url"] = course["url"]
                if progress is not None:
                    c["progress"] = progress
                if status is not None:
                    c["status"] = status
                c["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save()

    def set_course_status(
        self, username: str, course_id: str, status: str, progress: int | None = None
    ) -> bool:
        """True se il corso esisteva ed e' stato aggiornato."""
        with self.lock:
            u = self.get_user(username)
            if u is None:
                return False
            c = self._find_course(u, course_id)
            if c is None:
                return False
            c["status"] = status
            if progress is not None:
                c["progress"] = progress
            c["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save()
            return True

    def set_course_progress(self, username: str, course_id: str, progress: int) -> None:
        with self.lock:
            u = self.get_user(username)
            if u is None:
                return
            c = self._find_course(u, course_id)
            if c is None or c["progress"] == progress:
                return
            c["progress"] = progress
            c["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save()

    def drop_missing_courses(self, username: str, vivi: set[str]) -> None:
        """Dopo una scansione, butta i corsi che la piattaforma non elenca piu'."""
        with self.lock:
            u = self.get_user(username)
            if u is None:
                return
            prima = len(u["courses"])
            u["courses"] = [c for c in u["courses"] if c["id"] in vivi or c["status"] == STATUS_RUNNING]
            if len(u["courses"]) != prima:
                self.save()

    # ----------------------------------------------------------- aggregati

    def user_totals(self, user: dict) -> dict:
        corsi = user.get("courses") or []
        totale = contati = 0
        per_stato: dict[str, int] = {}
        somma = 0
        for c in corsi:
            per_stato[c["status"]] = per_stato.get(c["status"], 0) + 1
            totale += 1
            if c["status"] != STATUS_NO_ACTIVITY:
                contati += 1
                somma += c.get("progress") or 0
        completati = per_stato.get(STATUS_COMPLETED, 0)
        fermi = per_stato.get(STATUS_AI_TEST, 0)
        return {
            "total": totale,
            "counted": contati,
            # Quanto manca all'obiettivo: i corsi su cui il rig puo' ancora
            # lavorare (in coda, in riproduzione, in errore). Quelli fermi ai
            # test da qui non passano mai - i quiz non si fanno - quindi non
            # contano come lavoro rimasto, ne' quelli senza attivita'.
            "to_test": totale - completati - fermi - per_stato.get(STATUS_NO_ACTIVITY, 0),
            "completed": completati,
            "ai_test": per_stato.get(STATUS_AI_TEST, 0),
            "pending": per_stato.get(STATUS_PENDING, 0),
            "running": per_stato.get(STATUS_RUNNING, 0),
            "error": per_stato.get(STATUS_ERROR, 0),
            "no_activity": per_stato.get(STATUS_NO_ACTIVITY, 0),
            "avg_progress": int(somma / contati) if contati else 0,
        }
