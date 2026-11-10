// Testy predikatov metok knopok Zoom (zoom_labels.js). Zapusk: node --test web-bot/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isMicCandidate, micMutedFromLabel,
  isLeaveMeetingLabel, isEndMeetingLabel, isJoinedControlLabel,
} from "../src/zoom_labels.js";

// --- mikrofon ---------------------------------------------------------------
test("mikrofona-kandidat: angliyskiye i russkiye metki", () => {
  assert.equal(isMicCandidate("Mute my microphone"), true);
  assert.equal(isMicCandidate("Unmute"), true);
  assert.equal(isMicCandidate("Выключить микрофон"), true);
  assert.equal(isMicCandidate("Stop audio"), true);
});

test("mikrofona-kandidat: chuzhie knopki ne podhodiat", () => {
  assert.equal(isMicCandidate("Participants"), false);
  assert.equal(isMicCandidate("End meeting"), false);
  assert.equal(isMicCandidate("Share screen"), false);
});

test("aktivnyy mikrofon (klik vyklyuchit) — muted false", () => {
  assert.equal(micMutedFromLabel("Mute my microphone"), false);
  assert.equal(micMutedFromLabel("MUTE"), false);
  assert.equal(micMutedFromLabel("Stop audio"), false);
  assert.equal(micMutedFromLabel("Выключить микрофон"), false);
});

test("vyklyuchennyy mikrofon — muted true", () => {
  assert.equal(micMutedFromLabel("Unmute"), true);
  assert.equal(micMutedFromLabel("Unmute microphone"), true);
  assert.equal(micMutedFromLabel("Start audio"), true);
  assert.equal(micMutedFromLabel("Включить микрофон"), true);
});

test("nieopredelyonnaya nadpis' — null (nie klikat' vslepuyu)", () => {
  assert.equal(micMutedFromLabel("Audio"), null);
  assert.equal(micMutedFromLabel(""), null);
  assert.equal(micMutedFromLabel(null), null);
});

// --- Leave / End -------------------------------------------------------------
test("Leave-varianty v obychnoy vstreche", () => {
  assert.equal(isLeaveMeetingLabel("Leave meeting"), true);
  assert.equal(isLeaveMeetingLabel("Leave"), true);
  assert.equal(isLeaveMeetingLabel("Выйти из встречи"), true);
  assert.equal(isLeaveMeetingLabel("Оставить встречу"), true);
});

test("End-varianty v personal'noy komnate (PMN, vstrecha 39)", () => {
  assert.equal(isEndMeetingLabel("End"), true);
  assert.equal(isEndMeetingLabel("end"), true);
  assert.equal(isEndMeetingLabel("End meeting"), true);
  assert.equal(isEndMeetingLabel("End meeting for all"), true);
  assert.equal(isEndMeetingLabel("End for all"), true);
  assert.equal(isEndMeetingLabel("Завершить встречу"), true);
});

test("End ne lovit lozhnyye sovmesteniya", () => {
  assert.equal(isEndMeetingLabel("Recommend"), false);
  assert.equal(isEndMeetingLabel("Send"), false);
  assert.equal(isEndMeetingLabel("Attendee list"), false);
  assert.equal(isEndMeetingLabel("Calendar"), false);
  assert.equal(isLeaveMeetingLabel("Leaving soon"), false);
});

test("priznak zona (joined): Leave ili End", () => {
  assert.equal(isJoinedControlLabel("Leave"), true);
  assert.equal(isJoinedControlLabel("End"), true);
  assert.equal(isJoinedControlLabel("Mute my microphone"), false);
  assert.equal(isJoinedControlLabel("Share screen"), false);
});

// --- verdict klassifikatora (vstrecha 40: skrytyy tuslyar) --------------------
import { verdictFromSignals, JOINED_DOM_SEL, NO_AUDIO_SEL } from "../src/zoom_labels.js";

const base = { signRequired: false, botWall: false, ended: false, leaveBtn: false,
               captcha: false, waitingRoom: false, joinBrowser: false,
               join: false, micJoin: false };

test("verdict: priory tet strannits y prezhnim", () => {
  assert.equal(verdictFromSignals({ ...base, signRequired: true }), "login_required");
  assert.equal(verdictFromSignals({ ...base, botWall: true }), "bot_wall");
  assert.equal(verdictFromSignals({ ...base, botWall: true, captcha: true }), "captcha");
  assert.equal(verdictFromSignals({ ...base, ended: true }), "meeting_ended");
  assert.equal(verdictFromSignals({ ...base, leaveBtn: true }), "joined");
  assert.equal(verdictFromSignals({ ...base, waitingRoom: true }), "waiting_room");
  assert.equal(verdictFromSignals({ ...base, join: true }), "prejoin");
  assert.equal(verdictFromSignals({ ...base }), "pending");
});

