# ---------------------------------------------------------------------------
# CourseOpener — comandi da shell.
#
# Installazione: aggiungi al tuo ~/.zshrc la riga
#
#     source /percorso/del/clone/courseopener.zsh
#
#   courseopener run [N] [opzioni]   giro completo per l'account N (default 1)
#   courseopener stop [N|all]        chiude il browser di N, o tutti
#   courseopener status              quali account sono configurati e attivi
#   courseopener init                aggiunge al .env le righe username mancanti
#
# Ogni account ha profilo e porta propri, quindi più account possono girare in
# contemporanea senza vedersi: profili in ~/.courseopener, porte 9333-9342.
#
# Le credenziali stanno tutte nel .env del repo, perché gli account di test
# condividono la password e cambia solo l'username: FAD_PASSWORD, poi
# FAD_USERNAME per l'account 1 e FAD_USERNAME_02..10 per gli altri.
# Vengono passate allo script come variabili d'ambiente, che nel suo
# read_credentials hanno la precedenza sul contenuto del file.
#
# I nomi dei profili sono a due cifre di proposito. Lo script chiude il browser
# con pkill cercando il percorso del profilo, e quel confronto è per
# sottostringa: con "account1" e "account10" chiudere il primo porterebbe via
# anche il secondo.
# ---------------------------------------------------------------------------

# Cartella di questo file, risolta al momento del source: dentro una funzione
# $0 è il nome della funzione, non quello del file. Così il repo può stare
# ovunque, senza percorsi da correggere a mano su ogni macchina.
COURSEOPENER_REPO="${${(%):-%x}:A:h}"
COURSEOPENER_BASE="$HOME/.courseopener"
COURSEOPENER_CONF="$COURSEOPENER_REPO/.env"
COURSEOPENER_MAX=10

# L'account 1 tiene il profilo storico, così non perde la sessione già aperta.
_courseopener_profilo() {
  if (( $1 == 1 )); then print "$COURSEOPENER_BASE/chrome-profile-cft"
  else printf '%s/account-%02d\n' "$COURSEOPENER_BASE" "$1"; fi
}

_courseopener_porta() { print $(( 9332 + $1 )) }

_courseopener_numero_valido() {
  [[ "$1" == <-> ]] && (( $1 >= 1 && $1 <= COURSEOPENER_MAX ))
}

# Legge una chiave dal file senza fare "source": il file contiene una password,
# non va eseguito.
_courseopener_valore() {
  [[ -f "$1" ]] || return 1
  sed -n "s/^$2=//p" "$1" | head -1
}

_courseopener_utente() {
  local valore="$(_courseopener_valore "$COURSEOPENER_CONF" "$(printf 'FAD_USERNAME_%02d' $1)")"
  # L'account 1 sta sulla chiave senza numero, quella che legge anche lo script.
  if [[ -z "$valore" ]] && (( $1 == 1 )); then
    valore="$(_courseopener_valore "$COURSEOPENER_CONF" FAD_USERNAME)"
  fi
  print "$valore"
}

_courseopener_attivo() {
  curl -s --max-time 1 "http://127.0.0.1:$(_courseopener_porta $1)/json/version" >/dev/null 2>&1
}

