#!/usr/bin/env python3
"""Apre una finestra di Chrome con una scheda per ogni corso presente in
https://fad-for-me.formretail.it/my/courses.php

Usa un profilo Chrome dedicato, separato da quello personale. Le credenziali
si mettono nel file .env accanto allo script (FAD_USERNAME / FAD_PASSWORD):
le legge solo questo processo e vengono digitate direttamente nel form di
login del sito. Senza .env lo script aspetta che il login lo faccia l'utente
a mano nella finestra aperta.

Uso:
    python3 course_opener.py                 # apre tutte le schede
    python3 course_opener.py --dry-run       # elenca i corsi senza aprire schede
    python3 course_opener.py --max 5         # apre solo i primi 5
    python3 course_opener.py --filter team   # solo i corsi il cui titolo contiene "team"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

COURSES_URL = "https://fad-for-me.formretail.it/my/courses.php"
LOGIN_URL = "https://fad-for-me.formretail.it/login/index.php"
HERE = Path(__file__).resolve().parent
# Il profilo sta fuori dalla cartella di progetto apposta: contiene i cookie di
# sessione, e la cartella e' pensata per essere condivisa.
DEFAULT_PROFILE = Path.home() / ".courseopener" / "chrome-profile"
DEFAULT_EXTENSION = HERE / "VideoGo"
DEFAULT_ENV_FILE = HERE / ".env"

CARD_SELECTOR = 'div[data-region="course-content"][data-course-id]'
SIDEBAR_SELECTOR = "#course-index"
VIDEOTIME_PATH = "/mod/videotime"
LOGIN_TIMEOUT = 300  # secondi a disposizione per fare il login a mano
DEBUG_PORT = 9333  # porta di debug del Chrome dedicato (non la 9222, per non pestare i piedi ad altro)


def load_env_file(path: Path) -> dict[str, str]:
    """Parser minimale per .env (CHIAVE=valore, # per i commenti)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ")
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def read_credentials(env_file: Path) -> tuple[str, str]:
    """Credenziali da .env, con precedenza alle variabili d'ambiente."""
    env = load_env_file(env_file)
    username = os.environ.get("FAD_USERNAME") or env.get("FAD_USERNAME", "")
    password = os.environ.get("FAD_PASSWORD") or env.get("FAD_PASSWORD", "")
    return username.strip(), password


def debugger_alive(port: int) -> bool:
    """True se sulla porta c'e' gia' un Chrome nostro in ascolto."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
            return True
    except (urllib.error.URLError, OSError):
        return False


def close_dedicated_chrome(profile_dir: Path) -> None:
    """Chiude SOLO il Chrome lanciato con il profilo dedicato di questo script."""
    # Niente trattini iniziali nel pattern: pkill li scambierebbe per opzioni.
    subprocess.run(["pkill", "-f", f"user-data-dir={profile_dir}"], check=False)
    time.sleep(2)


def ensure_developer_mode(driver: webdriver.Chrome) -> None:
    """Accende la modalita' sviluppatore di chrome://extensions se e' spenta.

    Serve per poter ricaricare a mano l'estensione dalla sua pagina: senza,
    Chrome rifiuta il reload e la disattiva. Il pref e' protetto da un MAC in
    "Secure Preferences", quindi scriverlo nel file non funziona: va premuto
    l'interruttore vero. Una volta acceso resta, anche ai riavvii successivi.
    """
    corrente = current_url(driver)
    try:
        driver.get("chrome://extensions")
        acceso = driver.execute_script(
            """
            const bar = document.querySelector('extensions-manager')
                ?.shadowRoot.querySelector('extensions-toolbar');
            const toggle = bar?.shadowRoot.querySelector('#devMode');
            if (!toggle) return null;
            if (!toggle.checked) { toggle.click(); return 'acceso'; }
            return 'gia acceso';
            """
        )
        if acceso == "acceso":
            print("Modalita' sviluppatore attivata nel profilo.", flush=True)
    except WebDriverException:
        pass  # non e' essenziale: l'estensione funziona comunque
    finally:
        if corrente.startswith("http"):
            driver.get(corrente)


def prepare_clean_start(profile_dir: Path) -> None:
    """Impedisce a Chrome di ripristinare le finestre del giro precedente.

    Se il processo viene ucciso (o crasha) Chrome al rilancio ripristina la
    sessione, e le vecchie finestre si sommano alle nuove. I pref da soli non
    bastano: gli snapshot di sessione vanno anche rimossi. Sono solo elenchi di
    schede - cookie e login stanno altrove e restano intatti.
    """
    default = profile_dir / "Default"
    prefs = default / "Preferences"
    if prefs.is_file():
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        profilo = data.setdefault("profile", {})
        profilo["exit_type"] = "Normal"
        profilo["exited_cleanly"] = True
        # 5 = parti dalla pagina Nuova scheda, non dall'ultima sessione.
        data.setdefault("session", {})["restore_on_startup"] = 5
        data["session"]["startup_urls"] = []
        prefs.write_text(json.dumps(data), encoding="utf-8")

    for cartella in (default / "Sessions", default / "Session Storage"):
        if cartella.is_dir():
            for f in cartella.iterdir():
                if f.is_file():
                    f.unlink(missing_ok=True)


def unpacked_extension_id(path: Path) -> str:
    """ID che Chrome assegna a un'estensione caricata da cartella.

    E' l'hash SHA-256 del percorso assoluto: i primi 32 nibble mappati su a-p.
    Serve per raggiungere le sue pagine (chrome-extension://<id>/...) e quindi
    per verificare che sia stata caricata davvero.
    """
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def build_driver(
    profile_dir: Path, port: int = DEBUG_PORT, extension: Path | None = None
) -> tuple[webdriver.Chrome, bool]:
    """Riusa il Chrome dedicato se e' gia' aperto, altrimenti ne lancia uno.

    Restituisce (driver, reused). Riattaccarsi evita sia il conflitto sul
    profilo ("user data directory is already in use") sia un nuovo login: i
    cookie di sessione restano validi finche' quella finestra e' viva.
    """
    options = Options()
    # 'eager' = non aspetta immagini/iframe: aprire 30+ schede resta veloce.
    options.page_load_strategy = "eager"
    if extension:
        # Chrome stable (151+) ignora --load-extension anche disattivando la
        # feature che lo blocca: le estensioni fuori dal Web Store non si
        # caricano piu' in sessione automatizzata, ne' da cartella ne' da .crx.
        # Chrome for Testing invece le carica: lo scarica Selenium Manager e
        # resta in cache in ~/.cache/selenium.
        options.browser_version = "stable"

    if debugger_alive(port):
        options.debugger_address = f"127.0.0.1:{port}"
        try:
            return webdriver.Chrome(options=options), True
        except WebDriverException as exc:
            # Capita con una finestra rimasta orfana da un lancio precedente:
            # la porta risponde ma le schede non sono piu' pilotabili.
            print(f"Chrome dedicato non pilotabile ({exc.__class__.__name__}), lo riavvio.", flush=True)
            close_dedicated_chrome(profile_dir)
        options = Options()
        options.page_load_strategy = "eager"
        if extension:
            options.browser_version = "stable"

    prepare_clean_start(profile_dir)
    # Profilo dedicato: tiene la sessione e non va in conflitto con il Chrome
    # che l'utente ha gia' aperto per i fatti suoi.
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument(f"--remote-debugging-port={port}")
    options.add_argument("--start-maximized")
    options.add_experimental_option("detach", True)  # la finestra resta aperta a fine script
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    if extension:
        options.add_argument(f"--load-extension={extension}")
        # Senza, Chrome blocca l'autoplay con audio e l'estensione ripiega sul
        # muto: il video parte ma non si sente.
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        # Da Chrome 137 --load-extension e' disabilitato per default: questo
        # flag riapre la porta per l'avvio automatizzato.
        options.add_argument("--disable-features=DisableLoadExtensionCommandLineSwitch")
    return webdriver.Chrome(options=options), False


def current_url(driver: webdriver.Chrome) -> str:
    """URL della scheda attiva, "" se Chrome non lo espone (scheda ancora vuota)."""
    try:
        return driver.current_url or ""
    except WebDriverException:
        return ""


def fill_login_form(driver: webdriver.Chrome, username: str, password: str, attempts: int = 3) -> bool:
    """Compila e invia il form, ritentando se la pagina si ricarica sotto i piedi.

    La pagina di login Moodle puo' rifare il rendering subito dopo il primo
    caricamento: i riferimenti agli input diventano stale e vanno ripresi.
    """
    for _ in range(attempts):
        try:
            user_field = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[name="username"]'))
            )
            user_field.clear()
            user_field.send_keys(username)
            pass_field = driver.find_element(By.CSS_SELECTOR, 'input[name="password"]')
            pass_field.clear()
            pass_field.send_keys(password)
            try:
                driver.find_element(By.CSS_SELECTOR, "#loginbtn, button[type=submit]").click()
            except WebDriverException:
                pass_field.submit()
            return True
        except StaleElementReferenceException:
            time.sleep(1)
        except (TimeoutException, WebDriverException):
            return False
    return False


def auto_login(driver: webdriver.Chrome, username: str, password: str, url: str) -> bool:
    """Compila il form di login Moodle con le credenziali lette dal .env.

    La password non viene mai stampata ne' registrata: passa dal file
    direttamente al campo del form.
    """
    if "/login/" not in current_url(driver):
        driver.get(LOGIN_URL)

    print("Login automatico in corso...", flush=True)
    if not fill_login_form(driver, username, password):
        print("Form di login non compilabile: procedo col login manuale.", flush=True)
        return False

    deadline = time.time() + 30
    while time.time() < deadline:
        if "/login/" not in current_url(driver):
            driver.get(url)
            if is_courses_page(driver):
                print("Login automatico riuscito.", flush=True)
                return True
        for selector in (".loginerrors", "#loginerrormessage", ".alert-danger"):
            errors = driver.find_elements(By.CSS_SELECTOR, selector)
            if errors and errors[0].text.strip():
                print(f"Login rifiutato dal sito: {errors[0].text.strip()}", flush=True)
                return False
        time.sleep(1)
    print("Login automatico non concluso in tempo.", flush=True)
    return False


def ensure_logged_in(
    driver: webdriver.Chrome,
    url: str,
    credentials: tuple[str, str] = ("", ""),
    timeout: int = LOGIN_TIMEOUT,
) -> None:
    """Fa il login con le credenziali del .env, o aspetta quello manuale."""
    if is_courses_page(driver):
        return

    username, password = credentials
    if username and password and auto_login(driver, username, password, url):
        return

    print(
        "Completa il login nella finestra di Chrome appena aperta "
        f"(hai {timeout} secondi)...",
        flush=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_courses_page(driver):
            print("Login rilevato.", flush=True)
            return
        # Dopo il login Moodle atterra sulla dashboard: riportiamoci sui corsi.
        here = current_url(driver)
        if here and "/login/" not in here and "/my/courses.php" not in here:
            driver.get(url)
        time.sleep(2)
    raise TimeoutException("Login non completato entro il tempo limite.")


def is_courses_page(driver: webdriver.Chrome) -> bool:
    if "/login/" in current_url(driver):
        return False
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR)) or bool(
            driver.find_elements(By.CSS_SELECTOR, '[data-region="courses-view"]')
        )
    except WebDriverException:
        return False


def show_all_courses(driver: webdriver.Chrome) -> None:
    """Imposta la paginazione su 'Tutto' se non lo e' gia'."""
    try:
        toggle = driver.find_element(By.CSS_SELECTOR, '[data-action="limit-toggle"]')
        if toggle.text.strip().lower().startswith("tutto"):
            return
        toggle.click()
        item = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, '.dropdown-menu a[data-limit="0"]'))
        )
        item.click()
        time.sleep(2)  # il blocco ricarica le card via AJAX
    except WebDriverException:
        pass  # se il controllo non c'e' (o e' gia' a "Tutto") si prosegue


def collect_courses(driver: webdriver.Chrome) -> list[dict[str, str]]:
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, CARD_SELECTOR))
    )
    show_all_courses(driver)

    courses: list[dict[str, str]] = []
    seen: set[str] = set()
    for card in driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR):
        course_id = card.get_attribute("data-course-id") or ""
        if course_id in seen:
            continue
        try:
            link = card.find_element(By.CSS_SELECTOR, 'a.coursename[href*="/course/view.php"]')
        except WebDriverException:
            try:
                link = card.find_element(By.CSS_SELECTOR, 'a[href*="/course/view.php"]')
            except WebDriverException:
                continue
        href = link.get_attribute("href") or ""
        if not href:
            continue
        title = ""
        for sel in ("span.multiline", "span[title]"):
            found = card.find_elements(By.CSS_SELECTOR, sel)
            if found:
                title = found[0].get_attribute("title") or found[0].text.strip()
                if title:
                    break
        seen.add(course_id)
        courses.append(
            {
                "id": course_id,
                "title": title or f"Corso {course_id}",
                "url": urljoin(COURSES_URL, href),
            }
        )
    return courses


def first_incomplete_link(driver: webdriver.Chrome, timeout: int = 20) -> str | None:
    """href della prima voce della sidebar marcata come non completata.

    L'indice del corso (#course-index) e' reso lato server: le voci sono
    <li.courseindex-item> con dentro <a.courseindex-link> e, se l'attivita' non
    e' fatta, uno <span.completion_incomplete>. Si scorre in ordine di DOM, che
    e' l'ordine in cui compaiono nella sidebar.
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SIDEBAR_SELECTOR))
        )
    except TimeoutException:
        return None
    return driver.execute_script(
        """
        for (const item of document.querySelectorAll('#course-index li.courseindex-item')) {
            if (!item.querySelector('.completion_incomplete')) continue;
            const link = item.querySelector('a.courseindex-link[href], a[href]');
            return link ? link.href : null;
        }
        return null;
        """
    )


