"""Layer browser/piattaforma per FAD ForMe (https://fad-for-me.formretail.it).

Tutto quello che tocca Chrome o il sito sta qui: scelta del binario (Chrome for
Testing, con versione fissata), costruzione del driver, login, elenco dei corsi,
lettura dell'indice del corso e dello stato del video Vimeo.

Estratto da course_opener.py, che ora lo importano sia la CLI storica sia il
motore del rig di test (fadplatform.engine).
"""

from __future__ import annotations

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
from selenium.webdriver.common.selenium_manager import SeleniumManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

COURSES_URL = "https://fad-for-me.formretail.it/my/courses.php"
LOGIN_URL = "https://fad-for-me.formretail.it/login/index.php"
HOST = urlparse(COURSES_URL).netloc
HERE = Path(__file__).resolve().parent.parent
# Il profilo sta fuori dalla cartella di progetto apposta: contiene i cookie di
# sessione, e la cartella e' pensata per essere condivisa.
DEFAULT_PROFILE = Path.home() / ".courseopener" / "chrome-profile"
DEFAULT_EXTENSION = HERE / "VideoGo"
DEFAULT_ENV_FILE = HERE / ".env"
PROFILES_BASE = Path.home() / ".courseopener" / "profiles"

# Versione di Chrome for Testing da usare: fissata per non scaricarne una
# diversa a ogni avvio. Vuota = comportamento storico (la piu' alta in cache).
DEFAULT_PINNED_CFT = "153.0.8010.52"

# Niente nome di tag davanti: "I miei corsi" ha tre viste e cambia elemento.
# A schede il corso e' un <div>, in vista Elenco un <li>: imporre "div" faceva
# trovare zero corsi appena l'utente passava a Elenco.
CARD_SELECTOR = '[data-region="course-content"][data-course-id]'
SIDEBAR_SELECTOR = "#course-index"
VIDEOTIME_PATH = "/mod/videotime"
LOGIN_TIMEOUT = 300  # secondi a disposizione per fare il login a mano
DEBUG_PORT = 9333  # porta di debug del Chrome dedicato (non la 9222, per non pestare i piedi ad altro)
SELENIUM_CACHE = Path.home() / ".cache" / "selenium" / "chrome"
SYSTEM_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


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


def close_dedicated_chrome(profile_dir: Path, timeout: float = 20.0) -> None:
    """Chiude SOLO il Chrome lanciato con il profilo dedicato di questo script.

    Aspetta che i processi siano davvero spariti: dopo il SIGTERM Chrome puo'
    metterci diversi secondi. Tornare troppo presto e' peggio che non chiudere
    affatto, perche' il lancio successivo trova il profilo ancora occupato e
    Chrome, invece di partire, passa le finestre all'istanza vecchia
    (ProcessSingleton) e termina: si finisce col browser sbagliato, senza
    estensione.
    """
    # Niente trattini iniziali nel pattern: pkill li scambierebbe per opzioni.
    pattern = f"user-data-dir={profile_dir}"
    subprocess.run(["pkill", "-f", pattern], check=False)
    scadenza = time.monotonic() + timeout
    while time.monotonic() < scadenza:
        vivi = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        if vivi.returncode != 0:  # pgrep: 1 = nessun processo
            return
        time.sleep(0.5)
    # Non sono usciti con le buone: SIGKILL, altrimenti il giro parte sbagliato.
    subprocess.run(["pkill", "-9", "-f", pattern], check=False)
    time.sleep(1)


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


def password_prefs(data: dict) -> None:
    """Spegni il gestore password, sul posto, dentro un dict di Preferences.

    Niente bolla "salvare la password?" sui form di login del sito, niente
    auto-accesso con credenziali memorizzate, niente avvisi di leak. Sono le
    stesse chiavi che usa il toggle nelle impostazioni: forzarle qui vale
    anche dove non arrivano le opzioni di Selenium (browser riagganciato), e
    un eventuale true scritto da un giro precedente viene sovrascritto.
    """
    profilo = data.setdefault("profile", {})
    profilo["password_manager_enabled"] = False
    profilo["password_manager_leak_detection"] = False
    data["credentials_enable_service"] = False
    data["credentials_enable_autosignin"] = False


