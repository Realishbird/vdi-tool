// Builds docs/vdi-tool-guide.pdf from guide.html. Run: node docs/guide/build.js (needs Playwright with Chromium).
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto('file://' + path.join(__dirname, 'guide.html'));
  await p.waitForLoadState('networkidle');
  await p.pdf({
    path: path.join(__dirname, '..', 'vdi-tool-guide.pdf'), format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: '<div style="font-size:8px;color:#5b6876;width:100%;padding:0 15mm;display:flex;justify-content:space-between"><span>vdi-tool guide, version 1</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
  });
  await b.close();
})();
