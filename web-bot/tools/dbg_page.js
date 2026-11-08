// Debug: otkryt' ssylku profilem i vyvesti DOM-sostoyanie (SSYOKA pered payaplyam).
import { chromium } from "playwright";

const url = process.argv[2];
if (!url) { console.error("ispol'zovanie: node dbg_page.js <url>"); process.exit(2); }

const ctx = await chromium.launchPersistentContext(process.env.ZOOM_PROFILE_DIR || "/data/zoom-profile", {
  headless: true,
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
  viewport: { width: 1280, height: 800 },
});
const page = await ctx.newPage();
await page.goto(url, { waitUntil: "networkidle", timeout: 60000 }).catch(e => console.log("goto:", e.message));
await page.waitForTimeout(4000);
console.log("URL:", page.url());
console.log("=== BODY TEXT ===");
const txt = await page.evaluate(() => document.body ? document.body.innerText : "(pusto)");
console.log(txt.slice(0, 3000));
console.log("=== BUTTONS ===");
const btns = await page.evaluate(() => [...document.querySelectorAll("button, a[role=button]")].map(b => ({
  text: (b.innerText || b.getAttribute("aria-label") || "").trim().slice(0, 80),
  visible: !!(b.offsetParent),
})).filter(b => b.text));
console.log(JSON.stringify(btns, null, 1));
console.log("=== LINKS(href~browser/join/meeting) ===");
const links = await page.evaluate(() => [...document.querySelectorAll("a")]
  .map(a => (a.innerText || "").trim() + " | " + (a.getAttribute("href") || "").slice(0, 90))
  .filter(t => /.join|browser|meeting|присоедин/i.test(t)));
console.log(links.slice(0, 20).join("\n"));
await ctx.close();
