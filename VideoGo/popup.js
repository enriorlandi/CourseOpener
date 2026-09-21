const DEFAULTS = {
  enabled: true,
  startVolume: 0,
  startQuality: '240p',
  lastResortMute: true,
  clickPlayButton: true,
  resumeIfPaused: false,
};

const CHECKBOXES = ['enabled', 'lastResortMute', 'clickPlayButton', 'resumeIfPaused'];

chrome.storage.sync.get(DEFAULTS, (stored) => {
  for (const key of CHECKBOXES) {
    const el = document.getElementById(key);
    el.checked = stored[key];
    el.addEventListener('change', () => chrome.storage.sync.set({ [key]: el.checked }));
  }
  const vol = document.getElementById('startVolume');
  vol.value = String(stored.startVolume);
  vol.addEventListener('change', () =>
    chrome.storage.sync.set({ startVolume: Number(vol.value) })
  );
  // Qui il valore resta una stringa: sono gli id delle rendition di Vimeo
  // ('240p', 'auto'), non numeri.
  const qual = document.getElementById('startQuality');
  qual.value = String(stored.startQuality);
  qual.addEventListener('change', () =>
    chrome.storage.sync.set({ startQuality: qual.value })
  );
});

const status = document.getElementById('status');
const warn = document.getElementById('warn');

/* --------------------------------------------------------- stato dei frame */

/**
 * I video della piattaforma stanno dentro un iframe (SCORM/H5P), quindi non
 * basta la risposta del frame principale: sendMessage ne consegna una sola, e
 * sarebbe proprio quella senza video. Mandiamo un ping a tutti i frame e
 * raccogliamo le risposte, che arrivano come messaggi separati.
 */
const reports = new Map();
let arrived = false;

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.type !== 'videogo:report') return;
  arrived = true;
  reports.set(sender.frameId ?? reports.size, msg.data);
  render();
});

function render() {
  const frames = [...reports.values()];
  const sum = { players: 0, playing: 0, silent: 0 };
  for (const f of frames) {
    // Su questa piattaforma i player sono iframe Vimeo, non <video> nel DOM.
    sum.players += f.vimeo + f.videos;
    sum.playing += f.vimeoPlaying + f.videosPlaying;
    sum.silent += f.vimeoSilent + f.videosSilent;
  }

  const insisting = frames.some((f) => f.watching);

  if (sum.players === 0) {
    status.textContent = `Nessun player trovato in ${frames.length} frame.`;
    return;
  }

  status.textContent =
    `${sum.players} player · ` +
    (sum.playing ? `${sum.playing} in riproduzione` : 'nessuno in riproduzione') +
    (insisting ? ' · sto insistendo' : '');

  if (sum.silent > 0) {
    warn.textContent =
      'Un video sta suonando a volume zero: Chrome può bloccarlo quando la scheda non è ' +
      'attiva. Alza il volume dal player, o autorizza l’autoplay sonoro (vedi README).';
  }
}

function noAnswer() {
  if (arrived) return;
  status.textContent = 'Nessun frame ha risposto.';
  warn.textContent =
    'Vai su chrome://extensions e premi ↻ Ricarica sulla scheda di VideoGo, poi ricarica ' +
    'questa pagina. Il manifest viene riletto solo al ricaricamento dell’estensione.';
}

chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
  if (!tab?.url?.startsWith('https://fad-for-me.formretail.it/')) {
    status.textContent = 'Non sei sulla piattaforma FAD.';
    return;
  }

  document.getElementById('reload').addEventListener('click', () => {
    chrome.tabs.reload(tab.id);
    window.close();
  });

  // Senza frameId il ping raggiunge ogni frame della scheda. L'errore "nessun
  // destinatario" qui è atteso e innocuo: le risposte vere arrivano dopo, come
  // messaggi separati.
  chrome.tabs.sendMessage(tab.id, { type: 'videogo:ping' }, () => void chrome.runtime.lastError);

  status.textContent = 'Interrogo i frame…';
  setTimeout(noAnswer, 600);
});
