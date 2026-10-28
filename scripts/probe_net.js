const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  const reqs = [];
  p.on('request', (r) => reqs.push(`${r.method()} ${r.resourceType()} ${r.url().slice(0, 150)}`));
  await p.goto('https://www.generatestl.com/gear-generator/', { waitUntil: 'networkidle', timeout: 60000 }).catch((e) => console.log('GOTO', e.message));
  await p.waitForTimeout(3000);
  const buttons = await p.locator('button').allTextContents();
  console.log('BUTTONS', JSON.stringify(buttons.slice(0, 40)));
  const dl = p.locator('button', { hasText: /download|export|stl/i }).first();
  if (await dl.count()) {
    await Promise.all([p.waitForEvent('download', { timeout: 30000 }).then((d) => console.log('DOWNLOAD', d.suggestedFilename())).catch((e) => console.log('NODL', e.message)), dl.click().catch((e) => console.log('CLICK', e.message))]);
    await p.waitForTimeout(3000);
  }
  console.log('REQS\n' + reqs.filter((r) => !/\.(png|jpg|svg|woff2?|webp|ico)/.test(r)).join('\n'));
  const links = await p.$$eval('a[href]', (as) => [...new Set(as.map((a) => a.href))].filter((h) => h.includes('generatestl.com')));
  console.log('PAGELINKS', JSON.stringify(links));
  await b.close();
})();
