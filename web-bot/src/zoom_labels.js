// Chiistyye predikaty i regex-stroki metok knopok Zoom vebl-klienta.
// Strokovyye istochniki peredayutsya v page.evaluate, predikaty — dlya testov
// (node --test web-bot/test/).
//
// Vazhno: v personal'noy komnate (PMN, /my/...) krasnaya knopka zvetsya
// "End" / "End meeting", a NE "Leave" (vstrecha 39: "knopka Leave nie naydena").

const norm = (t) => String(t || "").trim().toLowerCase();
const re = (src) => new RegExp(src, "i");

// --- knopka mikrofona -------------------------------------------------------
export const MIC_CANDIDATE_SRC = "microphone|unmute|\\bmic\\b|микрофон|audio";
// Nadpisi, oznachayushchiy "klik — vklyuchim", t.ye. mikrofon UZHE vyklyuchen.
export const MIC_MUTED_SRC = "unmute|start audio|включить микрофон|подключить микрофон";
// Nadpisi aktivnogo mikrofona: klik po nikh vyklyuchaet.
export const MIC_ACTIVE_SRC = "\\bmute\\b|stop audio|выключить микрофон|отключить микрофон";
// Ikonka s perekhyorstiem/outline = vyknuchennyy (microphone-muted i t.p.).
export const MIC_MUTED_ICON_SRC = "muted|slash";

export function isMicCandidate(label) {
  return re(MIC_CANDIDATE_SRC).test(norm(label));
}

// true = vyklyuchen, false = vklyuchen, null = sostoyaniye nie opredelyaetsya
// po odnoy tol'ko nadpisi (chto-by nie klikat' vslepuyu).
export function micMutedFromLabel(label) {
  const t = norm(label);
  const muted = re(MIC_MUTED_SRC).test(t);
  const active = re(MIC_ACTIVE_SRC).test(t);
  if (muted && !active) return true;
  if (active && !muted) return false;
  return null;
}

// --- knopka vyhoda iz sozwona (Leave v obychnoy vstreche, End v PMN) --------
export const LEAVE_SRCS = [
  "leave meeting", "leave this meeting", "выйти (с|из) (встречи|созвона)",
  "оставить встречу", "покинуть встречу",
  "^leave$", "^выйти$", "^покинуть$", "^оставить$", // vstrecha 41: aria-label "Leave" golyakom
  "end meeting", "end for all", "end all meetings", "end call", "^end$",
  "завершить (встреч|созвон)", "закончить (встреч|созвон)",
];

// Dialog podtverzhdeniya posle Leave/End.
export const LEAVE_DIALOG_SRCS = [
  "leave meeting", "leave this meeting", "выйти (с|из) (встречи|созвона)",
  "disconnect", "отцепиться",
  "end meeting", "end for all", "end all meetings", "end your meeting",
  "завершить (встреч|созвон)", "закончить (встреч|созвон)",
];

// Odin rezhim dlya klassifikatora stranitsy: priznak "ya v zale".
export const END_CONTROL_SRC = LEAVE_SRCS.join("|");

function matchesAny(label, srcs) {
  const t = norm(label);
  if (!t) return false;
  return srcs.some((s) => re(s).test(t));
}

export function isLeaveMeetingLabel(label) {
  return matchesAny(label, [
    "leave meeting", "leave this meeting", "выйти (с|из) (встречи|созвона)",
    "оставить встречу", "покинуть встречу", "^leave$",
  ]);
}

// "End", "End meeting (for all)", "End all meetings…" — NO "recommend" "send":
// varianty s golym "end" trebuyut granitsy slova.
export function isEndMeetingLabel(label) {
  return matchesAny(label, [
    "^end$", "^end\\s", "end meeting", "end for all", "end all meetings",
    "end your meeting", "end call",
    "завершить (встреч|созвон)", "закончить (встреч|созвон)",
  ]);
}

export function isJoinedControlLabel(label) {
  return isLeaveMeetingLabel(label) || isEndMeetingLabel(label);
}

