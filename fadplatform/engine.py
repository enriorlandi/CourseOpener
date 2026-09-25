"""L'orchestratore del rig di test.

Il patto con chi lo usa:

- gli utenti si consumano **in ordine di lista**, e i corsi di ciascuno in
  ordine di scansione: la coda e' piatta, prima tutti i corsi dell'utente 1,
  poi quelli dell'utente 2, e cosi' via;
- il motore tiene aperte al piu' N finestre-corso in totale (N = impostazione
  "max_simultaneous", modificabile dalla UI a motore acceso);
- quando una finestra finisce (corso al 100%, attivita' non video = test, o
  errore) la finestra si chiude da sola e lo slot passa al prossimo corso
  della coda; se il prossimo corso e' dello stesso utente la finestra viene
  riusata, altrimenti se ne apre un'altra nel browser dell'utente giusto
  (un'istanza Chrome separata per utente, con profilo e porta propri);
- un listener controlla ogni finestra: video in pausa, video bloccato pur non
  essendo in pausa, video finito che non avanza -> refresh della pagina;
- tutto lo stato vive nello Store: chiuso e riaperto il software, si riprende
  da dove si era rimasti.

Thread: uno per il motore (scansioni + scheduling), uno per ogni utente con
browser acceso. Ogni driver Selenium e' toccato solo dal thread della propria
sessione: lo switch di finestra e' stato globale del driver, non si condivide.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from urllib.parse import urlparse

from selenium.common.exceptions import (
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from . import moodle
from .state import (
    STATUS_AI_TEST,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_NO_ACTIVITY,
    STATUS_PENDING,
    STATUS_RUNNING,
    Store,
    profile_slug,
)

# Quanto aspettare, alla chiusura del motore, che le sessioni chiudano da sole
# il proprio browser prima di forzarle con il pkill.
SHUTDOWN_GRACE_S = 25.0

# Un utente il cui browser e' morto (crash, chiusura manuale) riparte dopo
# questo cooldown, per non finire in un ciclo stretto crash-restart.
DEAD_COOLDOWN_S = 30.0

# Tentativi di apertura di uno stesso corso prima di marcarlo in errore.
MAX_OPEN_ATTEMPTS = 3


class WindowTracker:
    """Cio' che il listener ricorda di una finestra-corso."""

    def __init__(self, handle: str, course: dict):
        self.handle = handle
        self.course = course
        self.parked = False  # chiusa per il motore, in attesa di riuso o chiusura
        self.last_url = ""
        self.last_url_change = time.monotonic()
        self.last_current = 0.0  # secondo del video all'ultima lettura utile
        self.paused_since: float | None = None
        self.ended_since: float | None = None
        self.frozen_since: float | None = None
        self.refresh_streak = 0
        self.relogins = 0
        self.last_progress_poll = 0.0

    def note_url(self, url: str) -> None:
        """Registra l'URL corrente: se e' cambiato, il corso e' avanzato."""
        if url and url != self.last_url:
            self.last_url = url
            self.last_url_change = time.monotonic()
            self.clear_timers()
            self.refresh_streak = 0

    def clear_timers(self) -> None:
        self.paused_since = self.ended_since = self.frozen_since = None
        self.last_current = 0.0


