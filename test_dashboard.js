// Dependency-free tests of the exact activity calculation used by the page.
// Run: node --test test_dashboard.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(__dirname+'/dashboard.html','utf8');
const source = html.slice(html.indexOf('function activity(samples)'), html.indexOf('function warning('));
vm.runInThisContext(source);
process.env.TZ = 'UTC';
const start = Date.parse('2026-10-01T00:00:00Z')/1000;
const close = (actual, expected) => assert.ok(Math.abs(actual-expected)<1e-6, `${actual} != ${expected}`);

test('one sample means collecting, not zero activity', () => {
  const result = activity([[start,100]]);
  assert.equal(result.rate,null);
  assert.equal(result.seconds,0);
  assert.ok(result.cells.every(cell=>cell.seconds===0));
});

test('rates are weighted by observed duration', () => {
  const result = activity([[start,100],[start+900,110],[start+1200,120]]);
  close(result.rate,60);
  close(result.cells[0].seconds,1200);
  close(result.cells[0].posts,20);
  assert.equal(result.cells[1].seconds,0);
});

test('a measured zero is distinct from a missing hour', () => {
  const result = activity([[start,100],[start+900,100]]);
  assert.equal(result.rate,0);
  assert.equal(result.cells[0].seconds,900);
  assert.equal(result.cells[1].seconds,0);
});

test('intervals crossing a clock hour are apportioned', () => {
  const result = activity([[start+3300,100],[start+4200,130]]);
  close(result.cells[0].posts,10);
  close(result.cells[1].posts,20);
  assert.equal(result.cells[0].seconds,300);
  assert.equal(result.cells[1].seconds,600);
});

test('large gaps and counter decreases are excluded, later intervals recover', () => {
  const result = activity([[start,100],[start+3600,500],[start+4500,400],[start+5400,410]]);
  assert.equal(result.rejected,2);
  assert.equal(result.intervals,1);
  close(result.rate,40);
  assert.equal(result.seconds,900);
});

test('28 daily observations have equal exposure across all 24 hours', () => {
  const samples = Array.from({length:28*96+1},(_,i)=>[start+i*900,100+i*10]);
  const result = activity(samples);
  close(result.rate,40);
  for (const cell of result.cells) {
    close(cell.seconds,28*3600);
    close(cell.posts,28*40);
  }
});

test('half-hour timezone offsets work', () => {
  process.env.TZ='Asia/Kolkata';
  const result = activity([[start+900,0],[start+2700,60]]);
  assert.equal(result.cells[5].seconds,900);
  assert.equal(result.cells[6].seconds,900);
  process.env.TZ='UTC';
});

test('normalization compares timing, not instance size', () => {
  const samples = [[start,0],[start+1800,30],[start+3600,60],[start+5400,150]];
  const small = normalizedActivity(samples);
  const large = normalizedActivity(samples.map(([ts,count]) => [ts,count*10000]));
  close(small.rate,100); // 150 posts / 1.5 observed hours, not a mean of hourly means.
  close(small.relative[0],0.6);
  close(small.relative[1],1.8);
  assert.equal(small.relative[2],null);
  assert.deepEqual(small.relative,large.relative);
});

test('zero-volume and missing activity cannot be normalized', () => {
  const zero = normalizedActivity([[start,100],[start+900,100]]);
  assert.equal(zero.rate,0);
  assert.equal(zero.rates[0],0);
  assert.ok(zero.relative.every(value => value === null));
  assert.ok(normalizedActivity([]).relative.every(value => value === null));
});

test('measured zeros remain on the chart when there is a nonzero baseline', () => {
  const result = normalizedActivity([[start,0],[start+1800,30],[start+3600,60],[start+5400,60]]);
  assert.equal(result.relative[1],0);
  assert.equal(result.relative[2],null);
});

test('lines stop at missing hours instead of inventing connections', () => {
  const segments = activitySegments([null,1,2,null,0,null,3,4,null]);
  assert.deepEqual(segments,[[{hour:1,value:1},{hour:2,value:2}],[{hour:4,value:0}],[{hour:6,value:3},{hour:7,value:4}]]);
  assert.deepEqual(activitySegments(Array(24).fill(null)),[]);
});

const makeTag = (name,uses=10) => ({name,history:[{day:start,uses,accounts:2}]});
const server = (name,tags,error=null) => ({name,tags,trends_error:error});

test('tags deduplicate across case and Unicode forms, but not servers', () => {
  const merged = mergeTrends([
    server('a.social',[makeTag('Café',10),makeTag('CAFÉ',999)]),
    server('b.social',[makeTag('CAFE\u0301',20)]),
  ]);
  assert.equal(merged.length,1);
  assert.equal(merged[0].entries.length,2);
  assert.deepEqual(merged[0].entries.map(entry=>entry.tag.history[0].uses),[10,20]);
  assert.ok(!Object.hasOwn(merged[0],'uses')); // No double-counted cross-server total.
});

test('shared tags rank first, then average API rank rather than post counts', () => {
  const merged = mergeTrends([
    server('a.social',[makeTag('first',1),makeTag('popular',100000),makeTag('shared',2)]),
    server('b.social',[makeTag('shared',2),makeTag('FIRST',1)]),
  ]);
  assert.deepEqual(merged.map(tag=>tag.name),['first','shared','popular']);
});

test('failed sources contribute neither tags nor badges', () => {
  const merged = mergeTrends([
    server('a.social',[makeTag('hello')]),
    server('b.social',[makeTag('HELLO')],'HTTP 503'),
    server('c.social',null,'HTTP 429'),
    server('d.social',[]),
  ]);
  assert.equal(merged[0].entries.length,1);
  assert.equal(merged[0].entries[0].server.name,'a.social');
  assert.deepEqual(mergeTrends([server('a.social',[])]),[]);
});

test('DST fallback repeats the local hour without losing coverage', () => {
  process.env.TZ='Europe/Berlin';
  const base=Date.parse('2026-10-25T00:45:00Z')/1000;
  const result=activity([[base,0],[base+1800,60]]);
  assert.equal(result.cells[2].seconds,1800);
  close(result.cells[2].posts,60);
  process.env.TZ='UTC';
});
