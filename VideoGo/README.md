# VideoGo

Estensione Chrome che fa partire il video al caricamento della pagina su
`https://fad-for-me.formretail.it/` (piattaforma Moodle).

## Installazione

1. Apri `chrome://extensions`
2. Attiva **Modalità sviluppatore** (in alto a destra)
3. **Carica estensione non pacchettizzata** → seleziona questa cartella
4. Apri (o ricarica) una pagina della piattaforma FAD

Dopo ogni modifica ai file va premuto **↻ Ricarica** sulla scheda dell'estensione
in `chrome://extensions`: il manifest viene riletto solo lì.

## Com'è fatta la pagina, e perché conta

Le lezioni usano il modulo Moodle `mod_videotime`. Nel DOM della pagina **non
c'è nessun `<video>`**: il player è un iframe di `player.vimeo.com`, cioè un
dominio diverso, dentro il quale un'estensione con permessi sul solo dominio FAD
non può entrare.

Due valori della configurazione del modulo spiegano tutto il resto:

- `"autoplay":"0"` — l'autoplay è disattivato lato server, ed è il motivo per cui
  dopo un reload il video resta fermo
- `"next_activity_auto":"1"` — è la piattaforma ad avanzare da sola alla lezione
  successiva, quindi l'estensione non tocca quel meccanismo

L'iframe dichiara già `allow="autoplay; fullscreen; ..."`: il permesso c'è,
manca solo il comando.

## Cosa fa

Manda al player il comando `play` tramite l'**API postMessage di Vimeo**, la
stessa che usa VideoTime.

Quello che **non** fa è riscrivere l'`src` dell'iframe aggiungendo `autoplay=1`:
sarebbe la scorciatoia ovvia, ma ricaricherebbe il player e azzererebbe il
conteggio del tempo di visione — che è esattamente ciò che determina il
completamento della lezione (`completion_on_view_time`). Un `play` via
postMessage è invece indistinguibile da un clic sul pulsante, e il tracciamento
prosegue intatto.

Sequenza per ogni player trovato:

1. attesa di 1,2 s, per lasciare a VideoTime il tempo di inizializzare il player
   e di riprendere dal punto giusto (`resume_playback`)
2. `setVolume` al volume d'avvio (10% di default), poi `play`
3. `getPaused` per sapere se il comando ha fatto effetto — lo stato non è dedotto
   ma chiesto al player
4. dopo ~4 s senza risultato, se **Muta come ultima spiaggia** è attivo,
   `setVolume 0` e nuovo `play`

Riprova ogni 700 ms per **45 secondi**, poi si ferma comunque. Appena il video
parte lo script si spegne: con venti schede aperte il costo dopo i primi secondi
è zero.

Se una pagina contenesse un `<video>` normale, viene gestito con la stessa
logica (play diretto, volume basso, e in ultima istanza il clic sul pulsante play
dei player conosciuti).

## Perché volume basso e non muto

Chrome **non** applica il throttling intensivo alle schede che riproducono
audio. Ma un video muto non conta come audio: una scheda silenziosa e nascosta
scende a un timer al minuto, il player non riesce più a scaricare i segmenti
successivi e il video si blocca appena esaurisce il buffer.

Un video al 10% invece è audible a tutti gli effetti: la scheda resta a piena
velocità. Perciò il muto non è il default, e se un player finisce comunque a
volume zero il popup lo segnala.

## Se Chrome rifiuta l'autoplay con audio

La soluzione pulita è autorizzare l'autoplay sonoro sul dominio con la policy
`AutoplayAllowlist`. Su macOS:

```bash
defaults write com.google.Chrome AutoplayAllowlist -array "https://fad-for-me.formretail.it"
```

Riavvia Chrome e verifica su `chrome://policy` che la voce compaia. Se non
compare, questa versione di Chrome legge le policy solo dal dominio gestito:

```bash
sudo defaults write /Library/Managed\ Preferences/com.google.Chrome.plist AutoplayAllowlist -array "https://fad-for-me.formretail.it"
```

Per annullare: `defaults delete com.google.Chrome AutoplayAllowlist`.

In alternativa, senza toccare nulla: guarda qualche video con audio a scheda
attiva. Chrome alza da solo il *Media Engagement Index* del dominio e dopo
qualche sessione concede l'autoplay sonoro.

## Opzioni

- **Volume all'avvio** — vedi sopra. Il muto è disponibile ma sconsigliato.
- **Muta come ultima spiaggia** — il punto 4 della sequenza.
- **Clicca il pulsante play** — vale solo per eventuali `<video>` nel DOM.
- **Sorveglia dopo l'avvio** — lascialo spento: la piattaforma gestisce già la
  sequenza delle lezioni.

## Diagnostica

In `content.js` la costante `DEFAULTS` ha un flag `debug`. Portalo a `true`,
ricarica l'estensione e la pagina: la console mostra quando il player Vimeo viene
trovato, quando risponde "pronto", e quando parte davvero.

Il popup manda un ping a tutti i frame e ogni frame replica con un messaggio
proprio, invece di usare la `respond` di `sendMessage`: quella consegna una
risposta sola, e sarebbe quella del frame principale. L'unico permesso richiesto
è `storage`.
