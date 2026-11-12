// Konfiguratsiya web-bot iz env (FT-37/38).
export function loadConfig() {
  return {
    redisUrl: process.env.REDIS_URL || "redis://redis:6379/0",
    zoomProfileDir: process.env.ZOOM_PROFILE_DIR || "/data/zoom-profile",
    dataDir: process.env.DATA_DIR || "/data",
    botName: process.env.BOT_NAME || "Mnema",
    urlTemplate: process.env.RECORDER_URL_TEMPLATE ||
      "https://app.zoom.us/wc/{meeting_id}/join?pwd={pwd}",
    prejoinSeconds: int(process.env.BOT_PREJOIN_SECONDS, 300),
    joinDeadlineMin: int(process.env.JOIN_DEADLINE_MIN, 10),
    silenceEndTimeoutMin: int(process.env.SILENCE_END_TIMEOUT_MIN, 5),
    // avoviyod: posle vklyucheniya zapisi zhdyom greyys-min, daleyshiy
    // uroven' zvuka nizhe poroga (dBFS) >= silenceEndTimeoutMin podryad -> Leave
    silenceGraceMin: int(process.env.SILENCE_GRACE_MIN, 30),
    // porog v dBFS (vsegda otritsatel'nyy). Ranshe `<= 0` propuskalo 0,
    // a `> 0` v izmeryaemom diapazone byvaet redko (0 dBFS = clip),
    // SILENCE_LEVEL_DB=0 delayu kazhdiy tik "tishinoy" -> avovyhod v lyuboy
    // vstreche posle greyysa.
    silenceLevelDb: silenceDb(process.env.SILENCE_LEVEL_DB, -50),
    maxDurationMin: int(process.env.BOT_MAX_DURATION_MIN, 360),
    pageReloadRetries: int(process.env.PAGE_RELOAD_RETRIES, 2),
    announceEnabled: isAnnounceEnabled(process.env),
    announceWav: process.env.ANNOUNCE_WAV || "templates/announce.wav",
    headful: process.env.HEADFUL === "1",
    captchaWaitMin: int(process.env.CAPTCHA_WAIT_MIN, 15),
    chunkFlushMs: int(process.env.CHUNK_FLUSH_MS, 5000),
    // molotok zhivost'i web-bot na obshchem /data dla watchdog-a v api
    heartbeatFile: process.env.WEBBOT_HEARTBEAT_FILE
      || `${process.env.DATA_DIR || "/data"}/web-bot.heartbeat`,
    vncMaxSlots: int(process.env.VNC_MAX_SLOTS, 20),
    vncPublicPortBase: int(process.env.VNC_PUBLIC_PORT_BASE, 8080),
    vncScreen: process.env.VNC_SCREEN || "1440x900x24",
    // neprezyvyymo: esli zadano — noVNC zaishet cherez VNC-parol' (rfbauth);
    // rfb vsegda tol'ko na loopback (-localhost), vneshniy put' — websockify
    vncPassword: process.env.VNC_PASSWORD || "",
    // neprezyvyymo: ogranichenie publikatsii websockify (napr. 0.0.0.0);
    // pustoy — bind na vse interfeysy (tekushcheedovanie docker port-mapping)
    vncBindAddr: process.env.VNC_BIND_ADDR || "",
    // disk-gard: menshe etogo svobodnogo meste na tome /data -> alert, zapis'
    // prodolzhaetsya poka mozhet (dannye vazhne; chanki kopitsya v pamyati)
    minFreeDiskBytes: int(process.env.MIN_FREE_DISK_MB, 100) * 1_000_000,
    // P1-4: esli na api vklyuchen API_TOKEN — peredavat' X-API-Key v kazhdom
    // zaprose (status/finished/events/alert/join-info). Pusto = bez auth.
    apiKey: process.env.API_TOKEN || "",
  };
}

// Zagolovki vseh HTTP-zaprosov k api (content-type + optional'nyy X-API-Key).
// pustoy contentType (GET) — content-type ne stavimsya.
export function apiHeaders(contentType = "application/json") {
  const h = {};
  if (contentType) h["content-type"] = contentType;
  const key = process.env.API_TOKEN || "";
  if (key) h["x-api-key"] = key;
  return h;
}

function int(v, d) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : d;
}

// dBFS porog: tol'ko otritsatel'nyy v razumnom diapazone; 0/+/NaN -> default.
function silenceDb(v, d) {
  const n = parseFloat(v);
  if (!Number.isFinite(n) || n >= 0 || n < -120) return d;
  return n;
}

// Vkl/vykl flag: pustoy/neraspoznnannoe -> default; 0/false/off/no -> vykl.
export function boolFlag(v, d = true) {
  const s = String(v ?? "").trim().toLowerCase();
  if (!s) return d;
  if (["0", "false", "off", "no"].includes(s)) return false;
  if (["1", "true", "on", "yes"].includes(s)) return true;
  return d;
}

export function isAnnounceEnabled(env = process.env) {
  return boolFlag(env.ANNOUNCE_ENABLED, true);
}

// Normalizatsiya ssylki-priglasheniya (FT-1).
export function buildJoinUrl(cfg, meetingId, pwd) {
  let url = cfg.urlTemplate.replace("{meeting_id}", meetingId);
  if (pwd) return url.replace("{pwd}", pwd);
  // bez parolya: snyat' VSU parametr, a ne znachenie — inache
  // ".../join?pwd={pwd}" prevrashchaetsya v ".../join{pwd}" (not_started)
  url = url.replace("{pwd}", "");
  url = url.replace(/[?&]pwd=(?=&|$)/, "");
  url = url.replace(/[?&]$/, "");
  return url;
}
