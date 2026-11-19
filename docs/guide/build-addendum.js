// Builds docs/vdi-tool-guide-addendum-a.pdf from addendum-a-body.html, with the guide's own styles.
// Run: node docs/guide/build-addendum.js (needs Playwright with Chromium).
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
(async () => {
  const guide = fs.readFileSync(path.join(__dirname, 'guide.html'), 'utf8');
  const style = guide.slice(guide.indexOf('<style>'), guide.indexOf('</style>') + 8);
  const body = fs.readFileSync(path.join(__dirname, 'addendum-a-body.html'), 'utf8');
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>vdi-tool guide, addendum A</title>${style}</head><body>${body}</body></html>`;
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.setContent(html, { waitUntil: 'networkidle' });
  await p.pdf({
    path: path.join(__dirname, '..', 'vdi-tool-guide-addendum-a.pdf'), format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: '<div style="font-size:8px;color:#5b6876;width:100%;padding:0 15mm;display:flex;justify-content:space-between"><span>vdi-tool guide, addendum A: testing tools, version 1</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
  });
  await b.close();
})();
