// Screenshots of design/after/*.html at desktop (1440), 900 and phone (390) width, light and dark.
// Usage: node design/tools/shoot.mjs [page.html ...]
// puppeteer-core is found in HALLMARK_PUPPETEER, else in ~/.cache/reverie-design-tools/puppeteer (see design/after/README.md).
import { createRequire } from 'node:module';
import { readdirSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const after = resolve(here, '../after');
const out = resolve(after, 'screenshots');
const home = process.env.HALLMARK_PUPPETEER || resolve(homedir(), '.cache/reverie-design-tools/puppeteer');
const req = createRequire(resolve(home, 'x.js'));
const puppeteer = req('puppeteer-core');
const chrome = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
mkdirSync(out, { recursive: true });
const pages = process.argv.slice(2).length ? process.argv.slice(2) : readdirSync(after).filter((f) => f.endsWith('.html'));
const browser = await puppeteer.launch({ executablePath: chrome, headless: true });
for (const f of pages) {
  for (const [w, label] of [[1440, 'desktop'], [900, '900'], [390, '390']]) {
    for (const scheme of ['light', 'dark']) {
      const page = await browser.newPage();
      await page.setViewport({ width: w, height: 900, deviceScaleFactor: 1 });
      await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
      await page.goto(pathToFileURL(resolve(after, f)).href);
      const name = `${f.replace(/\.html$/, '')}-${label}-${scheme}.png`;
      await page.screenshot({ path: resolve(out, name), fullPage: true });
      await page.close();
    }
  }
  if (f === 'proposals.html') {                      // the disclosures opened: reject reason, commands, turned down
    for (const scheme of ['light', 'dark']) {
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
      await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
      await page.goto(pathToFileURL(resolve(after, f)).href);
      await page.evaluate(() => document.querySelectorAll('details.history, details.reject, details.cmd').forEach((d) => { d.open = true; }));
      await page.screenshot({ path: resolve(out, `proposals-opened-desktop-${scheme}.png`), fullPage: true });
      await page.close();
    }
  }
  console.log('shot', f);
}
await browser.close();
