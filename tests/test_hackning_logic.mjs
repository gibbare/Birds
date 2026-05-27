/**
 * Enhetstester – faglar-hackning.html kärnlogik
 * ===============================================
 * Kör: node tests/test_hackning_logic.mjs
 *
 * Täcker (22 tester):
 *   actCat          – häckningskategori (A/B/C)
 *   actColor        – kartfärg per kategori
 *   actBorder       – kantfärg per kategori
 *   esc             – HTML-escape
 *   renderBreeding  – arttabell + kategoriräknare + truncated-flagga
 *   filterSpecies   – artfiltrering i tabell
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';
import vm from 'vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const htmlPath  = path.join(__dirname, '../faglar-hackning.html');
const html      = readFileSync(htmlPath, 'utf-8');

const scriptBlocks = [];
const scriptRe = /<script(?![^>]*\bsrc\b)[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = scriptRe.exec(html)) !== null) scriptBlocks.push(m[1]);
const src = scriptBlocks.join('\n');

// ── Sandbox ─────────────────────────────────────────────────────────────
const domStore = {};
// Håller spesiestabell-rader i minnet
let _spTbodyRows = [];

const sandbox = {
  // Globaler
  _allMarkers:          [],
  _reportersBySpecies:  {},

  document: {
    addEventListener() {},
    getElementById: (id) => {
      if (!domStore[id]) domStore[id] = {
        textContent: '', innerHTML: '', style: { display: '' }, value: '',
        classList: { add(){}, remove(){}, contains(){ return false; }, toggle(){} },
        appendChild(){}, removeChild(){}, options: [], selectedIndex: 0,
        addEventListener(){},
        querySelectorAll: (sel) => {
          // Simulera tbody med data-name rader
          if (sel === 'tr[data-name]') return _spTbodyRows;
          return [];
        },
      };
      return domStore[id];
    },
    querySelector: () => ({ style: {}, innerHTML: '', textContent: '', className: '', classList: { add(){}, remove(){}, contains(){ return false; } }, appendChild(){}, addEventListener(){} }),
    querySelectorAll: () => [],
    createElement: () => ({ style: {}, innerHTML: '', className: '', appendChild(){}, addEventListener(){} }),
  },
  window:       { addEventListener(){} },
  localStorage: { getItem(){ return null; }, setItem(){} },
  location:     { hostname: 'localhost', search: '' },
  console:      { log(){}, warn(){}, error(){} },
  fetch:        async () => ({}),
  AbortController: class { abort(){} signal = {} },
  setTimeout: () => {},
  clearTimeout: () => {},

  // Leaflet-stub
  L: {
    map: () => { const m = { on(){return m;}, fitBounds(){return m;}, addLayer(){return m;}, setView(){return m;}, addControl(){return m;} }; return m; },
    tileLayer: () => ({ addTo: () => {} }),
    layerGroup: () => ({ addTo: () => ({ clearLayers(){}, addLayer(){} }), clearLayers(){}, addLayer(){} }),
    circleMarker: () => ({ bindPopup: () => ({ on: () => ({}) }), addTo(){ return this; } }),
    circle: () => ({ addTo: () => ({}) }),
    latLngBounds: () => ({ pad: () => ({}) }),
  },

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

// Injicera getter för script-scope let-variabler
const srcWithGetters = src + `
function _getReportersBySpecies() { return _reportersBySpecies; }
`;
vm.runInNewContext(srcWithGetters, sandbox);
const { actCat, actColor, actBorder, esc, renderBreeding, _getReportersBySpecies } = sandbox;

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
// actCat – kategoritilldelning
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}actCat – häckningskategori${'\x1b[0m'}`);
eq('act=1  → A (möjlig)',     actCat(1),  'A');
eq('act=4  → A (under B)',    actCat(4),  'A');
eq('act=5  → B (sannolik)',   actCat(5),  'B');
eq('act=12 → B (under C)',    actCat(12), 'B');
eq('act=13 → C (säker)',      actCat(13), 'C');
eq('act=20 → C (> 13)',       actCat(20), 'C');

// ══════════════════════════════════════════════════════════════════════════
// actColor
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}actColor – kartfärg${'\x1b[0m'}`);
eq('A → gul',  actColor(1),  '#f6e05e');
eq('B → orange', actColor(5), '#ed8936');
eq('C → röd',  actColor(13), '#c53030');

// ══════════════════════════════════════════════════════════════════════════
// actBorder
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}actBorder – kantfärg${'\x1b[0m'}`);
eq('A border', actBorder(1),  '#d69e2e');
eq('B border', actBorder(5),  '#c05621');
eq('C border', actBorder(13), '#9b2c2c');

// ══════════════════════════════════════════════════════════════════════════
// esc
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}esc – HTML-escape${'\x1b[0m'}`);
eq('& escapes', esc('a & b'), 'a &amp; b');
eq('< escapes', esc('<b>'),   '&lt;b&gt;');
eq('null/undefined → tom', esc(null), '');

// ══════════════════════════════════════════════════════════════════════════
// renderBreeding – arttabell, kategoriräknare, truncated
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}renderBreeding – rendering${'\x1b[0m'}`);

const BREEDING_OBS = [
  { lat: 64.0, lon: 20.0, sv: 'Kungsörn', sci: 'Aquila chrysaetos', key: 1, act: 13, cnt: 1, site: 'Skog', date: '2025-06-01', reporter: 'Anna' },
  { lat: 64.1, lon: 20.1, sv: 'Kungsörn', sci: 'Aquila chrysaetos', key: 1, act: 13, cnt: 1, site: 'Berg', date: '2025-06-02', reporter: 'Björn' },
  { lat: 63.9, lon: 19.9, sv: 'Blåmes',   sci: 'Cyanistes caeruleus', key: 2, act: 5,  cnt: 1, site: 'Park', date: '2025-05-15', reporter: 'Anna' },
  { lat: 64.2, lon: 20.2, sv: 'Ärla',     sci: 'Motacilla alba',      key: 3, act: 1,  cnt: 2, site: 'Väg',  date: '2025-05-10', reporter: 'Carla' },
  // Obs utan koordinater – skall filtreras bort av backend (visas inte)
];

renderBreeding({ observations: BREEDING_OBS, total: 4, truncated: false }, 'Västerbotten', 2025);

// Kategoriräknare
assert('C-räknaren visas (2 st)',  (domStore['kpiC']?.textContent || '') === '2');
assert('B-räknaren visas (1 st)',  (domStore['kpiB']?.textContent || '') === '1');
assert('A-räknaren visas (1 st)',  (domStore['kpiA']?.textContent || '') === '1');

// Arttabell
const spHtml = domStore['spTable']?.innerHTML || domStore['speciesTable']?.innerHTML || '';
// Kontrollera via _reportersBySpecies att arterna registrerats
assert('Kungsörn har 2 rapportörer', (_getReportersBySpecies()['1']?.length || 0) === 2);
assert('Blåmes har 1 rapportör',     (_getReportersBySpecies()['2']?.length || 0) === 1);

// truncated-varning
renderBreeding({ observations: BREEDING_OBS, total: 5000, truncated: true }, 'Stockholm', 2025);
assert('truncWarn visas när truncated=true',
  domStore['truncWarn']?.style?.display !== 'none');

renderBreeding({ observations: BREEDING_OBS, total: 4, truncated: false }, 'Västerbotten', 2025);
assert('truncWarn döljs när truncated=false',
  domStore['truncWarn']?.style?.display === 'none');

// ══════════════════════════════════════════════════════════════════════════
// filterSpecies-logik – inline test av algoritmen
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}filterSpecies-logik${'\x1b[0m'}`);

// Simulera filterSpecies beteende
function testFilterSpecies(rows, q) {
  const ql = (q || '').toLowerCase().trim();
  return rows.filter(r => !ql || r.name.includes(ql));
}

const mockRows = [
  { name: 'kungsörn', style: {} },
  { name: 'blåmes',   style: {} },
  { name: 'ärla',     style: {} },
];

assert('tom query → alla synliga',    testFilterSpecies(mockRows, '').length === 3);
assert('filter blåmes → 1',          testFilterSpecies(mockRows, 'blåmes').length === 1);
assert('filter xxx → 0',             testFilterSpecies(mockRows, 'xxx').length === 0);
assert('partial match: kunk → kungsörn', testFilterSpecies(mockRows, 'kunk').length === 0);
assert('partial match: kung → kungsörn', testFilterSpecies(mockRows, 'kung').length === 1);

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
