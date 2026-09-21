# ---------------------------------------------------------------------------
# CourseOpener - comandi da shell, versione Windows.
#
# E' l'equivalente di courseopener.zsh: stessi comandi, stesso .env, stessi
# profili e stesse porte, cosi' chi passa da una macchina all'altra non deve
# reimparare niente.
#
# Installazione: aggiungi al tuo $PROFILE la riga
#
#     . C:\percorso\del\clone\courseopener.ps1
#
#   courseopener run [N] [opzioni]   giro completo per l'account N (default 1)
#   courseopener stop [N|all]        chiude il browser di N, o tutti
#   courseopener status              quali account sono configurati e attivi
#   courseopener init                aggiunge al .env le righe username mancanti
#
# Ogni account ha profilo e porta propri, quindi piu' account possono girare in
# contemporanea senza vedersi: profili in ~/.courseopener, porte 9333-9342.
#
# I nomi dei profili sono a due cifre di proposito. Lo script chiude il browser
# cercando il percorso del profilo nella riga di comando, e quel confronto e'
# per sottostringa: con "account1" e "account10" chiudere il primo porterebbe
# via anche il secondo.
# ---------------------------------------------------------------------------

# Cartella di questo file, risolta al momento del dot-source: cosi' il repo puo'
# stare ovunque, senza percorsi da correggere a mano su ogni macchina.
$Global:CourseOpenerRepo = $PSScriptRoot
$Global:CourseOpenerBase = Join-Path $HOME ".courseopener"
$Global:CourseOpenerConf = Join-Path $PSScriptRoot ".env"
$Global:CourseOpenerMax  = 10

# L'account 1 tiene il profilo storico, cosi' non perde la sessione gia' aperta.
function _CourseOpener_Profilo {
    param([int]$N)
    if ($N -eq 1) { return (Join-Path $Global:CourseOpenerBase "chrome-profile-cft") }
    return (Join-Path $Global:CourseOpenerBase ("account-{0:D2}" -f $N))
}

function _CourseOpener_Porta { param([int]$N) return (9332 + $N) }

function _CourseOpener_NumeroValido {
    param($Valore)
    if ($Valore -notmatch '^\d+$') { return $false }
    return ([int]$Valore -ge 1 -and [int]$Valore -le $Global:CourseOpenerMax)
}

function _CourseOpener_Errore {
    param([string]$Messaggio)
    $Host.UI.WriteErrorLine($Messaggio)
}

# Legge una chiave dal file senza eseguirlo: il file contiene una password.
function _CourseOpener_Valore {
    param([string]$File, [string]$Chiave)
    if (-not (Test-Path -LiteralPath $File)) { return "" }
    $schema = "^" + [regex]::Escape($Chiave) + "=(.*)$"
    foreach ($riga in [System.IO.File]::ReadAllLines($File)) {
        if ($riga -match $schema) { return $Matches[1].Trim() }
    }
    return ""
}

function _CourseOpener_Utente {
    param([int]$N)
    $valore = _CourseOpener_Valore $Global:CourseOpenerConf ("FAD_USERNAME_{0:D2}" -f $N)
    # L'account 1 sta sulla chiave senza numero, quella che legge anche lo script.
    if (-not $valore -and $N -eq 1) {
        $valore = _CourseOpener_Valore $Global:CourseOpenerConf "FAD_USERNAME"
    }
    return $valore
}

