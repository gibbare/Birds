/**
 * Enhetstester – faglar-observatorer.html kärnlogik
 * ===================================================
 * Kör: node tests/test_observatorer_logic.mjs
 *
 * Täcker (25 tester):
 *   esc               – HTML-escape
 *   filter-algoritm   – filtrering på namn/lokal/favorit
 *   sort-algoritm     – sortering på art (desc) + namn (sv)
 *   buildDetail       – sub/hyb-sektioner i HTML-output
 *   renderSpList      – artlistning, alfabetisk sortering
 *   paginerings-logik – PAGE_SIZE, slice-logik
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';
import vm from 'vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const htmlPath  = path.join(__dirname, '../faglar-observatorer.html');
const html      = readFileSync(htmlPath, 'utf-8');

const scriptBlocks = [];
const scriptRe = /<script(?![^>]*\bsrc\b)[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = scriptRe.exec(html)) !== null) scriptBlocks.push(m[1]);
const src = scriptBlocks.join('\n');

// ── Sandbox ─────────────────────────────────────────────────────────────
let _sbValue = '';
const domStore = {};
const sandbox = {
  // Globaler
  ALL_DATA: [],
  filtered:  [],
  favorites: new Set(),
  onlyFavs:  false,
  currentPage: 1,
  openRow:   null,
  PAGE_SIZE: 50,
  MONTHS: ['Jan','Feb','Mar','Apr','Maj','Jun','Jul','Aug','Sep','Okt','Nov','Dec'],

  document: {
    addEventListener() {},
    getElementById: (id) => {
      if (id === 'searchInput') return { value: _sbValue };
      if (!domStore[id]) domStore[id] = {
        textContent: '', innerHTML: '', style: { display: '' }, value: '',
        classList: { add(){}, remove(){}, contains(){ return false; } },
        prepend(){}, querySelectorAll(){ return []; },
      };
      return domStore[id];
    },
    createElement: () => ({ style: {}, innerHTML: '', className: '' }),
    querySelector: () => null,
    querySelectorAll: () => [],
  },
  window:       { addEventListener(){} },
  localStorage: { getItem(){ return null; }, setItem(){} },
  location:     { hostname: 'localhost', search: '' },
  console:      { log(){}, warn(){}, error(){} },
  fetch:        async () => ({}),
  AbortController: class { abort(){} signal = {} },
  setTimeout: () => {},
  clearTimeout: () => {},
  Set,
  Map,
  Promise,
  parseInt,
  String,
  Array,
  Object,
  Math,
  JSON,
  Intl,
};

vm.runInNewContext(src, sandbox);

const { esc, buildDetail, renderSpList } = sandbox;

// ── Testinfrastruktur ───────────────────────────────────────────────────
const OK  = '\x1b[92m[OK]\x1b[0m';
const ERR = '\x1b[91m[FEL]\x1b[0m';
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
// esc – HTML-escape
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}esc – HTML-escape${'\x1b[0m'}`);
eq('& escapes till &amp;',         esc('a & b'),       'a &amp; b');
eq('< escapes till &lt;',          esc('<script>'),     '&lt;script&gt;');
eq('> escapes till &gt;',          esc('5 > 3'),        '5 &gt; 3');
eq('" escapes till &quot;',        esc('say "hi"'),     'say &quot;hi&quot;');
eq('tom sträng → tom',             esc(''),             '');
eq('null → tom sträng',            esc(null),           '');

// ══════════════════════════════════════════════════════════════════════════
// filter-algoritm – testar kärn-logiken direkt (utan DOM-anrop)
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}filter-algoritm${'\x1b[0m'}`);

const DATA = [
  { name: 'Anna Andersson', art: 120, obs: 500, lokaler: [{ name: 'Sjön', obs: 200 }],    monthly: Array(12).fill(0), species: [], sub: 0, hyb: 0 },
  { name: 'Björn Berg',     art:  80, obs: 300, lokaler: [{ name: 'Skogen', obs: 100 }],  monthly: Array(12).fill(0), species: [], sub: 0, hyb: 0 },
  { name: 'Carla Carlsson', art: 150, obs: 700, lokaler: [{ name: 'Havet', obs: 350 }],   monthly: Array(12).fill(0), species: [], sub: 0, hyb: 0 },
];

// ── Hjälpfunktion som replikerar applyFilters-logiken ──
function runFilter(data, q, onlyFavs, favSet) {
  const ql = q.toLowerCase();
  return data
    .filter(r => {
      if (onlyFavs && !favSet.has(r.name)) return false;
      if (!ql) return true;
      const lokStr = r.lokaler.map(l => l.name).join(' ').toLowerCase();
      return `${r.name} ${lokStr}`.toLowerCase().includes(ql);
    })
    .sort((a, b) => b.art - a.art || a.name.localeCompare(b.name, 'sv'));
}

const noFilter = runFilter(DATA, '', false, new Set());
assert('ingen filter → alla 3', noFilter.length === 3);
assert('sorterad på art desc: Carla (150) först', noFilter[0].name === 'Carla Carlsson');
assert('secondary sort namn: Anna (120) före Björn (80)', noFilter[1].name === 'Anna Andersson');

const nameFilter = runFilter(DATA, 'björn', false, new Set());
assert('filter på namn: 1 träff', nameFilter.length === 1);
eq('rätt person', nameFilter[0].name, 'Björn Berg');

const lokalFilter = runFilter(DATA, 'havet', false, new Set());
assert('filter på lokal: 1 träff', lokalFilter.length === 1);

const favFilter = runFilter(DATA, '', true, new Set(['Anna Andersson']));
assert('onlyFavs: bara favorit', favFilter.length === 1);
eq('favoriten är Anna', favFilter[0].name, 'Anna Andersson');

const emptyFav = runFilter(DATA, '', true, new Set());
assert('onlyFavs med tom favoritlista → 0 resultat', emptyFav.length === 0);

// ══════════════════════════════════════════════════════════════════════════
// sort-algoritm med sekundär namnsortering
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}sort-algoritm${'\x1b[0m'}`);

const DATA_TIE = [
  { name: 'Östen',  art: 100, obs: 200, lokaler: [], monthly: [], species: [], sub: 0, hyb: 0 },
  { name: 'Anders', art: 100, obs: 300, lokaler: [], monthly: [], species: [], sub: 0, hyb: 0 },
  { name: 'Åsa',    art: 100, obs: 150, lokaler: [], monthly: [], species: [], sub: 0, hyb: 0 },
];
const sorted = runFilter(DATA_TIE, '', false, new Set());
assert('vid lika art: Anders < Åsa < Östen (sv-locale)', sorted[0].name === 'Anders');
assert('Åsa före Östen i sv-sortering',                  sorted[1].name === 'Åsa');

// ══════════════════════════════════════════════════════════════════════════
// buildDetail – sub/hyb-sektioner
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}buildDetail – sub/hyb-sektioner${'\x1b[0m'}`);

const R_SUB_HYB = {
  name: 'Anna', art: 50, obs: 200, sub: 3, hyb: 2,
  lokaler: [{ name: 'Sjön', obs: 100 }, { name: 'Skog', obs: 50 }],
  monthly: [10, 20, 30, 40, 50, 60, 50, 40, 30, 20, 10, 5],
  species: [{ sv: 'Kungsörn', obs: 10, ind: 12 }],
};

const detailHtml = buildDetail(R_SUB_HYB);
assert('buildDetail returnerar sträng', typeof detailHtml === 'string');
assert('sub-sektion visas när sub > 0',  detailHtml.includes('3 underarter'));
assert('hyb-sektion visas när hyb > 0',  detailHtml.includes('2 hybrider'));
assert('sub-list ID finns',              detailHtml.includes('sub-list-'));
assert('hyb-list ID finns',              detailHtml.includes('hyb-list-'));

const R_NO_SUB = { ...R_SUB_HYB, sub: 0, hyb: 0 };
const detailNoSub = buildDetail(R_NO_SUB);
assert('ingen sub-sektion när sub = 0', !detailNoSub.includes('underarter'));
assert('ingen hyb-sektion när hyb = 0', !detailNoSub.includes('hybrider'));

// ══════════════════════════════════════════════════════════════════════════
// renderSpList – artlistning
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}renderSpList – artlistning${'\x1b[0m'}`);

const mockEl = { innerHTML: '' };

const SPECIES = [
  { sv: 'Kungsörn', obs: 15, ind: 18 },
  { sv: 'Blåmes',   obs:  8, ind:  8 },
  { sv: 'Ärla',     obs:  5, ind:  6 },
];

renderSpList(mockEl, SPECIES);
assert('renderSpList producerar HTML',         mockEl.innerHTML.length > 0);
assert('alla 3 arter renderas',               (mockEl.innerHTML.match(/all-sp-item/g)||[]).length === 3);
assert('obs-antal visas',                      mockEl.innerHTML.includes('15 obs'));
assert('ind-antal visas',                      mockEl.innerHTML.includes('18'));
// Alfabetisk sortering: Blåmes < Kungsörn < Ärla
assert('Blåmes renderas före Kungsörn',        mockEl.innerHTML.indexOf('Blåmes') < mockEl.innerHTML.indexOf('Kungsörn'));

renderSpList(mockEl, []);
assert('tom lista visar hjälpmeddelande', mockEl.innerHTML.includes('Inga data'));

// ══════════════════════════════════════════════════════════════════════════
// paginerings-logik
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}paginerings-logik${'\x1b[0m'}`);

function slicePage(data, page, size) {
  const start = (page - 1) * size;
  return data.slice(start, start + size);
}

const BIG_DATA = Array.from({ length: 55 }, (_, i) => ({ name: `Person${i}` }));
eq('sida 1 ger PAGE_SIZE rader',  slicePage(BIG_DATA, 1, 50).length, 50);
eq('sida 2 ger resterande 5',     slicePage(BIG_DATA, 2, 50).length,  5);
eq('sida 3 är tom (> total)',     slicePage(BIG_DATA, 3, 50).length,  0);

function pageCount(total, size) { return Math.ceil(total / size); }
eq('55 rader → 2 sidor (size 50)', pageCount(55, 50), 2);
eq('50 rader → 1 sida (exakt)',    pageCount(50, 50), 1);
eq('0 rader → 0 sidor',           pageCount(0, 50),   0);

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
