// Run: node tests/interface.cjs (Playwright and a Chromium browser required).
// Start python -m http.server 8766 first, or set RPL_TEST_URL.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH||undefined,args:['--no-sandbox']});
 const page=await browser.newPage();
 const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 // Deterministic oversized asset: verifies containment independently of remote image availability.
 await page.route('https://**/*',route=>route.request().resourceType()==='image' ? route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1600"><rect width="1200" height="1600" fill="#7b91af"/></svg>'}) : route.abort());
 await page.route('http://localhost:8765/**',r=>r.abort());
 const url=process.env.RPL_TEST_URL||'http://localhost:8766/rpl_arena_v6.html';
 await page.goto(url);
 async function checkLayout(route,width){
  await page.setViewportSize({width,height:1000});
  await page.evaluate(hash=>location.hash=hash,route);
  await page.waitForTimeout(100);
  const result=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth+1, escaped:[...document.querySelectorAll('.crest img')].filter(img=>{const a=img.getBoundingClientRect(),b=img.parentElement.getBoundingClientRect();return a.width>b.width+1||a.height>b.height+1||a.left<b.left-1||a.right>b.right+1;}).length}));
  assert.equal(result.escaped,0,`crest escape: ${route} at ${width}`);
  assert.equal(result.overflow,false,`page overflow: ${route} at ${width}`);
 }
 for(const width of [1440,1024,768,390,320])for(const route of ['#/','#/clubs','#/club/zen','#/matches/13','#/table','#/compare','#/favorites'])await checkLayout(route,width);
 await page.setViewportSize({width:1440,height:1000});
 await page.evaluate(()=>location.hash='#/favorites');await page.waitForTimeout(100);
 await page.locator('[data-favorite="zen"]').click();
 assert.equal(await page.locator('[data-favorite="zen"]').getAttribute('aria-pressed'),'true');
 await page.reload();
 assert.equal(await page.locator('[data-favorite="zen"]').getAttribute('aria-pressed'),'true');
 const matches=await page.evaluate(()=>[...document.querySelectorAll('.watch-panel .mrow')].map(a=>matchMap[a.hash.split('/').pop()]));
 assert.ok(matches.length>0);assert.ok(matches.every(m=>m.h==='zen'||m.a==='zen'));
 await page.getByRole('button',{name:'Результаты',exact:true}).click();
 assert.ok(await page.evaluate(()=>[...document.querySelectorAll('.watch-panel .mrow')].every(a=>played(matchMap[a.hash.split('/').pop()]))));
 await page.locator('[data-favorite="zen"]').click();
 assert.equal(await page.locator('.watch-panel .mrow').count(),0);
 await page.evaluate(()=>location.hash='#/compare');await page.waitForTimeout(100);
 await page.selectOption('#compare-left','zen');await page.selectOption('#compare-right','spa');
 assert.equal(await page.locator('.compare-row').first().innerText(),await page.evaluate(()=>`${TABLE.byId.zen.pos}\nМесто в таблице\n${TABLE.byId.spa.pos}`));
 await page.selectOption('#compare-right','zen');
 assert.notEqual(await page.locator('#compare-left').inputValue(),await page.locator('#compare-right').inputValue());
 const mid=await page.evaluate(()=>S.matches.find(m=>played(m)).id);
 await checkLayout('#/match/'+mid,390);
 for(const tab of ['Статистика','Составы','Форма и история']){await page.getByRole('button',{name:tab,exact:true}).click();assert.ok(await page.locator('#app').innerText());}
 const pid=await page.evaluate(()=>S.players[0].id);await checkLayout('#/player/'+pid,390);
 await page.click('#themeBtn');await checkLayout('#/clubs',390);
 await page.click('#themeBtn');await checkLayout('#/',1440);
 await page.screenshot({path:'/tmp/rpl-desktop.png',fullPage:true});
 await checkLayout('#/',390);await page.screenshot({path:'/tmp/rpl-mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS: 35 route/viewport checks, crest bounds, favorites persistence/filtering, comparison, match tabs, player view and theme.');
})().catch(e=>{console.error(e);process.exit(1)});
