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
const start = Date.parse('2026-10-01T00:00:00Z')/1000; // Thursday 00:00.
const cell = (weekday,hour) => weekday*24+hour; // Monday-first week: Monday 00:00 is index 0.
const THURSDAY = cell(3,0); // Oct 1 2026 is a Thursday, day 3 of a Monday-first week.
const close = (actual, expected) => assert.ok(Math.abs(actual-expected)<1e-6, `${actual} != ${expected}`);

test('one sample means collecting, not zero activity', () => {
  const result = activity([[start,100]]);
  assert.equal(result.cells.length,168);
  assert.equal(result.rate,null);
  assert.equal(result.seconds,0);
  assert.ok(result.cells.every(c=>c.seconds===0));
});

test('rates are weighted by observed duration', () => {
  const result = activity([[start,100],[start+900,110],[start+1200,120]]);
  close(result.rate,60);
  close(result.cells[THURSDAY].seconds,1200);
  close(result.cells[THURSDAY].posts,20);
  assert.equal(result.cells[THURSDAY+1].seconds,0);
});

test('a measured zero is distinct from a missing hour', () => {
  const result = activity([[start,100],[start+900,100]]);
  assert.equal(result.rate,0);
  assert.equal(result.cells[THURSDAY].seconds,900);
  assert.equal(result.cells[THURSDAY+1].seconds,0);
});

test('intervals crossing a clock hour are apportioned', () => {
  const result = activity([[start+3300,100],[start+4200,130]]);
  close(result.cells[THURSDAY].posts,10);
  close(result.cells[THURSDAY+1].posts,20);
  assert.equal(result.cells[THURSDAY].seconds,300);
  assert.equal(result.cells[THURSDAY+1].seconds,600);
});

test('large gaps and counter decreases are excluded, later intervals recover', () => {
  const result = activity([[start,100],[start+3600,500],[start+4500,400],[start+5400,410]]);
  assert.equal(result.rejected,2);
  assert.equal(result.intervals,1);
  close(result.rate,40);
  assert.equal(result.seconds,900);
});

test('28 days give every weekday-hour cell equal exposure', () => {
  const samples = Array.from({length:28*96+1},(_,i)=>[start+i*900,100+i*10]);
  const result = activity(samples);
  close(result.rate,40);
  assert.equal(result.cells.length,168);
  for (const c of result.cells) { // 28 days hold four of each weekday.
    close(c.seconds,4*3600);
    close(c.posts,4*40);
  }
});

test('half-hour timezone offsets work', () => {
  process.env.TZ='Asia/Kolkata';
  const result = activity([[start+900,0],[start+2700,60]]); // 05:45–06:15 local.
  assert.equal(result.cells[cell(3,5)].seconds,900);
  assert.equal(result.cells[cell(3,6)].seconds,900);
  process.env.TZ='UTC';
});

test('intervals crossing local midnight roll from Sunday into Monday', () => {
  const midnight = Date.parse('2026-10-04T23:30:00Z')/1000; // Sunday evening.
  const samples = Array.from({length:5},(_,i)=>[midnight+i*900,i*30]);
  const result = activity(samples); // Sunday 23:30 – Monday 00:30 at 120 posts/hour.
  close(result.cells[cell(6,23)].seconds,1800); // Sunday 23:00 keeps its half hour…
  close(result.cells[cell(6,23)].posts,60);
  close(result.cells[cell(0,0)].seconds,1800); // …and Monday 00:00 restarts the week.
  close(result.cells[cell(0,0)].posts,60);
});

test('the weekday comes from local time, not UTC', () => {
  const samples = [[start-3600,0],[start-1800,60]]; // Wed 23:00 UTC = Thu 08:00 Tokyo.
  process.env.TZ='Asia/Tokyo';
  assert.equal(activity(samples).cells[cell(3,8)].seconds,1800); // Thursday locally…
  assert.equal(activity(samples).cells[cell(2,23)].seconds,0); // …not Wednesday 23:00 as in UTC.
  process.env.TZ='UTC';
  assert.equal(activity(samples).cells[cell(2,23)].seconds,1800);
});

