/**
 * Enhetstester – faglar-statistik.html kärnlogik
 * ================================================
 * Kör: node tests/test_statistik_logic.mjs
 *
 * Täcker (25 tester):
 *   esc            – HTML-escape
 *   fmtCachedAt    – datumformatering
 *   _muniMonthly   – kommunmånadsvärden
 *   updateKpi      – KPI-datakälla (helår/månad/kommun/kombinerat)
 *   filterLists    – sökning i artlista/rapportörlista
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';
import vm from 'vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const htmlPath  = path.join(__dirname, '../faglar-statistik.html');
const html      = readFileSync(htmlPath, 'utf-8');

const scriptBlocks = [];
const scriptRe = /<script(?![^>]*\bsrc\b)[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = scriptRe.exec(html)) !== null) scriptBlocks.push(m[1]);
const src = scriptBlocks.join('\n');

// ── Sandbox ─────────────────────────────────────────────────────────────
const domStore = {};
const kpiVals  = {};

const sandbox = {
  selectedYear:    2025,
  selectedMonth:   0,
  selectedCountyId: '24',
  statsData:       {},
  pollTimer:       null,
  _currentSp:      [],
  _currentRep:     [],
  _rapporterAll:   [],
  _rapporterShown: 0,
  RAP_PAGE:        20,
  _mapVisible:     false,
  _mapSelection:   null,
  DEFAULT_COUNTY_ID: '24',
  MÅNADER_FULL: ['Januari','Februari','Mars','April','Maj','Juni','Juli','Augusti','September','Oktober','November','December'],
  MONTHS: ['Jan','Feb','Mar','Apr','Maj','Jun','Jul','Aug','Sep','Okt','Nov','Dec'],
  COUNTIES: [{ id: '24', name: 'Västerbottens län' }],

  document: {
    addEventListener() {},
    getElementById: (id) => {
      if (!domStore[id]) domStore[id] = {
        textContent: '', innerHTML: '', style: { display: '' }, value: '',
        classList: { add(){}, remove(){}, contains(){ return false; }, toggle(){} },
        options: [{ text: 'Västerbottens län' }],
        selectedIndex: 0,
      };
      return domStore[id];
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ style: {}, innerHTML: '', className: '' }),
  },
  window:       { addEventListener(){} },
  localStorage: { getItem(){ return null; }, setItem(){} },
  location:     { hostname: 'localhost', search: '' },
  console:      { log(){}, warn(){}, error(){} },
  fetch:        async () => ({ ok: true, json: async () => ({}) }),
  AbortController: class { abort(){} signal = {} },
  setTimeout: () => {},
  clearTimeout: () => {},
  Set,
  Map,
  Promise,
  parseInt,
  parseFloat,
  String,
  Number,
  Array,
  Object,
  Math,
  JSON,
  Intl,
  Date,
  isNaN,
};

vm.runInNewContext(src, sandbox);
const { esc, fmtCachedAt, _muniMonthly, updateKpi, filterLists } = sandbox;

// ── Testinfrastruktur ───────────────────────────────────────────────────
const OK   = '\x1b[92m[OK]\x1b[0m';
const ERR  = '\x1b[91m[FEL]\x1b[0m';
const HEAD = '\x1b[1;34m';
let failures = 0;

function assert(desc, cond, detail = '') {
  if (cond) console.log(`  ${OK} ${desc}`);
  else { console.log(`  ${ERR} ${desc}${detail ? '\n       → ' + detail : ''}`); failures++; }
}
function eq(desc, a, b) {
  assert(desc, a === b, `fick: ${JSON.stringify(a)}  väntat: ${JSON.stringify(b)}`);
}

// ══════════════════════════════════════════════════════════════════════════
// esc
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}esc – HTML-escape${'\x1b[0m'}`);
eq('& escapes', esc('a & b'), 'a &amp; b');
eq('< escapes', esc('<tag>'),  '&lt;tag&gt;');
eq('null → tom', esc(null),    '');
eq('siffra → sträng', esc(42), '42');

// ══════════════════════════════════════════════════════════════════════════
// fmtCachedAt
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}fmtCachedAt – datumformatering${'\x1b[0m'}`);
assert('giltig ISO returnerar ej tom sträng',  fmtCachedAt('2025-05-01T10:30:00').length > 0);
assert('innehåller 2025',                      fmtCachedAt('2025-05-01T10:30:00').includes('2025'));
assert('null → tom sträng',                    fmtCachedAt(null) === '');
assert('tom sträng → tom',                     fmtCachedAt('') === '');

// ══════════════════════════════════════════════════════════════════════════
// _muniMonthly
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}_muniMonthly – kommunmånadsaggregering${'\x1b[0m'}`);

const MUNI_DATA = {
  muni_month_species: {
    '0880': {
      5: [{ sv: 'Kungsörn', obs: 10, ind: 12 }, { sv: 'Blåmes', obs: 5, ind: 5 }],
      6: [{ sv: 'Ärla', obs: 8, ind: 9 }],
    },
  },
};

const monthly = _muniMonthly(MUNI_DATA, '0880');
eq('returnerar 12 element',     monthly.length, 12);
eq('maj (index 4) = 15',       monthly[4], 15);   // obs 10+5
eq('juni (index 5) = 8',       monthly[5], 8);
eq('januari (index 0) = 0',    monthly[0], 0);

// Saknad kommun → 12 nollor
const emptyMonthly = _muniMonthly({}, '9999');
assert('saknad kommun → 12 nollor', emptyMonthly.every(v => v === 0));

// ══════════════════════════════════════════════════════════════════════════
// updateKpi – datakälla-logik
// (updateKpi sätter DOM-element; vi läser domStore-värden efter anrop)
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}updateKpi – KPI-datakälla${'\x1b[0m'}`);

const STATS_D = {
  kpi: { arter: 180, obs: 5000, ind: 8000, reporters: 42 },
  month_species: {
    5: [
      { sv: 'Kungsörn', obs: 20, ind: 25 },
      { sv: 'Blåmes',   obs: 10, ind: 10 },
    ],
  },
  month_reporters: {
    5: [{ name: 'Anna', obs: 15, arter: 30 }, { name: 'Björn', obs: 10, arter: 20 }],
  },
  muni_species: {
    '0880': [{ sv: 'Kungsörn', obs: 8, ind: 10 }],
  },
  muni_reporters: {
    '0880': [{ name: 'Anna', obs: 8, arter: 15 }],
  },
  muni_month_species: {
    '0880': {
      6: [{ sv: 'Blåmes', obs: 3, ind: 3 }],
    },
  },
  muni_month_reporters: {
    '0880': {
      6: [{ name: 'Björn', obs: 3, arter: 5 }],
    },
  },
};

// Helår, hela länet → d.kpi
updateKpi(STATS_D, 0, null);
eq('helår arter = 180', domStore['kpiArter']?.textContent?.replace(/\s/g,''), '180');
assert('helår obs innehåller 5000', (domStore['kpiObs']?.textContent||'').replace(/[s  ]/g,'').includes('5000'));


// Månadsfilter → month_species/reporters
updateKpi(STATS_D, 5, null);
assert('månadsfilter: arter = längden på month_species[5]',
  domStore['kpiArter']?.textContent === '2');
assert('månadsfilter: rapportörer = 2',
  domStore['kpiRap']?.textContent === '2');

// Kommunfilter → muni_species/reporters
updateKpi(STATS_D, 0, '0880');
assert('kommunfilter: arter = 1',       domStore['kpiArter']?.textContent === '1');
assert('kommunfilter: rapportörer = 1', domStore['kpiRap']?.textContent === '1');

// Kommun + månad → muni_month_species/reporters
updateKpi(STATS_D, 6, '0880');
assert('kommun+månad: arter = 1',       domStore['kpiArter']?.textContent === '1');
assert('kommun+månad: rapportörer = 1', domStore['kpiRap']?.textContent === '1');

// Fallback: saknad muni_month → faller till muni_species
updateKpi(STATS_D, 7, '0880');  // månad 7 finns ej i muni_month → faller till muni[0880]
assert('fallback månads→kommunnivå', domStore['kpiArter']?.textContent === '1');

// ══════════════════════════════════════════════════════════════════════════
// filterLists
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}filterLists – sökning i artlista/rapportörlista${'\x1b[0m'}`);

// filterLists uppdaterar DOM direkt (returnerar inget).
// Vi testar filterlogiken via inline-replikat + DOM-verifiering.

const SP_LIST  = [
  { sv: 'Kungsörn', sci: 'Aquila chrysaetos', obs: 10, ind: 12 },
  { sv: 'Blåmes',   sci: 'Cyanistes caeruleus', obs: 5, ind: 5 },
];
const REP_LIST = [
  { name: 'Anna Andersson', obs: 200, art: 80 },
  { name: 'Björn Berg',     obs: 150, art: 60 },
];

function applyFilter(sp, rep, q) {
  const ql = (q || '').toLowerCase().trim();
  if (!ql) return { filtSp: sp, filtRep: rep };
  return {
    filtSp:  sp.filter(a => (a.sv  || '').toLowerCase().includes(ql)),
    filtRep: rep.filter(r => (r.name|| '').toLowerCase().includes(ql)),
  };
}

const all = applyFilter(SP_LIST, REP_LIST, '');
assert('tom sökning → alla arter',       all.filtSp.length  === 2);
assert('tom sökning → alla rapportörer', all.filtRep.length === 2);

const spMatch = applyFilter(SP_LIST, REP_LIST, 'kungsörn');
assert('artmatch → 1 art',              spMatch.filtSp.length  === 1);
assert('artmatch → inga rapportörer',   spMatch.filtRep.length === 0);

const repMatch = applyFilter(SP_LIST, REP_LIST, 'anna');
assert('rapportörsmatch → inga arter',  repMatch.filtSp.length  === 0);
assert('rapportörsmatch → 1 rapportör', repMatch.filtRep.length === 1);

const noMatch = applyFilter(SP_LIST, REP_LIST, 'xxxxxx');
assert('inget match → 0 arter',         noMatch.filtSp.length  === 0);
assert('inget match → 0 rapportörer',   noMatch.filtRep.length === 0);

// Verifiera att filterLists anropar DOM korrekt (DOM-smoke-test)
sandbox._currentSp  = SP_LIST;
sandbox._currentRep = REP_LIST;
filterLists('kungsörn');
assert('filterLists fyller topArtList med HTML',
  (domStore['topArtList']?.innerHTML || '').length > 0);
filterLists('');
assert('filterLists med tom query → topArtList fylls',
  (domStore['topArtList']?.innerHTML || '').length > 0);

// ══════════════════════════════════════════════════════════════════════════
// Sammanfattning
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${'═'.repeat(52)}`);
if (failures > 0) {
  console.log(`\x1b[91m[FEL]\x1b[0m ${failures} test(er) misslyckades`);
  process.exit(1);
} else {
  console.log(`\x1b[92m[OK]\x1b[0m Alla tester godkända.`);
}
