"""Avvio del rig: ``.venv/bin/python -m fadplatform``.

Serve la UI su localhost e tiene lo stato in ~/.courseopener/state.json.
Ctrl-C (o un segnale di terminazione) ferma il server e il motore: i browser
di test vengono chiusi e lo stato resta salvato su disco per la prossima
esecuzione.
"""

from __future__ import annotations

import argparse
import signal
import threading
import webbrowser

from werkzeug.serving import make_server

from .engine import Engine, SHUTDOWN_GRACE_S
from .server import create_app
from .state import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="interfaccia su cui servire la UI")
    parser.add_argument("--port", type=int, default=8788, help="porta della UI (default 8788)")
    parser.add_argument("--no-open", action="store_true", help="non aprire il browser sulla UI")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Chrome senza finestre: niente focus rubato al Mac, i video girano lo stesso",
    )
    args = parser.parse_args()

    store = Store()
    engine = Engine(store, headless=args.headless)
    app = create_app(store, engine)
    server = make_server(args.host, args.port, app, threaded=True)

    def spegni(_sig=None, _frame=None):
        engine.stop()
        # shutdown() blocca finche' serve_forever non torna: dal gestore del
        # segnale, che interrompe proprio serve_forever, andrebbe in deadlock.
        # Da un thread suo il server esce e il main prosegue fino al join.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, spegni)
    signal.signal(signal.SIGTERM, spegni)

    if not args.no_open:
        webbrowser.open(f"http://{args.host}:{args.port}/")

    try:
        server.serve_forever()
    finally:
        engine.stop()
        if engine.thread is not None:
            engine.thread.join(timeout=SHUTDOWN_GRACE_S + 15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