def prepare_clean_start(profile_dir: Path) -> None:
    """Impedisce a Chrome di ripristinare le finestre del giro precedente.

    Se il processo viene ucciso (o crasha) Chrome al rilancio ripristina la
    sessione, e le vecchie finestre si sommano alle nuove. I pref da soli non
    bastano: gli snapshot di sessione vanno anche rimossi. Sono solo elenchi di
    schede - cookie e login stanno altrove e restano intatti.

    Su profilo nuovo il file Preferences non esiste ancora: lo crea, perche'
    le preferenze del gestore password vanno dentro fin dal primo avvio.
    """
    default = profile_dir / "Default"
    default.mkdir(parents=True, exist_ok=True)
    prefs = default / "Preferences"
    if prefs.is_file():
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        profilo = data.setdefault("profile", {})
        profilo["exit_type"] = "Normal"
        profilo["exited_cleanly"] = True
        # 5 = parti dalla pagina Nuova scheda, non dall'ultima sessione.
        data.setdefault("session", {})["restore_on_startup"] = 5
        data["session"]["startup_urls"] = []
    else:
        data = {}
    password_prefs(data)
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


def version_key(versione: str) -> tuple[int, ...]:
    """Da "152.0.7977.42" a (152, 0, 7977, 42), per confrontare le versioni."""
    try:
        return tuple(int(pezzo) for pezzo in versione.split("."))
    except ValueError:
        return (0,)


