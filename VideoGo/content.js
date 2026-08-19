/**
 * VideoGo — content script
 *
 * Fa una cosa sola: al caricamento della pagina fa partire il video.
 *
 * Sulla piattaforma FAD il player non è un <video> nel DOM di Moodle: è un
 * iframe di player.vimeo.com, gestito dal modulo mod_videotime, che nella
 * configurazione del server ha "autoplay":"0" — ecco perché dopo un reload
 * resta fermo. L'iframe è cross-origin, quindi non possiamo entrarci; gli
 * parliamo con l'API postMessage del player Vimeo, la stessa che usa VideoTime.
 *
 * NON riscriviamo l'src dell'iframe per infilarci autoplay=1: ricaricherebbe il
 * player e farebbe ripartire da zero il conteggio del tempo di visione, che è
 * ciò che determina il completamento della lezione. Un comando "play" via
 * postMessage è invece identico a un clic sul pulsante, e il tracciamento
 * prosegue intatto.
 *
 * Il passaggio alla lezione successiva lo fa già la piattaforma
 * ("next_activity_auto":"1"): non lo tocchiamo.
 *
 * Appena il video parte questo script si spegne: con venti schede aperte, dopo
 * i primi secondi il costo è zero.
 */

const DEFAULTS = {
  enabled: true,
  startVolume: 0, // 0-100, applicato al player prima del play. 0 = muto
  lastResortMute: true, // se Chrome rifiuta l'autoplay sonoro, riprova a volume 0
  clickPlayButton: true, // per gli eventuali <video> nativi
  windowSec: 45, // per quanto insistere dal caricamento della pagina
  firstAttemptMs: 1200, // attesa iniziale: VideoTime deve fare il suo setup
  debug: false,
};

const VIMEO_ORIGIN = 'https://player.vimeo.com';
const RETRY_MS = 700;
const MUTE_AFTER_ATTEMPTS = 6; // ~4 s prima di ripiegare sul muto
const MAX_CLICKS = 5;
const CLICK_COOLDOWN_MS = 2000;

const PLAY_BUTTON_SELECTORS = [
  '.vjs-big-play-button', // VideoJS (player di default di Moodle)
  '.plyr__control--overlaid', // Plyr
  '.jw-icon-display', // JW Player
  '.mejs-overlay-play, .mejs__overlay-play', // MediaElement.js
  '.h5p-splash, .h5p-interactive-video .h5p-splash-wrapper', // H5P
  '.video-js .vjs-play-control.vjs-paused', // VideoJS, barra comandi
];

let settings = { ...DEFAULTS };
let timer = null;
let deadline = 0;
let clicks = 0;
let lastClick = 0;
let startedAny = false;
let mutedByUs = null; // <video> nativo mutato da noi

/** Un elemento per iframe Vimeo trovato nella pagina. */
const players = [];

const log = (...args) => {
  if (settings.debug) console.log('[VideoGo]', ...args);
};

/* ------------------------------------------------------------ player Vimeo */

function isVimeo(iframe) {
  try {
    return new URL(iframe.src, location.href).origin === VIMEO_ORIGIN;
  } catch (_) {
    return false;
  }
}

function send(player, method, value) {
  const win = player.frame.contentWindow;
  if (!win) return;
  const msg = value === undefined ? { method } : { method, value };
  try {
    win.postMessage(JSON.stringify(msg), VIMEO_ORIGIN);
  } catch (_) {}
}

function collectPlayers() {
  for (const frame of document.querySelectorAll('iframe')) {
    if (!isVimeo(frame)) continue;
    if (players.some((p) => p.frame === frame)) continue;
    players.push({ frame, started: false, silent: false, attempts: 0, subscribed: false });
    log('trovato player Vimeo', frame.src);
  }
}

/**
 * Il player Vimeo risponde ai comandi e notifica gli eventi sullo stesso canale
 * postMessage. Da qui sappiamo se il video è davvero partito, senza doverlo
 * dedurre.
 */
window.addEventListener('message', (event) => {
  if (event.origin !== VIMEO_ORIGIN) return;

  let data = event.data;
  if (typeof data === 'string') {
    try {
      data = JSON.parse(data);
    } catch (_) {
      return;
    }
  }
  if (!data || typeof data !== 'object') return;

  const player = players.find((p) => p.frame.contentWindow === event.source);
  if (!player) return;

  if (data.event === 'ready') {
    log('player pronto');
    subscribe(player);
  }
  if (data.event === 'play' || data.event === 'playing') {
    player.started = true;
    startedAny = true;
    log('in riproduzione');
  }
  if (data.method === 'getPaused' && data.value === false) {
    player.started = true;
    startedAny = true;
  }
});

function subscribe(player) {
  if (player.subscribed) return;
  player.subscribed = true;
  send(player, 'addEventListener', 'play');
  send(player, 'addEventListener', 'playing');
}

function pushPlayer(player) {
  if (player.started) return;
  subscribe(player);
  player.attempts++;

  if (player.attempts === 1) {
    // Va mandato anche quando vale 0: senza il comando il player resterebbe al
    // volume che si ritrova, e "muto" non verrebbe rispettato.
    send(player, 'setVolume', Math.min(100, settings.startVolume) / 100);
  }

  send(player, 'play');
  send(player, 'getPaused'); // ci dirà se il comando ha fatto effetto

  // Chrome sta rifiutando l'autoplay sonoro: a volume zero passa, ma la scheda
  // diventa silenziosa e quindi rallentabile in background.
  if (settings.lastResortMute && !player.silent && player.attempts === MUTE_AFTER_ATTEMPTS) {
    player.silent = true;
    log('autoplay sonoro rifiutato, riprovo a volume 0');
    send(player, 'setVolume', 0);
    send(player, 'play');
  }
}

