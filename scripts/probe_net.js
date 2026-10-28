const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  const seen = [];
  p.on('request', (r) => seen.push(r.url()));
  p.on('console', (m) => console.log('CONSOLE', m.type(), m.text().slice(0, 200)));
  p.on('pageerror', (e) => console.log('PAGEERROR', e.message.slice(0, 200)));
  await p.goto('https://alpaco.it/generators/case/', { waitUntil: 'networkidle', timeout: 60000 });
  await p.waitForTimeout(8000);
  const buttons = await p.locator('button').allTextContents();
  console.log('BUTTONS', JSON.stringify(buttons.map((t) => t.trim()).filter(Boolean).slice(0, 40)));
  for (const label of [/generate/i, /preview/i, /update/i, /render/i]) {
    const btn = p.locator('button', { hasText: label }).first();
    if (await btn.count()) { try { await btn.click({ timeout: 3000 }); await p.waitForTimeout(8000); console.log('CLICKED', String(label)); } catch (e) { console.log('CLICKFAIL', String(label)); } }
  }
  console.log('REQUESTS', JSON.stringify([...new Set(seen)]));
  await b.close();
})();