courseopener() {
  local py="$COURSEOPENER_REPO/.venv/bin/python"
  local comando="$1"
  [[ -n "$comando" ]] && shift

  case "$comando" in
    run)
      local n=1
      # Il primo argomento è il numero di account solo se è un numero: così
      # "courseopener run --max 5" resta valido e vale per l'account 1.
      if [[ -n "$1" && "$1" == <-> ]]; then n="$1"; shift; fi
      if ! _courseopener_numero_valido "$n"; then
        print -u2 "courseopener: account '$n' fuori intervallo (1-$COURSEOPENER_MAX)"
        return 1
      fi
      if [[ ! -x "$py" ]]; then
        print -u2 "courseopener: manca il virtualenv in $COURSEOPENER_REPO/.venv"
        print -u2 "  crealo con:  python3 -m venv $COURSEOPENER_REPO/.venv && $COURSEOPENER_REPO/.venv/bin/pip install -r $COURSEOPENER_REPO/requirements.txt"
        return 1
      fi
      if [[ ! -f "$COURSEOPENER_CONF" ]]; then
        print -u2 "courseopener: manca $COURSEOPENER_CONF — crealo con 'courseopener init'"
        return 1
      fi
      local utente="$(_courseopener_utente $n)"
      local password="$(_courseopener_valore "$COURSEOPENER_CONF" FAD_PASSWORD)"
      if [[ -z "$utente" ]]; then
        print -u2 "courseopener: nessun username per l'account $n in $COURSEOPENER_CONF"
        print -u2 "  compila $(printf 'FAD_USERNAME_%02d' $n)"
        return 1
      fi
      if [[ -z "$password" ]]; then
        print -u2 "courseopener: FAD_PASSWORD vuota in $COURSEOPENER_CONF"
        return 1
      fi
      print "courseopener: account $n ($utente) sulla porta $(_courseopener_porta $n)"
      # Subshell: le credenziali non restano nella shell dell'utente, e non
      # finiscono nella riga di comando (visibile con ps).
      (
        export FAD_USERNAME="$utente" FAD_PASSWORD="$password"
        "$py" "$COURSEOPENER_REPO/course_opener.py" \
          --profile "$(_courseopener_profilo $n)" \
          --port "$(_courseopener_porta $n)" "$@"
      )
      ;;

    stop)
      local -a da_chiudere
      if [[ -z "$1" ]]; then da_chiudere=(1)
      elif [[ "$1" == all ]]; then da_chiudere=({1..$COURSEOPENER_MAX})
      elif _courseopener_numero_valido "$1"; then da_chiudere=("$1")
      else
        print -u2 "courseopener: uso 'courseopener stop [N|all]'"
        return 1
      fi
      local n chiusi=0
      for n in $da_chiudere; do
        if pkill -f "user-data-dir=$(_courseopener_profilo $n)" 2>/dev/null; then
          print "  account $n: chiuso"
          (( chiusi++ ))
        fi
      done
      (( chiusi == 0 )) && print "  nessun browser da chiudere"
      return 0
      ;;

    status)
      if [[ ! -f "$COURSEOPENER_CONF" ]]; then
        print "nessuna configurazione: lancia 'courseopener init'"
        return 0
      fi
      local n utente configurati=0
      printf '  %-8s %-7s %-26s %s\n' account porta username stato
      for n in {1..$COURSEOPENER_MAX}; do
        utente="$(_courseopener_utente $n)"
        [[ -z "$utente" ]] && continue
        (( configurati++ ))
        printf '  %-8s %-7s %-26s %s\n' "$n" "$(_courseopener_porta $n)" "$utente" \
          "$(_courseopener_attivo $n && print 'in esecuzione' || print fermo)"
      done
      (( configurati == 0 )) && print "  nessun username compilato in $COURSEOPENER_CONF"
      return 0
      ;;

    init)
      # Aggiunge al .env le righe FAD_USERNAME_NN che mancano, senza toccare
      # quelle gia' compilate ne' la password.
      if [[ ! -f "$COURSEOPENER_CONF" ]]; then
        mkdir -p "${COURSEOPENER_CONF:h}"
        {
          print "# Credenziali degli account di test su https://fad-for-me.formretail.it"
          print "# La password e' la stessa per tutti: cambia solo l'username."
          print "FAD_PASSWORD="
          print "FAD_USERNAME="
        } > "$COURSEOPENER_CONF"
        chmod 600 "$COURSEOPENER_CONF"
        print "creato $COURSEOPENER_CONF (permessi 600)"
      fi
      # Se il file non finisce con un a capo, la prima riga aggiunta si
      # incollerebbe all'ultima esistente: con la password in fondo, la
      # renderebbe illeggibile. $(...) toglie gli a capo finali, quindi qui
      # "non vuoto" significa "l'ultimo carattere non e' un a capo".
      if [[ -s "$COURSEOPENER_CONF" && -n "$(tail -c 1 "$COURSEOPENER_CONF")" ]]; then
        print "" >> "$COURSEOPENER_CONF"
      fi
      local i chiave aggiunte=0
      for i in {2..$COURSEOPENER_MAX}; do
        chiave="$(printf 'FAD_USERNAME_%02d' $i)"
        if ! grep -q "^$chiave=" "$COURSEOPENER_CONF"; then
          print "$chiave=" >> "$COURSEOPENER_CONF"
          (( aggiunte++ ))
        fi
      done
      if (( aggiunte > 0 )); then
        print "aggiunte $aggiunte righe username a $COURSEOPENER_CONF"
      else
        print "$COURSEOPENER_CONF ha gia' tutte le righe username"
      fi
      print "compilale, poi:  courseopener status"
      ;;

    ""|-h|--help|help)
      print "uso: courseopener <comando>"
      print "  run [N] [opzioni]   giro completo per l'account N (default 1)"
      print "  stop [N|all]        chiude il browser di N, o tutti"
      print "  status              account configurati e loro stato"
      print "  init                aggiunge al .env le righe username mancanti"
      print ""
      print "opzioni utili dopo run: --dry-run, --max N, --tabs, --filter TESTO"
      [[ -n "$comando" ]]
      ;;

    *)
      print -u2 "courseopener: comando sconosciuto '$comando' (run, stop, status, init, help)"
      return 1
      ;;
  esac
}