test("verdict: leaveBtn (razom s dom-priznakom zala) pobezhdayet captcha/ocr", () => {
  assert.equal(verdictFromSignals({ ...base, leaveBtn: true, captcha: true }), "joined");
});

// --- dialog "ended by host" (job 17) -------------------------------------------
import { meetingEndedInText } from "../src/zoom_labels.js";

test("meetingEndedInText: variant 'has been ended by host' (job 17)", () => {
  assert.equal(meetingEndedInText("This meeting has been ended by host"), true);
  assert.equal(meetingEndedInText("this meeting has ended"), true);
  assert.equal(meetingEndedInText("The meeting has ended."), true);
  assert.equal(meetingEndedInText("MEETING HAS  BEEN  ENDED"), true);
});

test("meetingEndedInText: russkiye varianty", () => {
  assert.equal(meetingEndedInText("Встреча завершена"), true);
  assert.equal(meetingEndedInText("Встреча окончена"), true);
  assert.equal(meetingEndedInText("Совещание завершено"), true);
});

test("meetingEndedInText: ne lozhnyye frazy", () => {
  assert.equal(meetingEndedInText("When has the meeting started?"), false);
  assert.equal(meetingEndedInText("Ending soon"), false);
  assert.equal(meetingEndedInText(""), false);
  assert.equal(meetingEndedInText(null), false);
});

test("verdict: endedDialog pobezhdayet leaveBtn (tuslyar eshcho v DOM)", () => {
  assert.equal(verdictFromSignals({ ...base, endedDialog: true, leaveBtn: true }), "meeting_ended");
  assert.equal(verdictFromSignals({ ...base, endedDialog: true }), "meeting_ended");
});

test("verdict: bez endedDialog poryadok ne menyaetsya", () => {
  assert.equal(verdictFromSignals({ ...base, leaveBtn: true, ended: false }), "joined");
});

// --- dialog "You have been removed" (job 40) ------------------------------------
import { removedInText, EXIT_DIALOG_SRCS } from "../src/zoom_labels.js";

test("removedInText: angliyskiye varianty udaleniya (job 40)", () => {
  assert.equal(removedInText("You have been removed"), true);
  assert.equal(removedInText("You have been removed from this meeting by the host."), true);
  assert.equal(removedInText("YOU HAVE BEEN  REMOVED"), true);
});

test("removedInText: russkiye varianty", () => {
  assert.equal(removedInText("Вас удалили из встречи"), true);
  assert.equal(removedInText("Вы были удалены"), true);
});

test("removedInText: ne lozhnyye frazy", () => {
  assert.equal(removedInText("Remove background noise"), false);
  assert.equal(removedInText("Participant removed a pin"), false);
  assert.equal(removedInText(""), false);
  assert.equal(removedInText(null), false);
});

test("verdict: removedDialog pobezhdayet leaveBtn (tuslyar eshcho v DOM)", () => {
  assert.equal(verdictFromSignals({ ...base, removedDialog: true, leaveBtn: true }), "removed");
  assert.equal(verdictFromSignals({ ...base, removedDialog: true }), "removed");
});

test("verdict: endedDialog vazhne removedDialog", () => {
  assert.equal(verdictFromSignals({ ...base, endedDialog: true, removedDialog: true }),
    "meeting_ended");
});

test("verdict: bez removedDialog poryadok ne menyaetsya", () => {
  assert.equal(verdictFromSignals({ ...base, leaveBtn: true }), "joined");
});

test("EXIT_DIALOG_SRCS: knopka Exit v dialoge udaleniya", () => {
  const re = (s) => new RegExp(s, "i");
  assert.ok(EXIT_DIALOG_SRCS.some((s) => re(s).test("Exit")));
  assert.ok(EXIT_DIALOG_SRCS.some((s) => re(s).test("Выйти")));
  assert.ok(!EXIT_DIALOG_SRCS.some((s) => re(s).test("Exit fullscreen")));
});

test("dom-selektory ne pustyye i bez probelov v selectorah", () => {
  assert.ok(JOINED_DOM_SEL.includes("#leave_button"));
  assert.ok(JOINED_DOM_SEL.includes('[data-tid="leave_button"]'));
  assert.ok(NO_AUDIO_SEL.includes("#join_with_audio_link"));
});

// --- priznak zala bez Leave (vstrecha 43) -------------------------------------
import { isInMeetingLabel } from "../src/zoom_labels.js";

test("priznak zala: mute my microphone / participants list", () => {
  assert.equal(isInMeetingLabel("Mute my microphone"), true);
  assert.equal(isInMeetingLabel("Unmute my microphone"), true);
  assert.equal(isInMeetingLabel("Open the participants list panel"), true);
});

test("priznak zala ne lovit preview-knopki pred-join", () => {
  assert.equal(isInMeetingLabel("Mute"), false); // preview: prosto "Mute"
  assert.equal(isInMeetingLabel("Join"), false);
  assert.equal(isInMeetingLabel("Start Video"), false);
});