test('weekday cells are normalized against the whole-week baseline, not their own day', () => {
  const block = (t0,from,to) => Array.from({length:5},(_,i)=>[t0+i*900,from+(to-from)*i/4]);
  const samples = [...block(start,0,60),...block(start+86400,60,300)]; // Thu and Fri 00:00–01:00.
  const result = normalizedActivity(samples);
  close(result.rate,150);
  close(result.rates[cell(3,0)],60);
  close(result.rates[cell(4,0)],240);
  close(result.relative[cell(3,0)],0.4); // 60/150: one shared baseline, so a quiet Thursday…
  close(result.relative[cell(4,0)],1.6); // …and a busy Friday both compare against the same 1×.
});

test('normalization compares timing, not instance size', () => {
  const samples = [[start,0],[start+1800,30],[start+3600,60],[start+5400,150]];
  const small = normalizedActivity(samples);
  const large = normalizedActivity(samples.map(([ts,count]) => [ts,count*10000]));
  close(small.rate,100); // 150 posts / 1.5 observed hours, not a mean of hourly means.
  close(small.relative[THURSDAY],0.6);
  close(small.relative[THURSDAY+1],1.8);
  assert.equal(small.relative[THURSDAY+2],null);
  assert.equal(small.relative.length,168);
  assert.deepEqual(small.relative,large.relative);
});

test('zero-volume and missing activity cannot be normalized', () => {
  const zero = normalizedActivity([[start,100],[start+900,100]]);
  assert.equal(zero.rate,0);
  assert.equal(zero.rates[THURSDAY],0);
  assert.ok(zero.relative.every(value => value === null));
  assert.ok(normalizedActivity([]).relative.every(value => value === null));
});

test('measured zeros remain on the chart when there is a nonzero baseline', () => {
  const result = normalizedActivity([[start,0],[start+1800,30],[start+3600,60],[start+5400,60]]);
  assert.equal(result.relative[THURSDAY+1],0);
  assert.equal(result.relative[THURSDAY+2],null);
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

test('activityColor uses one fixed 0–2 scale for every server', () => {
  assert.equal(typeof activityColor(1),'string');
  assert.equal(activityColor(-3),activityColor(0)); // clamped below the scale…
  assert.equal(activityColor(9),activityColor(2)); // …and above it.
  assert.notEqual(activityColor(0),activityColor(2));
});

test('DST fallback repeats the local hour without losing coverage', () => {
  process.env.TZ='Europe/Berlin';
  const base=Date.parse('2026-10-25T00:45:00Z')/1000;
  const result=activity([[base,0],[base+1800,60]]);
  assert.equal(result.cells[cell(6,2)].seconds,1800); // Sunday hour 2 happens twice.
  close(result.cells[cell(6,2)].posts,60);
  process.env.TZ='UTC';
});

test('DST spring forward skips the missing hour without losing coverage', () => {
  process.env.TZ='Europe/Berlin';
  const base=Date.parse('2027-03-28T00:30:00Z')/1000; // local 01:30–03:30 across the jump.
  const samples = Array.from({length:5},(_,i)=>[base+i*900,i*30]);
  const result=activity(samples); // 120 posts/hour, split around the missing 02:00.
  close(result.seconds,3600);
  assert.equal(result.cells[cell(6,1)].seconds,1800); // Sunday 01:00, then 03:00;
  assert.equal(result.cells[cell(6,3)].seconds,1800);
  assert.equal(result.cells[cell(6,2)].seconds,0); // hour 02:00 never happens.
  close(result.cells[cell(6,1)].posts,60);
  close(result.cells[cell(6,3)].posts,60);
  process.env.TZ='UTC';
});
