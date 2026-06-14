/**
 * Enhetstester – Cloudflare Worker logik (buildObserverResult)
 * =============================================================
 * Kör: node tests/test_worker_logic.mjs
 *
 * Testar buildObserverResult() som konverterar R2-formatet till
 * API-svarsformatet med sub/hyb-räknare och top-3-listor.
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { createRequire } from 'module';
import path from 'path';
import vm from 'vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const workerSrc = path.join(__dirname, '../cloudflare-worker/src/index.js');

// Ladda worker-källkoden och ta bort 'export default { ... }' blocket
// så att vi kan köra funktionerna i ett vm-kontext
const src = readFileSync(workerSrc, 'utf-8')
  .replace(/\nexport default \{[\s\S]*/, '');   // ta bort export-blocket

// Kör i ett nytt sandboxat kontext – function-deklarationer blir
// egenskaper på sandbox-objektet (globalt i sandboxen)
const sandbox = {};
vm.runInNewContext(src, sandbox);

const { buildObserverResult } = sandbox;
if (typeof buildObserverResult !== 'function') {
  console.error('FATAL: buildObserverResult hittades inte i worker-källan');
  process.exit(1);
}

// ── Testinfrastruktur ────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    console.log(`  \x1b[92m[OK]\x1b[0m ${name}`);
    passed++;
  } catch (e) {
    console.log(`  \x1b[91m[FEL]\x1b[0m ${name}`);
    console.log(`       → ${e.message}`);
    failed++;
    failures.push(name);
  }
}

function assert(condition, msg) {
  if (!condition) throw new Error(msg || 'Assertion failed');
}

function assertEqual(a, b, msg) {
  if (a !== b) throw new Error(msg || `Förväntade ${JSON.stringify(b)}, fick ${JSON.stringify(a)}`);
}

function assertDeepEqual(a, b, msg) {
  const as = JSON.stringify(a), bs = JSON.stringify(b);
  if (as !== bs) throw new Error(msg || `\n  Förväntade: ${bs}\n  Fick:       ${as}`);
}

// ── Testdata ─────────────────────────────────────────────────────────────────

/** Bygg ett minimalt R2-reporters-objekt i nytt kompaktformat */
function makeReporter({
  obs = 10, art = 5, sub = 0, hyb = 0, dagar = 3,
  lastObs = '2026-04-01', monthly = Array(12).fill(0),
  sp = [], subsp = [], hybsp = [], pl = [],
} = {}) {
  return {
    obs, art, sub, hyb, dagar, lastObs, monthly,
    sp, subsp, hybsp, pl,
  };
}

function makeR2Data(reporters) {
  return { reporters };
}

// ── Tester ────────────────────────────────────────────────────────────────────

console.log('\n\x1b[1;34mcloudflare-worker: buildObserverResult()\x1b[0m');

test('Tom data ger tom array', () => {
  assertDeepEqual(buildObserverResult({}), []);
  assertDeepEqual(buildObserverResult({ reporters: {} }), []);
  assertDeepEqual(buildObserverResult(null), []);
});

test('Returnerar en post per reporter', () => {
  const data = makeR2Data({
    'Kalle': makeReporter({ art: 3 }),
    'Lisa':  makeReporter({ art: 7 }),
  });
  const res = buildObserverResult(data);
  assertEqual(res.length, 2);
});

test('Sorterar efter art (störst först)', () => {
  const data = makeR2Data({
    'Kalle': makeReporter({ art: 3 }),
    'Lisa':  makeReporter({ art: 10 }),
    'Nils':  makeReporter({ art: 7 }),
  });
  const res = buildObserverResult(data);
  assertEqual(res[0].name, 'Lisa');
  assertEqual(res[1].name, 'Nils');
  assertEqual(res[2].name, 'Kalle');
});

test('sub-räknare skickas med', () => {
  const data = makeR2Data({
    'Kalle': makeReporter({ art: 5, sub: 3 }),
  });
  const res = buildObserverResult(data);
  assertEqual(res[0].sub, 3);
});

test('hyb-räknare skickas med', () => {
  const data = makeR2Data({
    'Kalle': makeReporter({ art: 5, hyb: 2 }),
  });
  const res = buildObserverResult(data);
  assertEqual(res[0].hyb, 2);
});

test('art, obs, dagar, lastObs skickas korrekt', () => {
  const data = makeR2Data({
    'Kalle': makeReporter({ art: 5, obs: 100, dagar: 30, lastObs: '2026-04-15' }),
  });
  const r = buildObserverResult(data)[0];
  assertEqual(r.art,     5);
  assertEqual(r.obs,     100);
  assertEqual(r.dagar,   30);
  assertEqual(r.lastObs, '2026-04-15');
});

