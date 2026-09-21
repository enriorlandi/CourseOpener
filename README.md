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
- macOS, Windows o Linux

## Installazione

macOS e Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Poi le credenziali della piattaforma nel file `.env`:

```
FAD_USERNAME=...
FAD_PASSWORD=...
```

Il file è già in `.gitignore`; su macOS e Linux nasce con permessi `600`. Se non lo compili, lo
script apre il browser e aspetta che il login lo faccia tu a mano.

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
.venv/bin/python course_opener.py          # macOS, Linux
```

```powershell
.venv\Scripts\python course_opener.py      # Windows
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

Una funzione `courseopener` che evita di ricordare le opzioni a memoria, e sa gestire fino a
dieci account in parallelo. Ce ne sono due versioni con gli stessi comandi, lo stesso `.env`,
gli stessi profili e le stesse porte: `courseopener.zsh` per macOS e Linux,
`courseopener.ps1` per Windows.

macOS e Linux — una riga nel proprio `~/.zshrc`:

```bash
echo "source $PWD/courseopener.zsh" >> ~/.zshrc && source ~/.zshrc
```

Windows — una riga nel proprio `$PROFILE` di PowerShell. Il file di profilo spesso non esiste
ancora, e va creato prima (il `-Force` serve per la cartella, e il `Test-Path` che lo precede
non è di troppo: su un profilo già esistente `New-Item -Force` lo svuoterebbe):

```powershell
if (-not (Test-Path $PROFILE)) { New-Item -ItemType File -Path $PROFILE -Force | Out-Null }
Add-Content $PROFILE ". $PWD\courseopener.ps1"; . $PROFILE
```

Se PowerShell rifiuta di caricarlo, la execution policy è troppo stretta:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Entrambi i file ricavano da sé la posizione del repo, quindi non ci sono percorsi da correggere
a mano quando lo si clona su un'altra macchina.

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

Le credenziali arrivano allo script come variabili d'ambiente: non restano nella shell di chi
lancia il comando e non compaiono nella riga di comando, visibile a chiunque elenchi i processi.
Lo zsh le esporta dentro una subshell; PowerShell non ne ha una, quindi `courseopener.ps1` le
mette nell'ambiente del solo processo figlio, via `ProcessStartInfo.EnvironmentVariables`.

I profili sono numerati a due cifre di proposito. Lo script chiude il browser cercando il
percorso del profilo nella riga di comando — con `pkill` su macOS e Linux, con `Win32_Process`
e `taskkill` su Windows — e quel confronto è per sottostringa: con `account1` e `account10`,
chiudere il primo si porterebbe via anche il secondo.

## Come funziona, in breve

- **Browser**: Chrome for Testing, non il Chrome di sistema. Da Chrome 137 il Chrome stable
  rifiuta le estensioni fuori dal Web Store in sessione automatizzata: né `--load-extension`,
  né un `.crx` installato via Selenium, né la scappatoia
  `--disable-features=DisableLoadExtensionCommandLineSwitch` funzionano più. Chrome for Testing
  le carica, viene scaricato da Selenium Manager e resta in cache in `~/.cache/selenium`.
- **Profilo**: `~/.courseopener/chrome-profile-cft`, **fuori** da questa cartella di proposito —
  contiene i cookie di sessione, e la cartella è pensata per essere condivisa.
- **Chiusura del browser**: su macOS e Linux con `pkill -f` sul percorso del profilo, su Windows
  con una query CIM su `Win32_Process` e `taskkill`. In entrambi i casi il confronto sulla riga
  di comando è per sottostringa, da cui i profili numerati a due cifre.
- **Sessione**: lo script prova a riagganciarsi al browser già aperto sulla porta di debug; se
  non è pilotabile (tipico dopo un crash) chiude solo quell'istanza e la rilancia.
- **Finestra àncora**: la finestra con l'elenco corsi resta aperta per tutto il giro e viene
  chiusa alla fine. Senza, un giro che chiude tutte le finestre farebbe terminare il browser
  portandosi via la sessione.

## Se lo condividi

Nello zip non finiscano `.env` (le tue credenziali) e `.venv/`. Il resto — script, estensione,
`requirements.txt` — è autosufficiente e non contiene percorsi assoluti.