class UserSession(threading.Thread):
    """Il browser di un utente: le sue finestre-corso e il loro listener."""

    def __init__(self, engine: "Engine", user: dict, profile, port: int):
        super().__init__(name=f"session-{user['username']}", daemon=True)
        self.engine = engine
        self.username = user["username"]
        self.password = user["password"]
        self.profile = profile
        self.port = port
        self.cmds: queue.Queue = queue.Queue()
        self.windows: dict[str, WindowTracker] = {}
        self.driver = None
        self.stopping = False
        self.quit_browser = False
        self.settings = engine.store.settings()

    # ------------------------------------------------------------ reporting

    def _report(self, kind: str, *payload) -> None:
        self.engine.reports.put((self, kind, payload))

    def _event(self, text: str) -> None:
        self._report("event", text)

    # ------------------------------------------------------------- avvio

    def _connect(self) -> None:
        estensione = moodle.DEFAULT_EXTENSION
        if not (estensione / "manifest.json").is_file():
            raise RuntimeError(f"estensione VideoGo mancante in {estensione}")
        # Avanzi di un giro precedente col profilo occupato: un Chrome vivo su
        # questo profilo farebbe scattare il ProcessSingleton al lancio.
        moodle.close_dedicated_chrome(self.profile, timeout=8)
        self.driver, _ = moodle.build_driver(
            self.profile,
            self.port,
            estensione,
            self.settings.get("pinned_chrome", ""),
            self.engine.headless,
        )
        # L'interruttore serve a ricaricare l'estensione a mano dalla pagina
        # chrome://extensions: senza finestre non c'e' mano da usare.
        if not self.engine.headless:
            moodle.ensure_developer_mode(self.driver)
        self.driver.get(moodle.COURSES_URL)
        moodle.ensure_logged_in(
            self.driver, moodle.COURSES_URL, (self.username, self.password), allow_manual=False
        )

    def run(self) -> None:
        try:
            try:
                self._connect()
            except TimeoutException as exc:
                self._report("login_failed", str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - il motivo finisce nell'evento
                self._report("dead", f"avvio fallito: {exc}")
                return
            self._report("ready")
            while not self.stopping:
                self._drain()
                if self.stopping:
                    break
                try:
                    self._poll_windows()
                except WebDriverException as exc:
                    self._report("dead", f"browser perso: {exc.__class__.__name__}")
                    return
                except Exception as exc:  # noqa: BLE001
                    self._report("dead", f"errore inatteso: {exc}")
                    return
                time.sleep(0.4)
        finally:
            self._cleanup_browser()
            self._report("session_quit")

    def _cleanup_browser(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
        # Il pkill per profilo prende anche eventuali processi rimasti orfani.
        moodle.close_dedicated_chrome(self.profile, timeout=10)

    # ------------------------------------------------------------ comandi

    def _drain(self) -> None:
        while True:
            try:
                cmd, payload = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd == "quit":
                self.stopping = True
                self.quit_browser = True
                return
            try:
                if cmd == "open":
                    self._cmd_open(payload)
                elif cmd == "navigate":
                    handle, course = payload
                    self._cmd_open(course, handle)
                elif cmd == "close":
                    self._cmd_close(payload)
                elif cmd == "settings":
                    self.settings.update(payload)
                elif cmd == "rescan":
                    self._cmd_rescan()
            except WebDriverException as exc:
                self._report("dead", f"comando {cmd} fallito: {exc.__class__.__name__}")
                return

    def _cmd_open(self, course: dict, handle: str | None = None) -> None:
        """Porta una finestra (nuova o riusata) sul corso, fino al primo video."""
        if handle is None:
            # Riusa una finestra parcheggiata se c'e', altrimenti apri l'ultima
            # finestra vuota del browser o creane una nuova.
            parcheggiata = next((w.handle for w in self.windows.values() if w.parked), None)
            if parcheggiata is not None:
                handle = parcheggiata
            elif len(self.driver.window_handles) == 1 and not self.windows:
                handle = self.driver.window_handles[0]  # la finestra iniziale, vuota
            else:
                self.driver.switch_to.new_window("window")
                handle = self.driver.current_window_handle
        try:
            self.driver.switch_to.window(handle)
        except NoSuchWindowException:
            self.windows.pop(handle, None)
            self._report("open_failed", course["id"], "finestra scomparsa")
            return

        esito = moodle.open_course_in_window(self.driver, course["url"])
        tracker = WindowTracker(handle, course)
        tracker.note_url(moodle.current_url(self.driver))
        self.windows[handle] = tracker

        if esito == "videotime":
            self._report("opened", handle, course["id"])
            return
        if esito == "unknown":
            self.windows.pop(handle, None)
            self._report("open_failed", course["id"], "sidebar del corso non caricata")
            return
        # ai_test / completed / no_activity: niente video da guardare.
        done, tot = moodle.course_progress(self.driver)
        self._park(tracker)
        stato = {
            "ai_test": STATUS_AI_TEST,
            "completed": STATUS_COMPLETED,
            "no_activity": STATUS_NO_ACTIVITY,
        }[esito]
        self._report("terminal", handle, course["id"], stato, moodle.progress_percent(done, tot))

    def _cmd_close(self, handle: str) -> None:
        tracker = self.windows.pop(handle, None)
        if tracker is None:
            return
        try:
            self.driver.switch_to.window(handle)
            if len(self.driver.window_handles) > 1:
                self.driver.close()
            else:
                # Ultima finestra: chiuderla spegnerebbe il browser (e la
                # sessione con lei). La svuotiamo: al motore non serve piu'.
                self.driver.get("about:blank")
        except (NoSuchWindowException, WebDriverException):
            pass  # gia' chiusa a mano

    def _cmd_rescan(self) -> None:
        self._event("riscansione dei corsi dal browser aperto...")
        trovati = []
        try:
            handle = self.driver.current_window_handle
            self.driver.get(moodle.COURSES_URL)
            if "/login/" in moodle.current_url(self.driver):
                moodle.ensure_logged_in(
                    self.driver,
                    moodle.COURSES_URL,
                    (self.username, self.password),
                    allow_manual=False,
                )
            for c in moodle.collect_courses(self.driver):
                self.driver.get(c["url"])
                stato, _, prog = moodle.classify_course(self.driver)
                trovati.append(
                    {"course": c, "status": stato, "progress": moodle.progress_percent(*prog)}
                )
            self.driver.switch_to.window(handle)
            self.driver.get("about:blank")
            self._report("scan_done", trovati)
        except (WebDriverException, TimeoutException) as exc:
            self._event(f"riscansione fallita: {exc.__class__.__name__}")

    def _park(self, tracker: WindowTracker) -> None:
        """Sgancia la finestra dal corso fermandola, tenendola per il riuso."""
        tracker.parked = True
        try:
            self.driver.get("about:blank")
        except WebDriverException:
            pass

    # ----------------------------------------------------------- listener

    def _poll_windows(self) -> None:
        for tracker in list(self.windows.values()):
            if not tracker.parked:
                self._poll_window(tracker)

    def _poll_window(self, w: WindowTracker) -> None:
        try:
            self.driver.switch_to.window(w.handle)
        except NoSuchWindowException:
            self.windows.pop(w.handle, None)
            self._report("window_gone", w.handle, w.course["id"])
            return

        url = moodle.current_url(self.driver)
        w.note_url(url)
        now = time.monotonic()
        path = urlparse(url).path

        if "/login/" in url:
            if w.relogins >= 2:
                self._event(f"sessione scaduta due volte: chiudo {w.course['title']} in errore")
                self._terminal(w, STATUS_ERROR, None)
                return
            self._relogin(w)
            return

        if moodle.VIDEOTIME_PATH in path:
            self._poll_video(w, now)
            self._maybe_report_progress(w, now)
            return

        if path.startswith("/mod/"):
            # Auto-avanzamento atterrato su un quiz o altra attivita' non
            # video: i test non li facciamo, la finestra si chiude.
            self._event(f"attivita' non video ({path}): chiudo {w.course['title']}")
            self._terminal(w, STATUS_AI_TEST, None)
            return

        if path.startswith("/course/"):
            stato, href, prog = moodle.classify_course(self.driver)
            if stato == STATUS_PENDING and href:
                moodle.click_link(self.driver, href)
                w.clear_timers()
            else:
                self._terminal(
                    w, stato if stato != STATUS_PENDING else STATUS_ERROR,
                    moodle.progress_percent(*prog),
                )
            return

        # Dashboard, pagina sconosciuta, blank: se ci resta troppo a lungo,
        # si torna al corso.
        if now - w.last_url_change > 90:
            self.driver.get(w.course["url"])
            w.clear_timers()

    def _poll_video(self, w: WindowTracker, now: float) -> None:
        vs = moodle.video_state(self.driver)
        self._report("video", w.course["id"], vs)
        s = self.settings

        if not vs or not vs.get("found"):
            # Il player non si e' ancora visto: dopo la grazia, refresh.
            if now - w.last_url_change > float(s.get("missing_grace_s", 120)):
                self._refresh(w, "player non caricato")
            return

        if vs.get("ended"):
            w.paused_since = w.frozen_since = None
            w.ended_since = w.ended_since or now
            if now - w.ended_since > float(s.get("ended_grace_s", 45)):
                self._refresh(w, "finito ma non passato al successivo")
        elif vs.get("paused"):
            w.ended_since = w.frozen_since = None
            w.paused_since = w.paused_since or now
            if now - w.paused_since > float(s.get("paused_grace_s", 45)):
                self._refresh(w, "in pausa")
        else:
            w.ended_since = w.paused_since = None
            current = float(vs.get("current") or 0)
            if current > w.last_current + 0.3:
                w.last_current = current
                w.frozen_since = None
            else:
                w.frozen_since = w.frozen_since or now
                if now - w.frozen_since > float(s.get("stall_grace_s", 150)):
                    self._refresh(w, "in riproduzione ma bloccato")

    def _refresh(self, w: WindowTracker, motivo: str) -> None:
        limite = int(self.settings.get("max_refresh_streak", 6))
        w.refresh_streak += 1
        self._event(f"refresh di {w.course['title']}: {motivo} ({w.refresh_streak}/{limite})")
        if w.refresh_streak >= limite:
            # Troppi refresh di fila senza avanzare: la pagina non se ne esce
            # da sola. Guarda la sidebar e decidi una volta per tutte.
            try:
                stato, href, prog = moodle.classify_course(self.driver)
            except WebDriverException:
                stato, href, prog = STATUS_ERROR, None, (0, 0)
            if stato == STATUS_PENDING and href:
                self._event(f"{w.course['title']}: riparto dalla prima attivita' incompleta")
                moodle.click_link(self.driver, href)
                w.clear_timers()
                return
            self._event(f"{w.course['title']}: refresh ripetuti senza esito, chiudo ({stato})")
            self._terminal(
                w,
                stato if stato in (STATUS_COMPLETED, STATUS_AI_TEST, STATUS_NO_ACTIVITY) else STATUS_ERROR,
                moodle.progress_percent(*prog),
            )
            return
        try:
            self.driver.refresh()
        except WebDriverException:
            pass
        w.clear_timers()  # i timer ripartono; lo streak resta, conta i tentativi

    def _relogin(self, w: WindowTracker) -> None:
        w.relogins += 1
        self._event(f"sessione scaduta, rilogin come {self.username} (tentativo {w.relogins})")
        try:
            if moodle.auto_login(self.driver, self.username, self.password, w.course["url"]):
                self.driver.get(w.course["url"])
                stato, href, _ = moodle.classify_course(self.driver)
                if stato == STATUS_PENDING and href:
                    moodle.click_link(self.driver, href)
                w.clear_timers()
        except WebDriverException:
            pass  # al prossimo giro: o rilogin riuscito, o chiusura in errore

    def _terminal(self, w: WindowTracker, status: str, progress: int | None) -> None:
        """Il corso in questa finestra e' finito: parcheggia e riferisci."""
        if progress is None:
            done, tot = moodle.course_progress(self.driver)
            if not tot:
                # La pagina corrente (un quiz, tipicamente) non ha la sidebar:
                # una passata sulla pagina del corso per l'ultima percentuale.
                try:
                    self.driver.get(w.course["url"])
                    try:
                        WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, moodle.SIDEBAR_SELECTOR)
                            )
                        )
                    except TimeoutException:
                        pass
                    done, tot = moodle.course_progress(self.driver)
                except WebDriverException:
                    done, tot = 0, 0
            progress = moodle.progress_percent(done, tot)
        self._park(w)
        self._report("terminal", w.handle, w.course["id"], status, progress)

    def _maybe_report_progress(self, w: WindowTracker, now: float) -> None:
        if now - w.last_progress_poll < 60:
            return
        w.last_progress_poll = now
        done, tot = moodle.course_progress(self.driver)
        if tot:
            self._report("progress", w.course["id"], moodle.progress_percent(done, tot))


