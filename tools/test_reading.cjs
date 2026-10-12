const {test} = require('node:test');
const assert = require('node:assert/strict');
const {fitText, fitGrowing, workSettings, nextFontScale, needsRetry, speechPreset, sourceFrame, sourceFontSize, validateBackup} = require('../extension/reading.js');

test('settings backup keeps only known, valid values', () => {
  const r = validateBackup({app: 'BalloonLens', settings: {
    model: 'qwen3.5:9b', fontScale: 9, reading: 'ttb', token: 'secret', autoSites: ['comic.example', 'bad host/'],
    workProfiles: [{url: 'https://r.test/a', instructions: '반말', glossary: 'A = 에이', extra: 1}],
  }, works: {'https://r.test/a': {title: 'A', summary: 's', characters: 'c', junk: 1}, 'https://other.test': {title: 'X'}}});
  assert.deepEqual(r.settings, {model: 'qwen3.5:9b', reading: 'ttb',
    workProfiles: [{url: 'https://r.test/a', instructions: '반말', glossary: 'A = 에이'}]});
  assert.deepEqual(r.works, {'https://r.test/a': {title: 'A', summary: 's', characters: 'c'}});
  assert.deepEqual(r.skipped.sort(), ['autoSites', 'fontScale', 'token']);
  assert.throws(() => validateBackup({settings: {}}), /설정 파일/);
  assert.equal(validateBackup({app: 'BalloonLens', settings: {workProfiles: [{url: 'javascript:x'}]}}).settings.workProfiles, undefined);
});
const measure = (text, size) => [...text].reduce((n,c) => n + (/\s/.test(c) ? size*.3 : size*.9), 0);
test('Korean words stay intact at reading size with balanced lines', () => {
  const r = fitText('어디로 가는 거야?', 150, 110, measure, 1);
  assert.equal(r.overflow, false);
  assert.ok(r.size >= 14);
  assert.equal(r.lines.join(' '), '어디로 가는 거야?');
  assert.ok(r.lines.every(line => measure(line, r.size) <= 150));
});
test('very small frame signals overflow rather than enlarging over artwork', () => {
  const r = fitText('아직 이야기가 끝나지 않았어. 조금만 더 기다려 줘.', 35, 25, measure, 1);
  assert.ok(r.overflow);
  assert.ok(r.size >= 14);
});
test('long unspaced text is bounded and punctuation remains visible', () => {
  const r = fitText('가나다라마바사아자차카타파하!', 110, 140, measure, 1);
  assert.equal(r.lines.join(''), '가나다라마바사아자차카타파하!');
});
test('font gets smaller before a short Korean word is broken vertically', () => {
  const r = fitText('조용히 해!', 65, 155, measure, 1);
  assert.ok(r.lines.includes('조용히'));
  assert.ok(r.size>=14 && r.size<=24);
});
test('unavoidable word split does not create single-character columns', () => {
  const r=fitText('조용히 해!',39,129,measure,1);
  assert.ok(r.lines.some(line=>line.startsWith('조용')));
});
// Malgun Gothic: Hangul advances 1em, space/punctuation about .3em (checked against the font file).
const malgun = (text, size) => [...text].reduce((n,c) => n + (/[가-힣]/.test(c) ? size : size*.3), 0);
test('tiny cleaned bubble shrinks slightly instead of splitting words (frame sizes from the E2E layout)', () => {
  const quiet = fitText('조용히 해!', 34, 49, malgun, 1);
  assert.deepEqual(quiet.lines, ['조용히', '해!']);
  assert.equal(quiet.overflow, false);
  assert.ok(quiet.size >= 11 && quiet.size < 14);
  // With the old extra 5px side padding only 24px was left: no readable size keeps the word whole.
  assert.ok(fitText('조용히 해!', 24, 45, malgun, 1).broken);
  const retry = fitText('다시 한번 시도해 볼게.', 46, 62, malgun, 1);
  assert.equal(retry.broken, false);
  assert.equal(retry.overflow, false);
  assert.ok(retry.size >= 11);
});
test('below the readable floor it still reports overflow instead of shrinking further', () => {
  const r = fitText('아직 이야기가 끝나지 않았어. 조금만 더 기다려 줘.', 46, 40, measure, 1);
  assert.ok(r.overflow);
  assert.ok(r.size >= 11);
});
// Hangul-heavy translations are wider than the OCR box, so the plain fit cuts them (the bug the reader saw).
test('a line cut by its box grows around the box and then shows whole at a readable size', () => {
  const text = '아직 이야기가 끝나지 않았어요. 조금만 더 기다려 주세요.';
  assert.ok(fitText(text, 60, 45, malgun, 1).overflow);
  const r = fitGrowing(text, 60, 45, 1000, 1000, malgun, 1);
  assert.equal(r.overflow, false);
  assert.ok(r.size >= 14);
  assert.ok(r.width > 60 && r.width <= 120 && r.height > 45 && r.height <= 90);
  assert.ok(r.lines.every(line => malgun(line, r.size) <= r.width));
});
test('growth never passes the image size, and a line that still does not fit is reported', () => {
  const r = fitGrowing('아직 이야기가 끝나지 않았어요. 조금만 더 기다려 주세요.', 60, 45, 70, 50, malgun, 1);
  assert.ok(r.width <= 70 && r.height <= 50);
  assert.ok(r.overflow);
});
test('growth stops at the room that is free, so it never covers a neighbouring balloon', () => {
  const text = '아직 이야기가 끝나지 않았어요. 조금만 더 기다려 주세요.';
  // A neighbour leaves room for at most a 80x60 box: no grown box may be bigger than that.
  const r = fitGrowing(text, 60, 45, 1000, 1000, malgun, 1, 26, (w, h) => w <= 80 && h <= 60);
  assert.ok(r.width <= 80 && r.height <= 60);
  // No free room at all: the line stays in its own box, cut with the overflow mark, rather than covering anything.
  const none = fitGrowing(text, 60, 45, 1000, 1000, malgun, 1, 26, () => false);
  assert.equal(none.width, 60);
  assert.equal(none.height, 45);
  assert.equal(none.overflow, true);
});
test('a line that already fits at a readable size keeps its box', () => {
  const r = fitGrowing('어디로 가는 거야?', 150, 110, 400, 400, measure, 1);
  assert.equal(r.width, 150);
  assert.equal(r.height, 110);
  assert.equal(r.overflow, false);
});
test('a line that fits only at a small size grows until the font is readable', () => {
  const r = fitGrowing('도대체 무슨 일이 일어난 거야? 설명해 줄 사람이 없어?', 90, 70, 1000, 1000, malgun, 1);
  assert.equal(r.overflow, false);
  assert.ok(r.size >= 14);
  assert.ok(r.width > 90 || r.height > 70);
});
test('work profiles match URL boundaries and longest prefix, not sibling works', () => {
  const profiles = [
    {url:'https://reader.test/manga/a', instructions:'반말', glossary:'A=에이'},
    {url:'https://reader.test/manga/a/special', instructions:'존댓말', glossary:''},
  ];
  assert.equal(workSettings('https://reader.test/manga/a/1', profiles).instructions, '반말');
  assert.equal(workSettings('https://reader.test/manga/ab/1', profiles).instructions, '');
  assert.equal(workSettings('https://reader.test/manga/a/special/2', profiles).instructions, '존댓말');
});
test('font size actions clamp and reset predictably', () => {
  assert.equal(nextFontScale(1, 'up'),1.1);
  assert.equal(nextFontScale(1.5, 'up'),1.5);
  assert.equal(nextFontScale(.7, 'down'),.7);
  assert.equal(nextFontScale(1.4, 'reset'),1);
});
test('retry preserves successful and hand-edited dialogue', () => {
  assert.ok(needsRetry({status:'failed',edited:false}));
  assert.ok(needsRetry({status:'review',tgt:null,edited:false}));
  assert.equal(needsRetry({status:'review',tgt:'확인할 번역',edited:false}),false);
  assert.equal(needsRetry({status:'done',tgt:'완료',edited:false}),false);
  assert.equal(needsRetry({status:'failed',tgt:'내 번역',edited:true}),false);
});
test('speech presets leave identity decisions to the reader', () => {
  assert.match(speechPreset('casual'),/반말/);
  assert.match(speechPreset('polite'),/존댓말/);
  assert.match(speechPreset('natural'),/인물/);
  assert.equal(speechPreset('custom'),'');
});
test('small text in a large bubble does not acquire a bubble-sized overlay',()=>{
  const s={bbox:[180,190,40,20],src:'안녕',presentation:{frame:[100,100,240,280]}};
  const [x,y,w,h]=sourceFrame(s,500,500);
  assert.ok(w*h<=40*20*1.6);
  assert.ok(Math.abs(x+w/2-200)<1 && Math.abs(y+h/2-200)<1);
});
test('unverified vertical text does not widen over neighboring artwork',()=>{
  const [x,y,w,h]=sourceFrame({bbox:[90,10,20,300],src:'a'},500,500);
  assert.ok(w<=25 && h<=310 && x>=0 && y>=0);
});
test('adapted frame stays inside safe bubble and image at edges',()=>{
  const [x,y,w,h]=sourceFrame({bbox:[0,0,20,50],presentation:{frame:[2,3,30,40]}},100,100);
  assert.ok(x>=2 && y>=3 && x+w<=32 && y+h<=43);
});
test('short translation does not inflate beyond estimated original font',()=>{
  const size=sourceFontSize({bbox:[10,10,90,20],src:'HELLO WORLD'});
  const r=fitText('안녕',300,200,measure,1,size);
  assert.ok(r.size<=size && r.size<20);
});