function _CourseOpener_Attivo {
    param([int]$N)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $attesa = $client.BeginConnect("127.0.0.1", (_CourseOpener_Porta $N), $null, $null)
        if (-not $attesa.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($attesa)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# Quoting alla CommandLineToArgvW: le barre rovesciate che precedono un apice
# vanno raddoppiate, e cosi' quelle in fondo all'argomento. Senza, un percorso
# di profilo con uno spazio romperebbe la riga di comando.
function _CourseOpener_Quota {
    param([string]$Argomento)
    if ($Argomento -ne "" -and $Argomento -notmatch '[\s"]') { return $Argomento }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $barre = 0
    foreach ($c in $Argomento.ToCharArray()) {
        if ($c -eq '\') { $barre++; continue }
        if ($c -eq '"') {
            [void]$sb.Append('\' * ($barre * 2 + 1))
        } elseif ($barre -gt 0) {
            [void]$sb.Append('\' * $barre)
        }
        $barre = 0
        [void]$sb.Append($c)
    }
    [void]$sb.Append('\' * ($barre * 2))
    [void]$sb.Append('"')
    return $sb.ToString()
}

# Equivalente della subshell dello zsh: le credenziali finiscono nell'ambiente
# del solo processo figlio, non in quello della shell di chi lancia il comando,
# e non compaiono nella riga di comando, visibile a chiunque elenchi i processi.
function _CourseOpener_Esegui {
    param([string]$Eseguibile, [string[]]$Argomenti, [hashtable]$Ambiente)
    $avvio = New-Object System.Diagnostics.ProcessStartInfo
    $avvio.FileName = $Eseguibile
    $avvio.Arguments = (($Argomenti | ForEach-Object { _CourseOpener_Quota $_ }) -join ' ')
    # Niente shell e niente redirezioni: il figlio eredita la console, quindi
    # l'output dello script si vede mentre scorre.
    $avvio.UseShellExecute = $false
    foreach ($chiave in $Ambiente.Keys) {
        $avvio.EnvironmentVariables[$chiave] = $Ambiente[$chiave]
    }
    $processo = [System.Diagnostics.Process]::Start($avvio)
    $processo.WaitForExit()
    return $processo.ExitCode
}

function _CourseOpener_Chiudi {
    param([string]$Profilo)
    $schema = "user-data-dir=$Profilo"
    $processi = @(
        Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($schema) } |
            ForEach-Object { $_.ProcessId }
    )
    foreach ($idProcesso in $processi) {
        # Senza /F taskkill manda WM_CLOSE: e' il SIGTERM di pkill, Chrome fa in
        # tempo a chiudere la sessione pulita.
        & taskkill /PID $idProcesso | Out-Null
    }
    return $processi.Count
}

# Windows PowerShell 5.1 scrive il BOM con -Encoding utf8, e il parser .env di
# course_opener.py se lo ritroverebbe attaccato alla prima chiave.
function _CourseOpener_ScriviSenzaBom {
    param([string]$File, [string[]]$Righe, [switch]$Aggiungi)
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $testo = ($Righe -join [Environment]::NewLine) + [Environment]::NewLine
    if ($Aggiungi) { [System.IO.File]::AppendAllText($File, $testo, $utf8) }
    else { [System.IO.File]::WriteAllText($File, $testo, $utf8) }
}

function courseopener {
    $python = Join-Path $Global:CourseOpenerRepo ".venv\Scripts\python.exe"
    $comando = ""
    $resto = @()
    if ($args.Count -gt 0) {
        $comando = [string]$args[0]
        if ($args.Count -gt 1) { $resto = @($args[1..($args.Count - 1)]) }
    }

    switch ($comando) {

        "run" {
            $n = 1
            # Il primo argomento e' il numero di account solo se e' un numero:
            # cosi' "courseopener run --max 5" resta valido e vale per l'account 1.
            if ($resto.Count -gt 0 -and [string]$resto[0] -match '^\d+$') {
                $n = [int]$resto[0]
                if ($resto.Count -gt 1) { $resto = @($resto[1..($resto.Count - 1)]) } else { $resto = @() }
            }
            if (-not (_CourseOpener_NumeroValido $n)) {
                _CourseOpener_Errore "courseopener: account '$n' fuori intervallo (1-$($Global:CourseOpenerMax))"
                return
            }
            if (-not (Test-Path -LiteralPath $python)) {
                _CourseOpener_Errore "courseopener: manca il virtualenv in $($Global:CourseOpenerRepo)\.venv"
                _CourseOpener_Errore "  crealo con:  python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
                return
            }
            if (-not (Test-Path -LiteralPath $Global:CourseOpenerConf)) {
                _CourseOpener_Errore "courseopener: manca $($Global:CourseOpenerConf) - crealo con 'courseopener init'"
                return
            }
            $utente = _CourseOpener_Utente $n
            $password = _CourseOpener_Valore $Global:CourseOpenerConf "FAD_PASSWORD"
            if (-not $utente) {
                _CourseOpener_Errore "courseopener: nessun username per l'account $n in $($Global:CourseOpenerConf)"
                _CourseOpener_Errore ("  compila " + ("FAD_USERNAME_{0:D2}" -f $n))
                return
            }
            if (-not $password) {
                _CourseOpener_Errore "courseopener: FAD_PASSWORD vuota in $($Global:CourseOpenerConf)"
                return
            }
            $porta = _CourseOpener_Porta $n
            Write-Host "courseopener: account $n ($utente) sulla porta $porta"
            $argomenti = @(
                (Join-Path $Global:CourseOpenerRepo "course_opener.py"),
                "--profile", (_CourseOpener_Profilo $n),
                "--port", "$porta"
            ) + $resto
            _CourseOpener_Esegui $python $argomenti @{
                FAD_USERNAME = $utente
                FAD_PASSWORD = $password
            } | Out-Null
        }

        "stop" {
            $daChiudere = @()
            if ($resto.Count -eq 0) {
                $daChiudere = @(1)
            } elseif ([string]$resto[0] -eq "all") {
                $daChiudere = 1..$Global:CourseOpenerMax
            } elseif (_CourseOpener_NumeroValido $resto[0]) {
                $daChiudere = @([int]$resto[0])
            } else {
                _CourseOpener_Errore "courseopener: uso 'courseopener stop [N|all]'"
                return
            }
            $chiusi = 0
            foreach ($n in $daChiudere) {
                if ((_CourseOpener_Chiudi (_CourseOpener_Profilo $n)) -gt 0) {
                    Write-Host "  account ${n}: chiuso"
                    $chiusi++
                }
            }
            if ($chiusi -eq 0) { Write-Host "  nessun browser da chiudere" }
        }

        "status" {
            if (-not (Test-Path -LiteralPath $Global:CourseOpenerConf)) {
                Write-Host "nessuna configurazione: lancia 'courseopener init'"
                return
            }
            $formato = "  {0,-8} {1,-7} {2,-26} {3}"
            Write-Host ($formato -f "account", "porta", "username", "stato")
            $configurati = 0
            foreach ($n in 1..$Global:CourseOpenerMax) {
                $utente = _CourseOpener_Utente $n
                if (-not $utente) { continue }
                $configurati++
                if (_CourseOpener_Attivo $n) { $stato = "in esecuzione" } else { $stato = "fermo" }
                Write-Host ($formato -f $n, (_CourseOpener_Porta $n), $utente, $stato)
            }
            if ($configurati -eq 0) {
                Write-Host "  nessun username compilato in $($Global:CourseOpenerConf)"
            }
        }

        "init" {
            # Aggiunge al .env le righe FAD_USERNAME_NN che mancano, senza
            # toccare quelle gia' compilate ne' la password.
            if (-not (Test-Path -LiteralPath $Global:CourseOpenerConf)) {
                $cartella = Split-Path -Parent $Global:CourseOpenerConf
                if (-not (Test-Path -LiteralPath $cartella)) {
                    New-Item -ItemType Directory -Path $cartella -Force | Out-Null
                }
                _CourseOpener_ScriviSenzaBom $Global:CourseOpenerConf @(
                    "# Credenziali degli account di test su https://fad-for-me.formretail.it",
                    "# La password e' la stessa per tutti: cambia solo l'username.",
                    "FAD_PASSWORD=",
                    "FAD_USERNAME="
                )
                # Equivalente del chmod 600: tolta l'ereditarieta', accesso al
                # solo utente corrente. Per SID, che non dipende dalla lingua di
                # Windows ne' da come si chiama l'account.
                $sid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
                & icacls $Global:CourseOpenerConf /inheritance:r /grant:r "*${sid}:(F)" | Out-Null
                Write-Host "creato $($Global:CourseOpenerConf) (accesso al solo utente corrente)"
            }
            # Se il file non finisce con un a capo, la prima riga aggiunta si
            # incollerebbe all'ultima esistente: con la password in fondo, la
            # renderebbe illeggibile.
            $contenuto = [System.IO.File]::ReadAllText($Global:CourseOpenerConf)
            if ($contenuto.Length -gt 0 -and $contenuto[-1] -ne "`n") {
                [System.IO.File]::AppendAllText(
                    $Global:CourseOpenerConf,
                    [Environment]::NewLine,
                    (New-Object System.Text.UTF8Encoding($false))
                )
            }
            $aggiunte = 0
            foreach ($i in 2..$Global:CourseOpenerMax) {
                $chiave = "FAD_USERNAME_{0:D2}" -f $i
                if ($contenuto -notmatch ("(?m)^" + [regex]::Escape($chiave) + "=")) {
                    _CourseOpener_ScriviSenzaBom $Global:CourseOpenerConf @("$chiave=") -Aggiungi
                    $aggiunte++
                }
            }
            if ($aggiunte -gt 0) {
                Write-Host "aggiunte $aggiunte righe username a $($Global:CourseOpenerConf)"
            } else {
                Write-Host "$($Global:CourseOpenerConf) ha gia' tutte le righe username"
            }
            Write-Host "compilale, poi:  courseopener status"
        }

        { $_ -in @("", "-h", "--help", "help") } {
            Write-Host "uso: courseopener <comando>"
            Write-Host "  run [N] [opzioni]   giro completo per l'account N (default 1)"
            Write-Host "  stop [N|all]        chiude il browser di N, o tutti"
            Write-Host "  status              account configurati e loro stato"
            Write-Host "  init                aggiunge al .env le righe username mancanti"
            Write-Host ""
            Write-Host "opzioni utili dopo run: --dry-run, --max N, --tabs, --filter TESTO"
        }

        default {
            _CourseOpener_Errore "courseopener: comando sconosciuto '$comando' (run, stop, status, init, help)"
        }
    }
}