class Engine:
    """Scansiona gli utenti, riempie gli slot, consuma i report delle sessioni."""

    def __init__(self, store: Store, headless: bool = False):
        self.store = store
        self.headless = headless
        self.reports: queue.Queue = queue.Queue()
        self.control: queue.Queue = queue.Queue()
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self._stop_flag = False
        self.status = "fermo"
        self.sessions: dict[str, UserSession] = {}
        self.ports_in_use: set[int] = set()
        self.dead_until: dict[str, float] = {}
        self.open_attempts: dict[tuple[str, str], int] = {}
        self.video_info: dict[str, dict[str, dict]] = {}
        self.events: deque = deque(maxlen=400)
        self.scan_now: tuple[str, int, int] | None = None
        self._scanning_now: set[str] = set()

    # ---------------------------------------------------------- ciclo vita

    def start(self) -> bool:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            self._stop_flag = False
            self.status = "avvio"
            self.thread = threading.Thread(target=self._run, name="engine", daemon=True)
            self.thread.start()
            return True

    def stop(self) -> None:
        """Chiede l'arresto: le finestre si chiudono in background."""
        with self.lock:
            self._stop_flag = True

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _stop_requested(self) -> bool:
        with self.lock:
            return self._stop_flag

    def _revert_all_running(self) -> None:
        """Riporta in coda i corsi marcati "running".

        Uno stato "running" ha senso solo finche' la sua finestra esiste: a
        motore fermo (o dopo un crash) e' un fantasma che occuperebbe uno
        slot senza che nessuno lo riapra mai. Il video riprende dal punto
        giusto: la posizione e' della piattaforma, non nostra.
        """
        for utente in self.store.users():
            for c in utente["courses"]:
                if c["status"] == STATUS_RUNNING:
                    self.store.set_course_status(utente["username"], c["id"], STATUS_PENDING)

    def _run(self) -> None:
        try:
            self.status = "scansione"
            self.event("", "motore avviato")
            self._revert_all_running()
            self._scan_missing()
            if self._stop_requested():
                return
            self.status = "in esecuzione"
            while not self._stop_requested():
                self._drain_control()
                self._drain_reports()
                self._reconcile()
                self._maybe_scan_new_users()
                time.sleep(0.7)
        except Exception as exc:  # noqa: BLE001 - il motore non deve morire in silenzio
            self.event("", f"errore fatale del motore: {exc}")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self.status = "in arresto"
        with self.lock:
            sessioni = list(self.sessions.values())
        for sess in sessioni:
            sess.cmds.put(("quit", None))
        scadenza = time.monotonic() + SHUTDOWN_GRACE_S
        while time.monotonic() < scadenza:
            self._drain_reports()
            with self.lock:
                vive = [s for s in self.sessions.values() if s.is_alive()]
            if not vive:
                break
            time.sleep(0.3)
        for sess in vive:  # non hanno chiuso da sole: forza la mano
            try:
                moodle.close_dedicated_chrome(sess.profile, timeout=8)
            except OSError:
                pass
        with self.lock:
            self.sessions.clear()
            self.ports_in_use.clear()
            self.video_info.clear()
            self.scan_now = None
        self._revert_all_running()
        self.store.save()
        self.status = "fermo"
        self.event("", "motore fermato")

    # -------------------------------------------------------------- eventi

    def event(self, user: str, msg: str) -> None:
        self.events.append({"ts": time.strftime("%H:%M:%S"), "user": user, "msg": msg})

    # ------------------------------------------------------------ scansione

    def _pick_port(self) -> int:
        """Prima porta di debug libera, riservata a un browser nostro."""
        with self.lock:
            port = moodle.DEBUG_PORT
            while port < moodle.DEBUG_PORT + 200:
                if port not in self.ports_in_use and not moodle.debugger_alive(port):
                    self.ports_in_use.add(port)
                    return port
                port += 1
        raise RuntimeError("porte di debug esaurite: troppi browser contemporanei")

    def _release_port(self, port: int) -> None:
        with self.lock:
            self.ports_in_use.discard(port)

    def _scanning(self, username: str) -> bool:
        with self.lock:
            return username in self._scanning_now

    def _scan_missing(self) -> None:
        for user in self.store.users():
            if self._stop_requested():
                return
            if user.get("last_scan") is None and not self._scanning(user["username"]):
                self._scan_user(user)

    def _maybe_scan_new_users(self) -> None:
        """Utenti aggiunti a motore acceso: scannali appena compaiono."""
        for user in self.store.users():
            if (
                user.get("last_scan") is None
                and not user.get("login_error")
                and not self._scanning(user["username"])
            ):
                self._scan_user(user)
                return  # uno alla volta, il prossimo giro prende il seguente

    def _scan_user(self, user: dict) -> None:
        nome = user["username"]
        with self.lock:
            if nome in self._scanning_now:
                return
            self._scanning_now.add(nome)
        try:
            self._scan_user_locked(user)
        finally:
            with self.lock:
                self._scanning_now.discard(nome)

    def _scan_user_locked(self, user: dict) -> None:
        nome = user["username"]
        self.scan_now = (nome, 0, 0)
        self.event(nome, "scansione dei corsi...")
        driver = None
        port: int | None = None
        profile = moodle.PROFILES_BASE / user["slug"]
        try:
            port = self._pick_port()
            profile.mkdir(parents=True, exist_ok=True)
            moodle.close_dedicated_chrome(profile, timeout=8)
            driver, _ = moodle.build_driver(
                profile,
                port,
                moodle.DEFAULT_EXTENSION,
                self.store.settings().get("pinned_chrome", ""),
                self.headless,
            )
            if not self.headless:
                moodle.ensure_developer_mode(driver)
            driver.get(moodle.COURSES_URL)
            moodle.ensure_logged_in(
                driver, moodle.COURSES_URL, (nome, user["password"]), allow_manual=False
            )
            courses = moodle.collect_courses(driver)
            self.event(nome, f"trovati {len(courses)} corsi, leggo i progressi...")
            vivi: set[str] = set()
            for i, corso in enumerate(courses):
                if self._stop_requested():
                    break
                try:
                    driver.get(corso["url"])
                    stato, _, prog = moodle.classify_course(driver)
                    pct = moodle.progress_percent(*prog)
                except WebDriverException:
                    stato, pct = STATUS_PENDING, None
                attuale = self.store.get_course(nome, corso["id"])
                if attuale and attuale["status"] == STATUS_RUNNING:
                    stato, pct = None, None  # in corso adesso: non toccarlo
                self.store.merge_course(nome, corso, stato, pct)
                vivi.add(corso["id"])
                self.scan_now = (nome, i + 1, len(courses))
            self.store.drop_missing_courses(nome, vivi)
            self.store.set_scanned(nome)
            self.store.set_login_error(nome, None)
            self.event(nome, f"scansione completata: {len(courses)} corsi")
        except TimeoutException:
            self.store.set_login_error(nome, "login rifiutato o non riuscito")
            self.event(nome, "login non riuscito: utente saltato")
        except WebDriverException as exc:
            self.event(nome, f"errore browser durante la scansione: {exc.__class__.__name__}")
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except WebDriverException:
                    pass
            moodle.close_dedicated_chrome(profile, timeout=8)
            if port is not None:
                self._release_port(port)
            self.scan_now = None

    # ----------------------------------------------------------- scheduling

    def _running_count(self) -> int:
        return sum(
            1 for u in self.store.users() for c in u["courses"] if c["status"] == STATUS_RUNNING
        )

    def _next_pending(self) -> tuple[dict, dict] | None:
        adesso = time.monotonic()
        for u in self.store.users():
            if u.get("login_error"):
                continue
            if self.dead_until.get(u["username"], 0) > adesso:
                continue
            for c in u["courses"]:
                if c["status"] == STATUS_PENDING:
                    return u, c
        return None

    def _reconcile(self) -> None:
        maxsim = int(self.store.settings().get("max_simultaneous", 8))
        while not self._stop_requested() and self._running_count() < maxsim:
            nxt = self._next_pending()
            if nxt is None:
                return
            utente, corso = nxt
            sess = self._session_for(utente)
            if sess is None:
                return
            self.store.set_course_status(utente["username"], corso["id"], STATUS_RUNNING)
            sess.cmds.put(("open", dict(corso)))
            self.event(utente["username"], f"apro il corso: {corso['title']}")

    def _session_for(self, utente: dict) -> UserSession | None:
        nome = utente["username"]
        with self.lock:
            sess = self.sessions.get(nome)
            if sess is not None and sess.is_alive():
                return sess
            try:
                port = self._pick_port()
            except RuntimeError as exc:
                self.event(nome, str(exc))
                return None
            profile = moodle.PROFILES_BASE / utente["slug"]
            sess = UserSession(self, utente, profile, port)
            self.sessions[nome] = sess
            sess.start()
            return sess

    def _maybe_quit_session(self, username: str) -> None:
        """Niente finestre ne' corsi da aprire per questo utente: browser off."""
        utente = self.store.get_user(username)
        if utente is None:
            return
        if any(c["status"] in (STATUS_RUNNING, STATUS_PENDING) for c in utente["courses"]):
            return
        with self.lock:
            sess = self.sessions.get(username)
        if sess is not None and sess.is_alive():
            sess.cmds.put(("quit", None))
            self.event(username, "utente completato: chiudo il suo browser")

    # -------------------------------------------------------------- report

    def _drain_reports(self) -> None:
        while True:
            try:
                sess, kind, payload = self.reports.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_report(sess, kind, payload)
            except Exception as exc:  # noqa: BLE001
                self.event(sess.username, f"report {kind} non gestito: {exc}")

    def _handle_report(self, sess: UserSession, kind: str, payload: tuple) -> None:
        user = sess.username
        if kind == "event":
            self.event(user, payload[0])
        elif kind == "ready":
            self.event(user, "browser pronto e loggato")
        elif kind == "login_failed":
            self.store.set_login_error(user, "login rifiutato o non riuscito")
            self._revert_running(user)
            self.event(user, "login non riuscito: i suoi corsi restano in coda")
        elif kind == "dead":
            self.store.set_login_error(user, None)  # non e' colpa delle credenziali
            self._revert_running(user)
            self.dead_until[user] = time.monotonic() + DEAD_COOLDOWN_S
            self.event(user, f"browser perso ({payload[0]}): riparto tra {DEAD_COOLDOWN_S:.0f}s")
        elif kind == "session_quit":
            with self.lock:
                if self.sessions.get(user) is sess:
                    del self.sessions[user]
                self.ports_in_use.discard(sess.port)
        elif kind == "opened":
            _, course_id = payload
            with self.lock:
                self.video_info.setdefault(user, {})[course_id] = {}
        elif kind == "open_failed":
            course_id, motivo = payload
            tentativi = self.open_attempts.get((user, course_id), 0) + 1
            self.open_attempts[(user, course_id)] = tentativi
            if tentativi >= MAX_OPEN_ATTEMPTS:
                self.store.set_course_status(user, course_id, STATUS_ERROR)
                corso = self.store.get_course(user, course_id)
                self.event(user, f"corso non apribile ({motivo}): marco errore "
                                 f"{corso['title'] if corso else course_id}")
            else:
                self.store.set_course_status(user, course_id, STATUS_PENDING)
                self.event(user, f"apertura non riuscita ({motivo}), riprovo")
        elif kind == "terminal":
            handle, course_id, stato, progresso = payload
            self.store.set_course_status(user, course_id, stato, progresso)
            with self.lock:
                self.video_info.get(user, {}).pop(course_id, None)
            corso = self.store.get_course(user, course_id)
            titolo = corso["title"] if corso else course_id
            etichetta = {"completed": "completato", "ai_test": "fermo ai test",
                         "no_activity": "senza attivita'", "error": "in errore"}.get(stato, stato)
            self.event(user, f"chiudo la finestra di {titolo}: {etichetta} "
                             f"({progresso}%)")
            self._after_terminal(user, handle)
        elif kind == "window_gone":
            handle, course_id = payload
            self.store.set_course_status(user, course_id, STATUS_PENDING)
            with self.lock:
                self.video_info.get(user, {}).pop(course_id, None)
            self.event(user, "finestra chiusa a mano: il corso torna in coda")
            self._maybe_quit_session(user)
        elif kind == "progress":
            course_id, pct = payload
            self.store.set_course_progress(user, course_id, int(pct))
        elif kind == "video":
            course_id, vs = payload
            with self.lock:
                self.video_info.setdefault(user, {})[course_id] = dict(vs or {}) or {"missing": True}
        elif kind == "scan_done":
            self._merge_scan(user, payload[0])
        # gli altri eventi restano nel log delle sessioni

    def _after_terminal(self, user: str, handle: str) -> None:
        """Slot liberato: riusa la finestra se il prossimo corso e' lo stesso
        utente, altrimenti chiudila e lascia che il _reconcile apra altrove."""
        if self._stop_requested():
            return
        nxt = self._next_pending()
        if nxt is not None:
            utente, corso = nxt
            if utente["username"] == user:
                with self.lock:
                    sess = self.sessions.get(user)
                if sess is not None and sess.is_alive():
                    self.store.set_course_status(user, corso["id"], STATUS_RUNNING)
                    sess.cmds.put(("navigate", (handle, dict(corso))))
                    self.event(user, f"riuso la finestra per: {corso['title']}")
                    return
        with self.lock:
            sess = self.sessions.get(user)
        if sess is not None and sess.is_alive():
            sess.cmds.put(("close", handle))
        self._maybe_quit_session(user)

    def _revert_running(self, user: str) -> None:
        utente = self.store.get_user(user)
        if utente is None:
            return
        for c in utente["courses"]:
            if c["status"] == STATUS_RUNNING:
                self.store.set_course_status(user, c["id"], STATUS_PENDING)
        with self.lock:
            self.video_info.pop(user, None)

    def _merge_scan(self, user: str, trovati: list[dict]) -> None:
        vivi = set()
        for voce in trovati:
            corso = voce["course"]
            attuale = self.store.get_course(user, corso["id"])
            if attuale and attuale["status"] == STATUS_RUNNING:
                self.store.merge_course(user, corso, None, None)
            else:
                self.store.merge_course(user, corso, voce["status"], voce["progress"])
            vivi.add(corso["id"])
        self.store.drop_missing_courses(user, vivi)
        self.store.set_scanned(user)
        self.store.set_login_error(user, None)
        self.event(user, f"riscansione completata: {len(trovati)} corsi")

    # -------------------------------------------------------------- control

    def _drain_control(self) -> None:
        while True:
            try:
                cmd, payload = self.control.get_nowait()
            except queue.Empty:
                return
            if cmd == "settings":
                for sess in list(self._live_sessions()):
                    sess.cmds.put(("settings", dict(payload)))

    def _live_sessions(self) -> list[UserSession]:
        with self.lock:
            return [s for s in self.sessions.values() if s.is_alive()]

    # ------------------------------------------------------------ da fuori

    def drop_user(self, username: str) -> bool:
        """Rimozione immediata dallo stato; il browser si chiude in background.

        Non passa dalla coda del motore: con il motore fermo nessuno la
        processerebbe, e l'utente resterebbe li' per sempre.
        """
        rimosso = self.store.remove_user(username)
        if rimosso:
            self.event(username, "utente rimosso")
        with self.lock:
            sess = self.sessions.get(username)
        if sess is not None and sess.is_alive():
            sess.cmds.put(("quit", None))
        return rimosso

    def rename_user(
        self,
        vecchio: str,
        nuovo: str,
        name: str | None = None,
        password: str | None = None,
    ) -> tuple[dict | None, str | None]:
        """Modifica un utente (username, nome, password).

        Rinominare sposta anche la cartella del profilo browser, cosi' la
        sessione scoperta finora resta valida; serve che l'utente non abbia
        browser accesi, perche' il suo processo punta ai percorsi vecchi.
        Ritorna (utente, None) oppure (None, motivo_del_rifiuto).
        """
        with self.lock:
            sess = self.sessions.get(vecchio)
        if sess is not None and sess.is_alive():
            return None, "l'utente ha il browser attivo: ferma il motore prima di modificarlo"
        utente = self.store.get_user(vecchio)
        if utente is None:
            return None, "utente inesistente"
        nuovo = (nuovo or "").strip()
        if not nuovo:
            return None, "username obbligatorio"
        if nuovo != vecchio:
            if self.store.get_user(nuovo) is not None:
                return None, f"esiste gia' un utente '{nuovo}'"
            vecchia_dir = moodle.PROFILES_BASE / utente["slug"]
            nuova_dir = moodle.PROFILES_BASE / profile_slug(nuovo)
            if vecchia_dir.is_dir() and not nuova_dir.exists():
                try:
                    nuova_dir.parent.mkdir(parents=True, exist_ok=True)
                    vecchia_dir.rename(nuova_dir)
                except OSError:
                    pass  # resta la vecchia: alla prossima scansione se ne crea una nuova
            with self.lock:
                self.open_attempts = {
                    k: v for k, v in self.open_attempts.items() if k[0] != vecchio
                }
                self.dead_until.pop(vecchio, None)
                self.video_info.pop(vecchio, None)
        modificato = self.store.update_user(vecchio, new_username=nuovo, name=name, password=password)
        self.event(
            nuovo,
            f"utente modificato{f' (era {vecchio})' if nuovo != vecchio else ''}",
        )
        return modificato, None

    def clear_users(self) -> int:
        """Rimuove tutti gli utenti e chiude i loro browser.

        Il motore, se e' acceso, resta su ma resta anche a piedi: senza
        utenti non ha niente da scansionare ne' da aprire.
        """
        with self.lock:
            sessioni = list(self.sessions.values())
        for sess in sessioni:
            if sess.is_alive():
                sess.cmds.put(("quit", None))
        with self.lock:
            self.open_attempts.clear()
            self.dead_until.clear()
            self.video_info.clear()
        quanti = self.store.clear_users()
        self.event("", "utenti svuotati: rimossi tutti i browser e i dati")
        return quanti

    def rescan_user(self, username: str) -> bool:
        """Riscansione: dal browser aperto se c'e', altrimenti con uno suo.

        Con il motore fermo parte in un thread dedicato, cosi' la richiesta
        della UI non resta appesa per tutta la scansione.
        """
        utente = self.store.get_user(username)
        if utente is None or self._scanning(username):
            return False
        with self.lock:
            sess = self.sessions.get(username)
        if sess is not None and sess.is_alive():
            sess.cmds.put(("rescan", None))
            return True
        threading.Thread(
            target=self._scan_user, args=(utente,), daemon=True, name=f"scan-{username}"
        ).start()
        return True

    def retry_login(self, username: str) -> None:
        """Sblocca un utente fermato da login errata o da un browser morto."""
        with self.lock:
            self.dead_until.pop(username, None)
        self.store.set_login_error(username, None)
        self.event(username, "nuovo tentativo di login abilitato")

    def broadcast_settings(self, settings: dict) -> None:
        self.control.put(("settings", settings))

    def snapshot(self) -> dict:
        """Quadro per la UI: stato motore, slot, browser, video, eventi."""
        with self.lock:
            return {
                "running": self.is_running(),
                "status": self.status,
                "headless": self.headless,
                "windows": self._running_count(),
                "scan": {"user": self.scan_now[0], "done": self.scan_now[1], "total": self.scan_now[2]}
                if self.scan_now
                else None,
                "browsers": {u: s.is_alive() for u, s in self.sessions.items()},
                "video": {u: dict(v) for u, v in self.video_info.items()},
                "events": list(self.events)[-120:],
            }