test('monthly-array skickas med (12 element)', () => {
  const monthly = [1,0,3,5,2,0,0,0,0,1,2,0];
  const data = makeR2Data({ 'Kalle': makeReporter({ monthly }) });
  const r = buildObserverResult(data)[0];
  assertDeepEqual(r.monthly, monthly);
});

test('top-3 artlista (sp) skickas med', () => {
  const sp = [
    { sv: 'talgoxe', obs: 10, ind: 10 },
    { sv: 'blåmes',  obs: 8,  ind: 8  },
    { sv: 'pilfink', obs: 5,  ind: 5  },
  ];
  const data = makeR2Data({ 'Kalle': makeReporter({ sp }) });
  const r = buildObserverResult(data)[0];
  assertEqual(r.species.length, 3);
  assertEqual(r.species[0].sv, 'talgoxe');
});

test('subsp top-3 lista skickas med', () => {
  const subsp = [
    { sv: 'nordlig talgoxe',   obs: 4, ind: 4 },
    { sv: 'mellaneuropeisk ME', obs: 2, ind: 2 },
  ];
  const data = makeR2Data({ 'Kalle': makeReporter({ sub: 2, subsp }) });
  const r = buildObserverResult(data)[0];
  assert(Array.isArray(r.subsp), 'subsp ska vara en array');
  assertEqual(r.subsp.length, 2);
  assertEqual(r.subsp[0].sv, 'nordlig talgoxe');
});

test('hybsp top-3 lista skickas med', () => {
  const hybsp = [{ sv: 'grågås x kanadagås', obs: 1, ind: 1 }];
  const data = makeR2Data({ 'Kalle': makeReporter({ hyb: 1, hybsp }) });
  const r = buildObserverResult(data)[0];
  assert(Array.isArray(r.hybsp), 'hybsp ska vara en array');
  assertEqual(r.hybsp[0].sv, 'grågås x kanadagås');
});

test('topLokal hämtas från pl[0]', () => {
  const pl = [
    { name: 'Umeå fjärd', obs: 50 },
    { name: 'Örnsköldsviksviken', obs: 30 },
  ];
  const data = makeR2Data({ 'Kalle': makeReporter({ pl }) });
  const r = buildObserverResult(data)[0];
  assertEqual(r.topLokal, 'Umeå fjärd');
});

test('lokaler top-3 skickas med korrekt', () => {
  const pl = [
    { name: 'Lokal A', obs: 100 },
    { name: 'Lokal B', obs: 80  },
    { name: 'Lokal C', obs: 60  },
    { name: 'Lokal D', obs: 40  },
  ];
  const data = makeR2Data({ 'Kalle': makeReporter({ pl }) });
  const r = buildObserverResult(data)[0];
  assertEqual(r.lokaler.length, 3);
  assertEqual(r.lokaler[0].name, 'Lokal A');
});

test('Bakåtkompatibilitet: gammalt format med species/places-objekt', () => {
  // Gammalt format utan sp/subsp/hybsp-listor
  const oldReporter = {
    obs: 20,
    dagar: 5,
    lastObs: '2026-03-01',
    monthly: Array(12).fill(0),
    species: {
      '100': { sv: 'talgoxe', obs: 5, ind: 5 },
      '200': { sv: 'blåmes',  obs: 3, ind: 3 },
    },
    places: {
      'Umeå': 10,
      'Luleå': 5,
    },
  };
  const data = makeR2Data({ 'Kalle': oldReporter });
  const r = buildObserverResult(data)[0];
  // art räknas från keys i species
  assertEqual(r.art, 2);
  assertEqual(r.topLokal, 'Umeå');
  assert(r.species.length > 0, 'species-lista ska finnas');
});

test('sub och hyb defaultar till 0 om de saknas', () => {
  const data = makeR2Data({ 'Kalle': makeReporter({ sub: undefined, hyb: undefined }) });
  const r = buildObserverResult(data)[0];
  assertEqual(r.sub, 0);
  assertEqual(r.hyb, 0);
});

test('monthly defaultar till 12 nollor om det saknas', () => {
  const rep = makeReporter();
  delete rep.monthly;
  const data = makeR2Data({ 'Kalle': rep });
  const r = buildObserverResult(data)[0];
  assert(Array.isArray(r.monthly), 'monthly ska vara array');
  assertEqual(r.monthly.length, 12);
  assert(r.monthly.every(v => v === 0), 'monthly ska vara nollor');
});

// ── Sammanfattning ────────────────────────────────────────────────────────────

console.log(`\n${'═'.repeat(52)}`);
if (failed > 0) {
  console.log(`\x1b[91m[FEL]\x1b[0m ${failed} test(er) misslyckades:\n`);
  failures.forEach(f => console.log(`   • ${f}`));
  console.log('');
  process.exit(1);
} else {
  console.log(`\x1b[92m[OK]\x1b[0m Alla ${passed} tester godkända.`);
  process.exit(0);
}