// --- strukturnye priznaki DOM (vstrecha 40: tuslyar Zoom skryt bez myshi, ---
// --- innerText pust, no knopka Leave yest' v DOM kak #leave_button) --------
export const JOINED_DOM_SEL =
  '#leave_button, [data-tid="leave_button"], [data-tid="leave_meeting"], ' +
  '#disconnect_call_button, #meetingControlBtnsLeave';
// Banner "nety audia" (puskayetsya posle vhoda bez podklyucheniya zvuka).
export const NO_AUDIO_SEL =
  '#join_with_audio_link, [data-tid="join_with_audio_link"]';

// Priznak zala BEZ Leave-knopki (vstrecha 43: Leave ne sovpa ni s odnim
// patternom, no aria-label "mute my microphone"/"unmute my microphone"
// byvaet TOL'KO v zale — na pre-join preview tot nazivaetsya prosto "mute").
export const IN_MEETING_LABELS = [
  "^mute my microphone$", "^unmute my microphone$",
  "^open the participants list",
];

export function isInMeetingLabel(label) {
  const t = norm(label);
  if (!t) return false;
  return IN_MEETING_LABELS.some((s) => re(s).test(t));
}

export const IN_MEETING_SRC = IN_MEETING_LABELS.join("|");

// --- vstrecha zavershena hostom (job 17) ------------------------------------
// Zoom pokazyvayet modaliku "This meeting has been ended by host" — NE "the
// meeting has ended". Pri etom (screenshot job 17) nizhniy tuslyar s Leave
// ostayetsya v DOM, tak chto obychonyy poryadok leaveBtn>ended ne srabatyvaet:
// dlya modaly vveden otdel'nyy signal endedDialog, idushchiy pered leaveBtn.
// Chtoby ne lozhnykh srabatyvaniy po textContent vsogo freyma (chat kogo ugodno),
// dialog-teksty v evaluate ogranicheny korotkimi uzlami-dialogami.
export const ENDED_MEETING_SRC =
  "meeting has\\s+(been\\s+)?ended" +
  "|встреча (завершена|окончена)" +
  "|совещание (завершено|окончено)";

export function meetingEndedInText(text) {
  return re(ENDED_MEETING_SRC).test(String(text || ""));
}

// --- vynesenie uchastnika hostom (job 40) --------------------------------------
// Modal' "You have been removed / You have been removed from this meeting by
// the host." s knopkoy Exit. Tuslyar s Leave pri etom ostayetsya v DOM, a
// ENDED_MEETING_SRC frazu "removed" ne lovit — bez otdel'nogo signala bot
// zhivet do tajmera tishiny (job 40: ~8.5 min posle udaleniya).
export const REMOVED_SRC =
  "you have\\s+been\\s+removed|removed\\s+from\\s+this\\s+meeting" +
  "|вас\\s+удалили|вы\\s+были\\s+удалены";

export function removedInText(text) {
  return re(REMOVED_SRC).test(String(text || ""));
}

// Knopka v dialoge udaleniya.
export const EXIT_DIALOG_SRCS = ["^exit$", "^выйти$", "^выход$"];

// Chisty verdict po agregirovannym po signalam (dlya testov).
// Poryadok vazhen (vstrecha 45/46): structural'nyy priznak zala (leaveBtn)
// idu PIERED bot_wall — na zhivoy zapisi tekst vstroeannyh freymov (team-chat,
// whiteboard) sluchayno soderzhal frazy antybot-steny, a knopki zala pri etom
// byli na meste. Na nastoyashchey stene knopok zala NET, tak chto poryadok
// bezopasen.
// Poryadok changed (job 17): endedDialog (modal' "has been ended by host")
// idu PIERED leaveBtn — pri takoy moderate tuslyar s Leave eshcho v DOM.
export function verdictFromSignals(s) {
  if (s.signRequired) return "login_required";
  if (s.endedDialog) return "meeting_ended";
  if (s.removedDialog) return "removed";
  if (s.leaveBtn) return "joined";
  if (s.botWall && !s.captcha) return "bot_wall";
  if (s.ended) return "meeting_ended";
  if (s.captcha) return "captcha";
  if (s.waitingRoom) return "waiting_room";
  if (s.joinBrowser || s.join || s.micJoin) return "prejoin";
  return "pending";
}