test('subscription read-ahead queues what is on screen and one image below, and no second one while busy', () => {
  const {planReadAhead} = require('../extension/reading.js');
  const item = (id, top, bottom, extra = {}) => ({id, top, bottom, left: 0, right: 500, queued: false, ...extra});
  const ids = (plan) => plan.map(p => [p.item.id, p.priority]);
  const items = [item('above', -900, -100), item('visible', 50, 700), item('next', 800, 1500), item('later', 1600, 2300)];
  assert.deepEqual(ids(planReadAhead(items, 500, 800, false)), [['visible', 2], ['next', 3]]);
  assert.deepEqual(ids(planReadAhead(items, 500, 800, true)), [['visible', 2]]);
  const queued = [item('visible', 50, 700, {queued: true}), item('next', 800, 1500)];
  assert.deepEqual(ids(planReadAhead(queued, 500, 800, false)), [['next', 3]]);
  const done = [item('visible', 50, 700), item('next', 800, 1500, {queued: true}), item('later', 1600, 2300)];
  assert.deepEqual(ids(planReadAhead(done, 500, 800, false)), [['visible', 2]]);
});
test('a horizontal row queues only the pages on screen, not the ones off to the side', () => {
  const {planReadAhead} = require('../extension/reading.js');
  // Four pages side by side, each 760px wide, in a 588px window: only the first one is on screen.
  const row = [0, 1, 2, 3].map(i => ({id: i, top: 40, bottom: 1117, left: 40 + i * 800, right: 800 + i * 800, queued: false}));
  const ids = (plan, reading) => plan.map(p => [p.item.id, p.priority]);
  // rtl: the next page is to the left, so nothing off-screen to the right is read ahead.
  assert.deepEqual(ids(planReadAhead(row, 588, 376, false, 'rtl'), 'rtl'), [[0, 2]]);
  // ltr: the next page is the one to the right of the window.
  assert.deepEqual(ids(planReadAhead(row, 588, 376, false, 'ltr'), 'ltr'), [[0, 2], [1, 3]]);
});