def is_videotime(href: str, host: str) -> bool:
    """True se il link punta a un'attivita' videotime dello stesso sito."""
    parsed = urlparse(href)
    return parsed.netloc == host and parsed.path.startswith(VIDEOTIME_PATH)


def click_link(driver: webdriver.Chrome, href: str) -> None:
    """Clicca il link nella sidebar; se non e' cliccabile ci naviga direttamente."""
    try:
        link = driver.find_element(By.CSS_SELECTOR, f'#course-index a[href="{href}"]')
        driver.execute_script("arguments[0].click();", link)
        WebDriverWait(driver, 20).until(lambda d: VIDEOTIME_PATH in current_url(d))
    except (TimeoutException, WebDriverException):
        driver.get(href)


def open_courses(
    driver: webdriver.Chrome,
    courses: list[dict[str, str]],
    reuse_first: bool,
    as_windows: bool = True,
    prune: bool = True,
    keep_anchor: bool = False,
) -> int:
    """Apre ogni corso in una finestra a se' (default) o in una scheda.

    Con prune attivo tiene solo le finestre in cui la prima attivita' non
    completata della sidebar e' un videotime (e la apre), chiudendo le altre.
    Restituisce quante finestre restano aperte.
    """
    target = "window" if as_windows else "tab"
    host = urlparse(COURSES_URL).netloc
    # La finestra dell'elenco fa da ancora: se chiudessimo tutte le altre senza
    # tenerne una, Chrome si chiuderebbe del tutto portandosi via la sessione.
    anchor = driver.current_window_handle
    kept: list[str] = []

    for index, course in enumerate(courses):
        print(f"[{index + 1}/{len(courses)}] {course['title']}", flush=True)
        if not (index == 0 and reuse_first and not prune):
            driver.switch_to.new_window(target)
        handle = driver.current_window_handle
        try:
            driver.get(course["url"])
        except TimeoutException:
            print("    (caricamento lento, proseguo)")

        if not prune:
            kept.append(handle)
            continue

        href = first_incomplete_link(driver)
        if href and is_videotime(href, host):
            print(f"    tengo: primo incompleto e' videotime -> {href}")
            click_link(driver, href)
            kept.append(handle)
        else:
            motivo = "nessuna attivita' incompleta" if not href else f"primo incompleto non videotime -> {href}"
            print(f"    chiudo: {motivo}")
            driver.close()
            driver.switch_to.window(anchor)

    if prune and kept and anchor not in kept and not keep_anchor:
        driver.switch_to.window(anchor)
        driver.close()
    if kept:
        driver.switch_to.window(kept[0])
    return len(kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=COURSES_URL, help="pagina elenco corsi")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="cartella profilo Chrome dedicato")
    parser.add_argument("--port", type=int, default=DEBUG_PORT, help="porta di debug del Chrome dedicato")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="file con FAD_USERNAME/FAD_PASSWORD")
    parser.add_argument("--extension", type=Path, default=DEFAULT_EXTENSION, help="cartella dell'estensione da caricare")
    parser.add_argument("--no-extension", action="store_true", help="niente estensione: usa il Chrome di sistema")
    parser.add_argument("--no-auto-login", action="store_true", help="ignora il .env e fai il login a mano")
    parser.add_argument("--max", type=int, default=0, help="apri al massimo N schede (0 = tutte)")
    parser.add_argument("--filter", default="", help="apri solo i corsi il cui titolo contiene questo testo")
    parser.add_argument("--dry-run", action="store_true", help="elenca i corsi senza aprire nulla")
    parser.add_argument("--tabs", action="store_true", help="schede in un'unica finestra invece di una finestra per corso")
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="apri tutti i corsi senza chiudere quelli il cui primo incompleto non e' un videotime",
    )
    parser.add_argument(
        "--keep-list-tab",
        action="store_true",
        help="tieni aperta anche la scheda con l'elenco corsi (default: viene riusata per il primo corso)",
    )
    args = parser.parse_args()

    extension: Path | None = None if args.no_extension else args.extension
    if extension and not (extension / "manifest.json").is_file():
        print(f"Nessun manifest.json in {extension}: proseguo senza estensione.", file=sys.stderr)
        extension = None
    if extension:
        # Profilo separato: Chrome for Testing e' un binario diverso da quello
        # di sistema, meglio non far litigare i due sulla stessa cartella.
        if args.profile == DEFAULT_PROFILE:
            args.profile = DEFAULT_PROFILE.with_name(DEFAULT_PROFILE.name + "-cft")
        print(f"Estensione: {extension} (Chrome for Testing)", flush=True)

    credentials = ("", "") if args.no_auto_login else read_credentials(args.env_file)
    if not args.no_auto_login and not all(credentials):
        print(
            f"Nessuna credenziale in {args.env_file}: il login andra' fatto a mano.",
            flush=True,
        )

    args.profile.mkdir(parents=True, exist_ok=True)
    driver, reused = build_driver(args.profile, args.port, extension)

    try:
        if reused:
            # Chrome dedicato gia' aperto: lavoriamo in una finestra nuova per
            # non toccare le schede del lancio precedente.
            print("Riuso la finestra di Chrome gia' aperta.", flush=True)
            driver.switch_to.new_window("window")
        if extension:
            ensure_developer_mode(driver)
        driver.get(args.url)
        ensure_logged_in(driver, args.url, credentials)
        if "/my/courses.php" not in current_url(driver):
            driver.get(args.url)

        courses = collect_courses(driver)
        if args.filter:
            needle = args.filter.casefold()
            courses = [c for c in courses if needle in c["title"].casefold()]
        if args.max > 0:
            courses = courses[: args.max]

        if not courses:
            print("Nessun corso trovato.", file=sys.stderr)
            return 1

        print(f"Trovati {len(courses)} corsi.", flush=True)
        if args.dry_run:
            for course in courses:
                print(f"  {course['id']:>6}  {course['title']}  ->  {course['url']}")
            return 0

        rimaste = open_courses(
            driver,
            courses,
            reuse_first=not args.keep_list_tab,
            as_windows=not args.tabs,
            prune=not args.no_prune,
            keep_anchor=args.keep_list_tab,
        )
        cosa = "schede" if args.tabs else "finestre"
        print(f"\nFatto: {rimaste} {cosa} su {len(courses)} corsi. Restano aperte a fine script.")
        if rimaste == 0:
            print("Nessun corso col primo incompleto su videotime: non ho lasciato nulla aperto.")
        return 0
    except TimeoutException as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    finally:
        # con detach=True Chrome sopravvive alla fine dello script
        pass


if __name__ == "__main__":
    raise SystemExit(main())