def chrome_for_testing_binary(pinned: str = "") -> Path | None:
    """Binario di Chrome for Testing in cache.

    Con ``pinned`` si cerca la versione esatta: se c'e', non si scarica nulla e
    non si cambia mai browser. Senza, il comportamento storico: la versione
    piu' alta presente.

    Selenium Manager lo scarica sotto ~/.cache/selenium/chrome/<piattaforma>/.
    Va puntato esplicitamente: chiedere browser_version = "stable" fa risolvere
    il canale stable *installato*, cioe' /Applications/Google Chrome.app, che da
    Chrome 137 ignora --load-extension. Lo script scaricava quindi la CfT ma
    lanciava il Chrome di sistema, e l'estensione non veniva mai caricata.
    """
    if not SELENIUM_CACHE.is_dir():
        return None
    if pinned:
        # Layout: <piattaforma>/<versione>/Google Chrome for Testing.app/...
        schemi = (
            f"*/{pinned}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            f"*/{pinned}/chrome",  # Linux
        )
        for schema in schemi:
            for percorso in SELENIUM_CACHE.glob(schema):
                return percorso
        return None
    trovati = []
    for schema in (
        "*/*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "*/*/chrome",  # Linux
    ):
        for percorso in SELENIUM_CACHE.glob(schema):
            parti = percorso.relative_to(SELENIUM_CACHE).parts
            if len(parti) >= 2:
                trovati.append((version_key(parti[1]), percorso))
    return max(trovati)[1] if trovati else None


def binary_version(binario: Path) -> str:
    """Versione dichiarata da un binario Chrome ("" se non risponde)."""
    try:
        uscita = subprocess.run(
            [str(binario), "--version"], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for pezzo in uscita.split():
        if pezzo[:1].isdigit():
            return pezzo
    return ""


def download_chrome_for_testing(pinned: str = "") -> Path | None:
    """Scarica Chrome for Testing con Selenium Manager, e ne ritorna il binario.

    Con ``pinned`` si chiede proprio quella versione. Non basta chiedere una
    versione numerica: se quella versione e' anche quella installata sul
    sistema, Selenium Manager restituisce il Chrome di sistema e non scarica
    niente. Serve --force-browser-download, che scarica Chrome for Testing
    anche quando il Chrome installato corrisponde.
    """
    argomenti = ["--browser", "chrome", "--force-browser-download"]
    if pinned:
        argomenti += ["--browser-version", pinned]
    else:
        major = binary_version(Path(SYSTEM_CHROME)).split(".")[0]
        if major:
            argomenti += ["--browser-version", major]
    print("Chrome for Testing non in cache, lo scarico...", flush=True)
    try:
        percorsi = SeleniumManager().binary_paths(argomenti)
    except (WebDriverException, OSError) as exc:
        print(f"Download di Chrome for Testing non riuscito: {exc}", file=sys.stderr)
        return None
    percorso = percorsi.get("browser_path") or ""
    return Path(percorso) if percorso else None


def binario_utilizzabile(binario: Path) -> bool:
    """True se il binario c'e' davvero e risponde a --version.

    Non basta che il percorso esista. Selenium Manager fa pulizia della propria
    cache e puo' portarsi via un'installazione a meta': il glob trova ancora
    l'eseguibile dentro l'.app, ma mancano i Frameworks e l'errore arriva dopo,
    da dentro Selenium, come "Browser path does not exist".
    """
    return binario.is_file() and bool(binary_version(binario))


def use_chrome_for_testing(options: Options, pinned: str = "") -> None:
    """Fa usare a Selenium Chrome for Testing e non il Chrome installato."""
    binario = chrome_for_testing_binary(pinned)
    if binario is not None and not binario_utilizzabile(binario):
        print(f"Chrome for Testing in cache inservibile ({binario}), lo riscarico.", flush=True)
        binario = None
    if binario is None:
        binario = download_chrome_for_testing(pinned)
    if binario is None or not binario_utilizzabile(binario):
        raise SystemExit(
            "Chrome for Testing non disponibile. Svuota la cache e riprova:\n"
            f"  rm -rf {SELENIUM_CACHE}"
        )
    options.binary_location = str(binario)


def debugger_browser_version(port: int) -> str:
    """Versione del browser in ascolto sulla porta ("" se non risponde)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as risposta:
            return json.load(risposta).get("Browser", "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def debugger_binary(port: int) -> Path | None:
    """Eseguibile del browser in ascolto sulla porta ("" se non identificabile)."""
    try:
        pid = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.split()
        if not pid:
            return None
        comando = subprocess.run(
            ["ps", "-o", "comm=", "-p", pid[0]],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(comando) if comando else None


def debugger_is_chrome_for_testing(port: int, pinned: str = "") -> bool:
    """True se sulla porta risponde la Chrome for Testing che useremmo noi.

    Serve perche' un giro precedente puo' aver lasciato aperto il Chrome di
    sistema: riattaccarsi a quello significherebbe un altro giro senza
    estensione, e quindi senza autoplay.

    Il confronto e' sul percorso dell'eseguibile, non sulla versione: da
    Chrome 153 il Chrome di sistema e Chrome for Testing dichiarano lo stesso
    numero di versione, quindi la versione non li distingue piu'.
    """
    binario = chrome_for_testing_binary(pinned)
    if binario is None:
        return True  # non verificabile: meglio non forzare un riavvio inutile
    in_ascolto = debugger_binary(port)
    if in_ascolto is not None:
        return in_ascolto == binario
    # lsof/ps non disponibili: ripiego sulla versione, meglio di niente.
    attesa = binary_version(binario)
    return bool(attesa) and debugger_browser_version(port).endswith(attesa)


def build_driver(
    profile_dir: Path,
    port: int = DEBUG_PORT,
    extension: Path | None = None,
    pinned: str = "",
    headless: bool = False,
) -> tuple[webdriver.Chrome, bool]:
    """Lancia (o riaggancia) il Chrome dedicato a un profilo.

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
        use_chrome_for_testing(options, pinned)

    riusabile = debugger_alive(port)
    if riusabile and extension and not debugger_is_chrome_for_testing(port, pinned):
        print(
            "Sulla porta di debug c'e' il Chrome di sistema, che non carica "
            "l'estensione: lo riavvio con Chrome for Testing.",
            flush=True,
        )
        close_dedicated_chrome(profile_dir)
        riusabile = False

    if riusabile:
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
            use_chrome_for_testing(options, pinned)

    prepare_clean_start(profile_dir)
    # Profilo dedicato: tiene la sessione e non va in conflitto con il Chrome
    # che l'utente ha gia' aperto per i fatti suoi.
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument(f"--remote-debugging-port={port}")
    if headless:
        # Senza finestre: il Mac resta libero e il focus non viene mai rubato
        # dai continui cambi di finestra del listener. I video partono lo
        # stesso (autoplay gia' autorizzato, l'estensione carica anche cosi')
        # e il tracciamento del tempo di visione sta sulla piattaforma.
        options.add_argument("--headless")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    # Portachiavi finto: senza, ogni profilo che tocca password puo' far
    # comparire il prompt di accesso al Portachiavi di sistema, e le password
    # di test finirebbero nel portachiavi vero dell'utente. Ignorato altrove.
    if sys.platform == "darwin":
        options.add_argument("--use-mock-keychain")
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


# Moodle protegge il form con un "logintoken" che invecchia: su una pagina di
# login rimasta aperta (o ereditata dal giro precedente) il primo invio torna
# "La sessione e' scaduta" anche con le credenziali giuste.
SESSIONE_SCADUTA = ("sessione", "session has expired", "token")


def esito_login(driver: webdriver.Chrome, url: str) -> tuple[str, str]:
    """Aspetta l'esito dell'invio: ok / scaduta / rifiutato / timeout."""
    deadline = time.time() + 30
    while time.time() < deadline:
        if "/login/" not in current_url(driver):
            driver.get(url)
            if is_courses_page(driver):
                return "ok", ""
            # Fuori dalla pagina di login gli avvisi non parlano di login:
            # ".alert-danger" e' la classe di qualunque notifica Moodle, e una
            # lezione bloccata da un prerequisito ne mostra una. Presa per un
            # errore di autenticazione, faceva rinunciare a un login riuscito.
            time.sleep(1)
            continue
        for selector in (".loginerrors", "#loginerrormessage", ".alert-danger"):
            errors = driver.find_elements(By.CSS_SELECTOR, selector)
            if errors and errors[0].text.strip():
                messaggio = errors[0].text.strip()
                basso = messaggio.lower()
                if any(spia in basso for spia in SESSIONE_SCADUTA):
                    return "scaduta", messaggio
                return "rifiutato", messaggio
        time.sleep(1)
    return "timeout", ""


def login_page_state(driver: webdriver.Chrome, timeout: float = 10) -> str:
    """Com'e' messa la pagina di login: "form", "dentro" o "vuoto".

    "dentro" copre due casi: l'URL ha lasciato /login/ (rimbalzo verso la
    dashboard, possibile con la page load strategy "eager") o la pagina e'
    l'interstitial "Sei gia' autenticato come ..., Esci" che Moodle mostra
    invece del form quando la sessione e' ancora valida.
    """
    fine = time.time() + timeout
    while time.time() < fine:
        if "/login/" not in current_url(driver):
            return "dentro"
        try:
            if driver.find_elements(By.CSS_SELECTOR, 'input[name="username"]'):
                return "form"
            if driver.find_elements(By.CSS_SELECTOR, 'form[action*="logout.php"]'):
                return "dentro"  # interstitial "gia' autenticato"
        except WebDriverException:
            pass
        time.sleep(0.3)
    return "vuoto"


def auto_login(driver: webdriver.Chrome, username: str, password: str, url: str) -> bool:
    """Compila il form di login Moodle con le credenziali date.

    La password non viene mai stampata ne' registrata: passa dal chiamante
    direttamente al campo del form.
    """
    if "/login/" not in current_url(driver):
        driver.get(LOGIN_URL)
    stato = login_page_state(driver)
    if stato == "dentro":
        return True  # sessione valida: Moodle ha rimbalzato il login
    if stato != "form":
        print("Pagina di login senza form: procedo col login manuale.", flush=True)
        return False

    print("Login automatico in corso...", flush=True)
    for tentativo in (1, 2):
        if not fill_login_form(driver, username, password):
            print("Form di login non compilabile: procedo col login manuale.", flush=True)
            return False

        esito, messaggio = esito_login(driver, url)
        if esito == "ok":
            print("Login automatico riuscito.", flush=True)
            return True
        if esito == "scaduta" and tentativo == 1:
            # Non e' un rifiuto delle credenziali: il form era vecchio. Ne
            # prendiamo uno nuovo, con un token valido, e lo rimandiamo.
            print("Token del form scaduto, ricarico la pagina e riprovo.", flush=True)
            driver.get(LOGIN_URL)
            continue
        if esito == "timeout":
            print("Login automatico non concluso in tempo.", flush=True)
        else:
            print(f"Login rifiutato dal sito: {messaggio}", flush=True)
        return False
    return False


def ensure_logged_in(
    driver: webdriver.Chrome,
    url: str,
    credentials: tuple[str, str] = ("", ""),
    timeout: int = LOGIN_TIMEOUT,
    allow_manual: bool = True,
) -> None:
    """Fa il login con le credenziali date, o aspetta quello manuale.

    Con allow_manual=False (uso da motore) niente attesa: se il login
    automatico fallisce, solleva subito TimeoutException.
    """
    if is_courses_page(driver):
        return

    # Chiediamo la pagina che serve davvero: con la sessione valida ci si
    # arriva direttamente; se e' scaduta Moodle rimbalza sul login col form.
    # Non andiamo mai su LOGIN_URL di nostra iniziativa: da autenticati mostra
    # l'interstitial "Sei gia' autenticato", senza form, e sembrerebbe un
    # login rotto.
    if "/login/" not in current_url(driver):
        driver.get(url)
        if "/login/" not in current_url(driver):
            return

    username, password = credentials
    if username and password and auto_login(driver, username, password, url):
        return

    if not allow_manual:
        raise TimeoutException("Login automatico non riuscito.")

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
        if not title:
            # Vista Elenco: il titolo non sta in uno span suo, e' un nodo di
            # testo dentro il link, insieme a etichette per screen reader
            # ("Titolo del corso", "Il corso e' tra i preferiti") che vanno
            # tolte, altrimenti finiscono nel nome del corso.
            title = driver.execute_script(
                """const copia = arguments[0].cloneNode(true);
                   copia.querySelectorAll('.sr-only, .hidden, [aria-hidden="true"]')
                        .forEach((e) => e.remove());
                   return copia.textContent.trim().replace(/\\s+/g, ' ');""",
                link,
            ) or ""
        seen.add(course_id)
        courses.append(
            {
                "id": course_id,
                "title": title or f"Corso {course_id}",
                "url": urljoin(COURSES_URL, href),
            }
        )
    return courses


def sidebar_present(driver: webdriver.Chrome) -> bool:
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, SIDEBAR_SELECTOR))
    except WebDriverException:
        return False


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


def course_progress(driver: webdriver.Chrome) -> tuple[int, int]:
    """(completate, totali) sulle attivita' tracciate nell'indice del corso.

    Le voci senza stato di completamento (etichette, sezioni) non contano.
    completion_fail conta come fatta: ai fini del rig e' un'attivita' chiusa.
    """
    try:
        risultato = driver.execute_script(
            """
            const items = document.querySelectorAll('#course-index li.courseindex-item');
            let done = 0, tot = 0;
            for (const it of items) {
              if (it.querySelector('.completion_complete, .completion_done, .completion_fail')) { done++; tot++; }
              else if (it.querySelector('.completion_incomplete')) { tot++; }
            }
            return [done, tot];
            """
        )
    except WebDriverException:
        return (0, 0)
    if not isinstance(risultato, list) or len(risultato) != 2:
        return (0, 0)
    return (int(risultato[0]), int(risultato[1]))


def progress_percent(done: int, total: int) -> int:
    return int(round(done * 100 / total)) if total else 0


def is_videotime(href: str, host: str = HOST) -> bool:
    """True se il link punta a un'attivita' videotime dello stesso sito."""
    parsed = urlparse(href)
    return parsed.netloc == host and parsed.path.startswith(VIDEOTIME_PATH)


def classify_course(driver: webdriver.Chrome) -> tuple[str, str | None, tuple[int, int]]:
    """Classifica il corso della pagina corrente.

    Ritorna (stato, href_primo_incompleto, (completate, totali)) con stato:
    - "pending": il primo incompleto e' un video -> il rig lo puo' lavorare;
    - "completed": nessuna attivita' incompleta (e ce ne sono di tracciate);
    - "ai_test": il primo incompleto non e' un video (quiz, dispense, ...);
    - "no_activity": nessuna attivita' tracciata nella sidebar.
    """
    href = first_incomplete_link(driver)
    progresso = course_progress(driver)
    if href is None:
        return ("completed" if progresso[1] else "no_activity"), None, progresso
    if is_videotime(href):
        return "pending", href, progresso
    return "ai_test", href, progresso


def click_link(driver: webdriver.Chrome, href: str) -> None:
    """Clicca il link nella sidebar; se non e' cliccabile ci naviga direttamente."""
    try:
        link = driver.find_element(By.CSS_SELECTOR, f'#course-index a[href="{href}"]')
        driver.execute_script("arguments[0].click();", link)
        WebDriverWait(driver, 20).until(lambda d: VIDEOTIME_PATH in current_url(d))
    except (TimeoutException, WebDriverException):
        driver.get(href)


def video_state(driver: webdriver.Chrome) -> dict | None:
    """Stato del video della pagina corrente, o None se non c'e'/non leggibile.

    Il player e' un iframe cross-origin di player.vimeo.com: WebDriver pero'
    puo' entrarci (il limite same-origin vale per il JavaScript della pagina,
    non per il protocollo di automazione), quindi entriamo nell'iframe e
    leggiamo il <video> direttamente. Fallback: un <video> nativo nella pagina.

    Ritorna {"found": bool, "paused": bool, "ended": bool,
            "current": float, "duration": float}.
    """
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="player.vimeo.com"]')
        dentro_frame = False
        if frames:
            driver.switch_to.frame(frames[0])
            dentro_frame = True
        try:
            return driver.execute_script(
                """
                const v = document.querySelector('video');
                if (!v) return null;
                return {found: true, paused: v.paused, ended: v.ended,
                        current: v.currentTime || 0, duration: v.duration || 0};
                """
            )
        finally:
            if dentro_frame:
                driver.switch_to.default_content()
    except WebDriverException:
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass
        return None


def open_course_in_window(driver: webdriver.Chrome, course_url: str) -> str:
    """Porta la finestra corrente sul corso e sulla prima attivita' da fare.

    Ritorna "videotime" (finestra gia' sul video da vedere), "ai_test",
    "completed", "no_activity" (nessun video da guardare: la finestra va
    chiusa) oppure "unknown" se la sidebar non e' arrivata in tempo.
    """
    try:
        driver.get(course_url)
    except TimeoutException:
        pass  # eager strategy: la pagina c'e', mancano solo i lenti
    if not sidebar_present(driver):
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SIDEBAR_SELECTOR))
            )
        except TimeoutException:
            return "unknown"
    stato, href, _ = classify_course(driver)
    if stato == "pending" and href:
        click_link(driver, href)
        return "videotime"
    return stato


def open_courses(
    driver: webdriver.Chrome,
    courses: list[dict[str, str]],
    reuse_first: bool,
    as_windows: bool = True,
    prune: bool = True,
    keep_anchor: bool = False,
) -> int:
    """Apre ogni corso in una finestra a se' (default) o in una scheda.

    Con prune attivo tiene solo le finestre in cui la prima attivita'
    non completata della sidebar e' un videotime (e la apre), chiudendo le altre.
    Restituisce quante finestre restano aperte.
    """
    target = "window" if as_windows else "tab"
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
        if href and is_videotime(href):
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