/* ------------------------------------------------------- <video> nel DOM */

function hasSource(v) {
  return !!(v.currentSrc || v.src || v.querySelector('source[src]'));
}

function mainVideo() {
  let best = null;
  let bestArea = 0;
  let fallback = null;

  for (const v of document.querySelectorAll('video')) {
    if (!hasSource(v)) continue;
    if (!fallback) fallback = v;
    const r = v.getBoundingClientRect();
    const area = r.width * r.height;
    if (area > bestArea) {
      best = v;
      bestArea = area;
    }
  }
  return best || fallback;
}

async function pushVideo() {
  const video = mainVideo();
  if (!video) return false;

  if (!video.paused && !video.ended) {
    startedAny = true;
    return true;
  }
  if (video.ended) return true;

  if (settings.startVolume > 0) {
    video.muted = false;
    video.volume = Math.min(100, settings.startVolume) / 100;
  } else {
    video.muted = true;
  }

  try {
    await video.play();
    startedAny = true;
    log('<video> avviato');
    return true;
  } catch (err) {
    if (err && err.name !== 'NotAllowedError') return false;
  }

  if (settings.lastResortMute && !video.muted) {
    video.muted = true;
    try {
      await video.play();
      mutedByUs = video;
      startedAny = true;
      log('<video> avviato solo da muto');
      return true;
    } catch (_) {
      video.muted = false;
    }
  }

  if (settings.clickPlayButton) clickPlayButtonNear(video);
  return false;
}

/**
 * Solo selettori di player noti: non clicchiamo mai bottoni generici, che in un
 * corso FAD potrebbero far avanzare le slide.
 */
function clickPlayButtonNear(video) {
  if (clicks >= MAX_CLICKS || Date.now() - lastClick < CLICK_COOLDOWN_MS) return false;

  let scope = video.parentElement;
  for (let depth = 0; scope && depth < 5; depth++, scope = scope.parentElement) {
    for (const sel of PLAY_BUTTON_SELECTORS) {
      const btn = scope.querySelector(sel);
      if (!btn) continue;
      const r = btn.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      btn.click();
      clicks++;
      lastClick = Date.now();
      log('cliccato', sel);
      return true;
    }
  }
  return false;
}

/* ------------------------------------------------------------------ ciclo */

function stop() {
  if (timer) {
    clearInterval(timer);
    timer = null;
    log('inerte');
  }
}

async function tick() {
  if (!settings.enabled) return stop();
  if (Date.now() > deadline) {
    log('finestra scaduta senza avvio');
    return stop();
  }

  collectPlayers();
  for (const p of players) pushPlayer(p);
  const videoOk = await pushVideo();

  const vimeoPending = players.some((p) => !p.started);
  const videoPending = !videoOk && !!mainVideo();
  if (!vimeoPending && !videoPending && (players.length || videoOk)) stop();
}

function start() {
  if (!settings.enabled) return;

  deadline = Date.now() + settings.windowSec * 1000;
  // VideoTime deve prima inizializzare il player e riprendere dal punto giusto
  // ("resume_playback"): se piombiamo subito con un play, gli passiamo davanti.
  setTimeout(() => {
    if (!settings.enabled) return;
    tick();
    if (!timer) timer = setInterval(tick, RETRY_MS);
  }, settings.firstAttemptMs);

  const onGesture = () => {
    if (!mutedByUs) return;
    mutedByUs.muted = false;
    mutedByUs.volume = Math.min(100, Math.max(settings.startVolume, 5)) / 100;
    mutedByUs = null;
  };
  for (const ev of ['pointerdown', 'keydown']) {
    document.addEventListener(ev, onGesture, { capture: true, passive: true });
  }
}

/* ----------------------------------------------------------- impostazioni */

/**
 * Il popup interroga tutti i frame insieme. Non rispondiamo con la callback di
 * onMessage: sendMessage consegna al mittente una risposta sola. Rispondiamo
 * con un messaggio nostro, così il popup li riceve tutti e li somma.
 */
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type !== 'videogo:ping') return;
  const vs = [...document.querySelectorAll('video')];
  chrome.runtime
    .sendMessage({
      type: 'videogo:report',
      data: {
        vimeo: players.length,
        vimeoPlaying: players.filter((p) => p.started).length,
        vimeoSilent: players.filter((p) => p.silent).length,
        videos: vs.length,
        videosPlaying: vs.filter((v) => !v.paused && !v.ended).length,
        videosSilent: vs.filter((v) => !v.paused && !v.ended && (v.muted || v.volume === 0)).length,
        watching: timer !== null,
      },
    })
    .catch(() => {});
});

chrome.storage.sync.get(DEFAULTS, (stored) => {
  settings = { ...DEFAULTS, ...stored };
  try {
    localStorage.removeItem('videogo:enabled');
    localStorage.removeItem('videogo:alwaysVisible');
  } catch (_) {}
  start();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'sync') return;
  for (const [key, { newValue }] of Object.entries(changes)) settings[key] = newValue;
  if (!settings.enabled) stop();
});
