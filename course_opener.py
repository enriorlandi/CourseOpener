#!/usr/bin/env python3
"""Apre una finestra di Chrome con una scheda per ogni corso presente in
https://fad-for-me.formretail.it/my/courses.php

Usa un profilo Chrome dedicato, separato da quello personale. Le credenziali
si mettono nel file .env accanto allo script (FAD_USERNAME / FAD_PASSWORD):
le legge solo questo processo e vengono digitate direttamente nel form di
login del sito. Senza .env lo script aspetta che il login lo faccia l'utente
a mano nella finestra aperta.

La logica browser/piattaforma vive in fadplatform/moodle.py: qui restano solo
la riga di comando e il giro "apri tutto" del caso d'uso storico. Il rig di
test completo (piu utenti, slot in simultanea, listener anti-blocco) e'
``python -m fadplatform``.

Uso:
    python3 course_opener.py                 # apre tutte le schede
    python3 course_opener.py --dry-run       # elenca i corsi senza aprire schede
    python3 course_opener.py --max 5         # apre solo i primi 5
    python3 course_opener.py --filter team   # solo i corsi il cui titolo contiene "team"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from selenium.common.exceptions import TimeoutException

from fadplatform import moodle
from fadplatform.moodle import (  # noqa: F401 - usati da chi importa questo modulo
    COURSES_URL,
    DEFAULT_ENV_FILE,
    DEFAULT_EXTENSION,
    DEFAULT_PROFILE,
    DEBUG_PORT,
    auto_login,
    build_driver,
    click_link,
    close_dedicated_chrome,
    collect_courses,
    current_url,
    ensure_logged_in,
    esito_login,
    fill_login_form,
    first_incomplete_link,
    is_courses_page,
    is_videotime,
    open_courses,
    read_credentials,
    show_all_courses,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=moodle.COURSES_URL, help="pagina elenco corsi")
    parser.add_argument("--profile", type=Path, default=moodle.DEFAULT_PROFILE, help="cartella profilo Chrome dedicato")
    parser.add_argument("--port", type=int, default=moodle.DEBUG_PORT, help="porta di debug del Chrome dedicato")
    parser.add_argument("--env-file", type=Path, default=moodle.DEFAULT_ENV_FILE, help="file con FAD_USERNAME/FAD_PASSWORD")
    parser.add_argument("--extension", type=Path, default=moodle.DEFAULT_EXTENSION, help="cartella dell'estensione da caricare")
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
        if args.profile == moodle.DEFAULT_PROFILE:
            args.profile = moodle.DEFAULT_PROFILE.with_name(moodle.DEFAULT_PROFILE.name + "-cft")
        print(f"Estensione: {extension} (Chrome for Testing)", flush=True)

    credentials = ("", "") if args.no_auto_login else moodle.read_credentials(args.env_file)
    if not args.no_auto_login and not all(credentials):
        print(
            f"Nessuna credenziale in {args.env_file}: il login andra' fatto a mano.",
            flush=True,
        )

    args.profile.mkdir(parents=True, exist_ok=True)
    driver, reused = moodle.build_driver(args.profile, args.port, extension)

    try:
        if reused:
            # Chrome dedicato gia' aperto: lavoriamo in una finestra nuova per
            # non toccare le schede del lancio precedente.
            print("Riuso la finestra di Chrome gia' aperta.", flush=True)
            driver.switch_to.new_window("window")
        if extension:
            moodle.ensure_developer_mode(driver)
        driver.get(args.url)
        moodle.ensure_logged_in(driver, args.url, credentials)
        if "/my/courses.php" not in moodle.current_url(driver):
            driver.get(args.url)

        courses = moodle.collect_courses(driver)
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

        rimaste = moodle.open_courses(
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
