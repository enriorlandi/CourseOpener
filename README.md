# CourseOpener

Due strumenti per collaudare la piattaforma [FAD ForMe](https://fad-for-me.formretail.it):

- **`python -m fadplatform`** — il rig di test completo: UI web, più utenti di test,
  N corsi in simultanea, listener anti-blocco, chiusura automatica sui test.
- **`course_opener.py`** — lo script storico: apre una finestra per ogni corso il cui
  prossimo passo è un video. Comodo per un giro veloce su un account.

## Il rig di test (`fadplatform`)

Si avvia con:

```bash
.venv/bin/python -m fadplatform
```

e apre la UI su http://127.0.0.1:8788 (solo locale). Da lì si fa tutto:

- **Utenti di test**: aggiunti a mano (username + password, nessun controllo su
  lunghezza o caratteri: sono credenziali di collaudo) o importati da un **CSV a due
  colonne** (`username,password`; accetta anche `;` o tabulazione come separatore, e
  salta l'eventuale riga d'intestazione).
- **Corsi in simultanea**: quanti corsi tenere aperti in totale, modificabile anche
  a motore acceso (0 = sola scansione).
- **Avvio**: il rig parte dal primo utente della lista e va in ordine. Prima scansiona
  i corsi di ogni utente (titoli, stati, percentuali), poi apre finestre finché lo
  slot lo consente. Quando un corso finisce, lo slot passa al prossimo della coda —
  dello stesso utente riusando la finestra, di un altro utente aprendo un'istanza
  Chrome separata con profilo e porta propri (login incluso).
- **Obiettivo**: portare tutti i corsi di tutti gli utenti al 100%. Quando un corso
  arriva alla parte di test (risposta multipla), la finestra si chiude e il corso
  resta marcato "fermo ai test": i test non vengono mai compilati.
- **Memoria**: tutto lo stato vive in `~/.courseopener/state.json` — chiuso il
  software e riaperto, utenti, progressi e coda riprendono da dove erano. Il file
  contiene le password di test ed è a permessi 600.

Il **listener anti-blocco** controlla ogni finestra e fa un refresh della pagina in
tre casi: il video è in pausa da un po', è in riproduzione ma il tempo non scorre,
oppure è finito e non è passato al successivo. Dopo troppi refresh a vuoto il rig
rilegge l'indice del corso e decide se chiudere la finestra. Dalla UI si vede lo
stato del video di ogni finestra (posizione, durata, pausa/finito).

La **versione di Chrome for Testing è fissata** (impostazione "Versione Chrome for
Testing" nella UI, default `153.0.8010.52`, quella già in cache): niente download a
ogni avvio. Se la svuoti o ne metti un'altra, il rig la scarica una volta sola.
Lasciando il campo vuoto torna il comportamento automatico (la più alta in cache).

Ogni utente ha il suo profilo in `~/.courseopener/profiles/<slug>`: sessioni
indipendenti, più utenti insieme senza pestarsi i piedi.

## Lo script storico (`course_opener.py`)

Apre una finestra di Chrome per ogni corso della propria pagina
[I miei corsi](https://fad-for-me.formretail.it/my/courses.php),
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
```Poi le credenziali della piattaforma nel file `.env`:

```
FAD_USERNAME=...
FAD_PASSWORD=...
```

Il file è già in `.gitignore` e nasce con permessi `600`. Se non lo compili, lo script apre il
browser e aspetta che il login lo faccia tu a mano.

Per più account di test, che condividono la password e differiscono solo nell'username, lo
stesso file accetta altre righe numerate — le usa `courseopener.zsh`, non lo script:

```
FAD_PASSWORD=...
FAD_USERNAME=...        # account 1
FAD_USERNAME_02=...     # account 2
FAD_USERNAME_03=...     # e così via, fino a FAD_USERNAME_10
```

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

## Comandi da shell

`courseopener.zsh` definisce una funzione `courseopener` che evita di ricordare le opzioni a
memoria, e sa gestire fino a dieci account in parallelo. Per installarla, una riga nel proprio
`~/.zshrc`:

```bash
echo "source $PWD/courseopener.zsh" >> ~/.zshrc && source ~/.zshrc
```

Il file ricava da sé la posizione del repo, quindi non ci sono percorsi da correggere a mano
quando lo si clona su un'altra macchina.

| Comando | Effetto |
| --- | --- |
| `courseopener run` | giro completo sull'account 1 |
| `courseopener run 3` | giro completo sull'account 3 |
| `courseopener run 3 --max 5` | dopo il numero, le opzioni dello script |
| `courseopener stop 3` | chiude il browser di quell'account |
| `courseopener stop all` | chiude tutti |
| `courseopener status` | account compilati, porta e se stanno girando |
| `courseopener init` | aggiunge al `.env` le righe username mancanti |

Ogni account ha profilo e porta propri — `~/.courseopener/account-NN` e porte da 9333 a 9342 —
quindi più account possono girare insieme senza vedersi: i cookie stanno nel profilo, e profili
diversi significano sessioni diverse. L'account 1 tiene il profilo storico
`chrome-profile-cft`.

Le credenziali arrivano allo script come variabili d'ambiente, esportate dentro una subshell:
non restano nella shell di chi lancia il comando e non compaiono nella riga di comando visibile
con `ps`.

I profili sono numerati a due cifre di proposito. Lo script chiude il browser con `pkill`
cercando il percorso del profilo, e quel confronto è per sottostringa: con `account1` e
`account10`, chiudere il primo si porterebbe via anche il secondo.

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
