// Debug2: payti put' vhoda i vyvesti sostoyanie posle kazhdogo shaga.
//
// VIMAHNIE: eto odnorazovyy otладochnik NE iz khodovogo koda (nikto ego ne
// importiruet). Zapusk protiw zhivogo /data/zoom-profile, kotoryy derzhit
// setup-brauzer, sdul SingletonLock i mozhet izportit' profil' (poterya
// Zoom-logina). Pered zapuskom ostanovite setup-brauzer/web-bot, ukazhite
// ZOOM_PROFILE_DIR v otdel'nuyu kopiyu, i NE dayte emu boevoy profil'.
import { chromium } from "playwright";

const url = process.argv[2];
const ctx = await chromium.launchPersistentContext(process.env.ZOOM_PROFILE_DIR || "/data/zoom-profile", {
  headless: true,
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
  viewport: { width: 1280, height: 800 },
});
const page = await ctx.newPage();
page.on("dialog", d => d.accept().catch(() => {}));
await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(e => console.log("goto:", e.message));
await page.waitForTimeout(6000);

const dump = async (label) => {
  console.log("=== " + label + " | " + page.url().slice(0, 110));
  for (const [i, fr] of page.frames().entries()) {
    const d = await fr.evaluate(() => {
      const txt = document.body ? document.body.innerText.replace(/\s+/g, " ").slice(0, 300) : "";
      const btns = [...document.querySelectorAll("button, a")]
        .map(b => (b.innerText || b.getAttribute("aria-label") || "").trim().slice(0, 60))
        .filter(Boolean).slice(0, 25);
      return { txt, btns };
    }).catch(e => ({ txt: "ERR " + e.message, btns: [] }));
    console.log(`frame ${i}: ${fr.url().slice(0, 90)}`);
    console.log(`  text: ${d.txt}`);
    console.log(`  btns: ${JSON.stringify(d.btns)}`);
  }
};

// Knopki/polya vo vseh freymah
const clickFr = async (re, waitMs = 5000) => {
  const until = Date.now() + waitMs;
  while (Date.now() < until) {
    for (const fr of page.frames()) {
      const b = fr.getByRole("button", { name: re }).first();
      if (await b.isVisible({ timeout: 400 }).catch(() => false)) {
        await b.click({ timeout: 4000 }).catch(() => {});
        console.log("CLICK [" + re + "] v", fr.url().slice(0, 60));
        return true;
      }
    }
    await page.waitForTimeout(800);
  }
  return false;
};

await dump("posle goto");
await clickFr(/accept cookies/i, 3000);
await dump("posle cookie");
if (await clickFr(/join from browser/i)) await page.waitForTimeout(7000) , await dump("posle join-from-browser");
for (const fr of page.frames()) {
  const ni = fr.locator("input[type='text']").first();
  if (await ni.isVisible({ timeout: 600 }).catch(() => false)) {
    await ni.fill("Mnema").catch(() => {});
    console.log("FILL imya v", fr.url().slice(0, 60)); break;
  }
}
await clickFr(/^join$/i, 6000);
for (let i = 1; i <= 8; i++) {
  await page.waitForTimeout(5000);
  await dump("posle Join +" + i * 5 + "s");
  const leave = await page.evaluate(() =>
    [...document.querySelectorAll("button")].some(b => /leave|выйти/i.test(b.innerText || "")));
  if (leave) { console.log("*** LEAVE NAIDEN ***"); break; }
}
await ctx.close();
