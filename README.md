# CourseOpener

Apre una finestra di Chrome per ogni corso della propria pagina
[I miei corsi](https://fad-for-me.formretail.it/my/courses.php) sulla piattaforma FAD ForMe,
tenendo aperte solo quelle in cui la prossima cosa da fare è un video.

Per ogni corso lo script guarda l'indice laterale, trova la **prima attività non completata**
e decide:

- punta a `/mod/videotime/...` → la apre e lascia la finestra aperta;
- qualsiasi altra cosa (quiz, dispense, ...) → chiude la finestra.

Nelle finestre superstiti viene caricata l'estensione [VideoGo](VideoGo/), che fa partire il
video da sola.

## Requisiti

- Python 3.10+
- Google Chrome installato (serve come riferimento di versione; il browser effettivamente
  usato è Chrome for Testing, scaricato da solo al primo avvio)

## Installazione

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Poi le credenziali della piattaforma nel file `.env`:

```
FAD_USERNAME=...
FAD_PASSWORD=...
```

Il file è già in `.gitignore` e nasce con permessi `600`. Se non lo compili, lo script apre il
browser e aspetta che il login lo faccia tu a mano.

## Uso

```bash
.venv/bin/python course_opener.py
```

| Opzione | Effetto |
| --- | --- |
| `--max N` | si ferma ai primi N corsi (comodo per le prove) |
| `--filter TESTO` | solo i corsi il cui titolo contiene TESTO |
| `--dry-run` | elenca i corsi e basta, non apre niente |
| `--no-prune` | apre tutti i corsi senza chiudere quelli senza video |
| `--tabs` | schede di un'unica finestra invece di una finestra per corso |
| `--no-extension` | usa il Chrome di sistema, senza estensione |
| `--extension PATH` | carica un'altra estensione |
| `--no-auto-login` | ignora il `.env` e fa fare il login a mano |
| `--profile PATH` | cartella del profilo browser |
| `--port N` | porta di debug (default 9333) |

## Come funziona, in breve

- **Browser**: Chrome for Testing, non il Chrome di sistema. Da Chrome 137 il Chrome stable
  rifiuta le estensioni fuori dal Web Store in sessione automatizzata: né `--load-extension`,
  né un `.crx` installato via Selenium, né la scappatoia
  `--disable-features=DisableLoadExtensionCommandLineSwitch` funzionano più. Chrome for Testing
  le carica, viene scaricato da Selenium Manager e resta in cache in `~/.cache/selenium`.
- **Profilo**: `~/.courseopener/chrome-profile-cft`, **fuori** da questa cartella di proposito —
  contiene i cookie di sessione, e la cartella è pensata per essere condivisa.
- **Sessione**: lo script prova a riagganciarsi al browser già aperto sulla porta di debug; se
  non è pilotabile (tipico dopo un crash) chiude solo quell'istanza e la rilancia.
- **Finestra àncora**: la finestra con l'elenco corsi resta aperta per tutto il giro e viene
  chiusa alla fine. Senza, un giro che chiude tutte le finestre farebbe terminare il browser
  portandosi via la sessione.

## Se lo condividi

Nello zip non finiscano `.env` (le tue credenziali) e `.venv/`. Il resto — script, estensione,
`requirements.txt` — è autosufficiente e non contiene percorsi assoluti.
