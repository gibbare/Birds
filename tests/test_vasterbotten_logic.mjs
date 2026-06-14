/**
 * Enhetstester – faglar-vasterbotten.html kärnlogik
 * ==================================================
 * Kör: node tests/test_vasterbotten_logic.mjs
 *
 * Täcker (40 tester):
 *   sosStr               – fältnormalisering (sträng/objekt/null)
 *   normalizeSosResults  – SOS API → internt format
 *   buildSpeciesMap      – GBIF-gruppering per taxon
 *   sortedSpecies        – sortering (antal/obs/namn)
 *   filteredSpecies      – textsökning
 *   filteredRows         – multi-fälts textsökning (proxy-rader)
 *   sortedRows           – kolumnsortering
 *   groupRows            – artgruppering
 *   rlBadge              – rödlistebadge-HTML
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';
import vm from 'vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const htmlPath  = path.join(__dirname, '../faglar-vasterbotten.html');

// Extrahera alla <script>-block (exkl. externa src=)
const html = readFileSync(htmlPath, 'utf-8');
const scriptBlocks = [];
const scriptRe = /<script(?![^>]*\bsrc\b)[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = scriptRe.exec(html)) !== null) scriptBlocks.push(m[1]);
const src = scriptBlocks.join('\n');

// ── Sandbox med DOM-stubs och nödvändiga globaler ────────────────────────
const domStore = {};
const sandbox = {
  // Globaler som funktionerna refererar till
  sortBy: 'count',        // används av sortedSpecies
  filterText: '',         // används av filteredSpecies / filteredRows
  tableSortCol: 'art',    // används av sortedRows
  tableSortDir: 'desc',   // används av sortedRows
  proxyLoggedIn: false,
  allResults: [],
  dataSource: 'gbif',
  currentDate: new Date('2025-05-01'),
  selectedLanId: '24',
  selectedKommunId: null,
  PAGE_SIZE: 50,
  MONTHS: ['Jan','Feb','Mar','Apr','Maj','Jun','Jul','Aug','Sep','Okt','Nov','Dec'],

  // Leaflet-stub
  L: {
    map: () => ({ setView: () => ({}) }),
    tileLayer: () => ({ addTo: () => {} }),
    layerGroup: () => ({ addTo: () => ({}) }),
    circleMarker: () => ({ bindPopup: () => ({}), on: () => ({}) }),
  },

  // DOM-stub
  document: {
    addEventListener() {},
    getElementById: (id) => {
      if (!domStore[id]) domStore[id] = {
        textContent: '', innerHTML: '', style: { display: '' },
        value: '', classList: { add(){}, remove(){}, contains(){ return false; } },
        prepend() {}, querySelectorAll(){ return []; },
        appendChild() {}, removeChild() {}, insertBefore() {},
        previousElementSibling: null, parentNode: null,
        children: [], options: [], selectedIndex: 0,
        addEventListener() {},
      };
      return domStore[id];
    },
    querySelector: () => ({ style: {}, innerHTML: '', textContent: '', className: '', classList: { add(){}, remove(){}, contains(){ return false; } }, appendChild(){}, addEventListener(){} }),
    querySelectorAll: () => [],
    createElement: () => ({ style: {}, innerHTML: '', className: '', dataset: {}, appendChild() {}, addEventListener() {} }),
  },
  window:    { addEventListener() {}, matchMedia(){ return { matches: false, addEventListener(){} }; } },
  navigator: {},
  localStorage: { getItem(){ return null; }, setItem(){} },
  location:  { hostname: 'localhost', search: '' },
  AbortController: class { abort(){} signal = {} },
  setTimeout: () => {},
  clearTimeout: () => {},
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  console: { log(){}, warn(){}, error(){} },
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
  isNaN,
  Intl,
};

// Injicera hjälpsättare för script-scope let-variabler (nås ej via sandbox)
const srcWithSetters = src + `
function _setSortBy(v)       { sortBy = v; }
function _setFilterText(v)   { filterText = v; }
function _setTableSortCol(v) { tableSortCol = v; }
function _setTableSortDir(v) { tableSortDir = v; }
`;
vm.runInNewContext(srcWithSetters, sandbox);

// Plocka ut testade funktioner
const {
  sosStr,
  normalizeSosResults,
  buildSpeciesMap,
  sortedSpecies,
  filteredSpecies,
  filteredRows,
  sortedRows,
  groupRows,
  rlBadge,
  _setSortBy,
  _setFilterText,
  _setTableSortCol,
  _setTableSortDir,
} = sandbox;

// ── Testinfrastruktur ───────────────────────────────────────────────────────
const OK  = '\x1b[92m[OK]\x1b[0m';
const ERR = '\x1b[91m[FEL]\x1b[0m';
const HEAD = '\x1b[1;34m';
const RST = '\x1b[0m';
let failures = 0;

function assert(desc, cond, detail = '') {
  if (cond) { console.log(`  ${OK} ${desc}`); }
  else       { console.log(`  ${ERR} ${desc}${detail ? '\n       → ' + detail : ''}`); failures++; }
}
function eq(desc, a, b) {
  assert(desc, JSON.stringify(a) === JSON.stringify(b),
    `fick: ${JSON.stringify(a)}  väntat: ${JSON.stringify(b)}`);
}

// ══════════════════════════════════════════════════════════════════════════
// sosStr
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}sosStr – fältnormalisering${RST}`);
eq('null → tom sträng',       sosStr(null),              '');
eq('undefined → tom sträng',  sosStr(undefined),         '');
eq('sträng returneras direkt', sosStr('Kungsörn'),       'Kungsörn');
eq('objekt med .name',        sosStr({ name: 'Trast' }), 'Trast');
eq('objekt med .value',       sosStr({ value: '42' }),   '42');
eq('objekt utan känt fält',   sosStr({ other: 'x' }),    '');
eq('nummer → sträng',         sosStr(42),                '42');

// ══════════════════════════════════════════════════════════════════════════
// normalizeSosResults
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}normalizeSosResults – SOS API → internt format${RST}`);

assert('tom records → tom array',
  normalizeSosResults({ records: [] }).length === 0);
assert('stödjer .observations nyckel',
  normalizeSosResults({ observations: [{ taxon: {}, location: {}, occurrence: {}, event: {}, identification: {} }] }).length === 1);
assert('stödjer .results nyckel',
  normalizeSosResults({ results: [{ taxon: {}, location: {}, occurrence: {}, event: {}, identification: {} }] }).length === 1);

// Bygger ett SOS-obs och verifierar normalisering
const SOS_OBS = {
  taxon: {
    id: 999,
    scientificName: 'Aquila chrysaetos',
    vernacularNames: [
      { language: 'sv', name: 'Kungsörn' },
      { language: 'en', name: 'Golden Eagle' },
    ],
  },
  location: {
    decimalLatitude:  64.5,
    decimalLongitude: 20.3,
    locality: 'Abborrtjärn',
    site:     'Vindeln',
    coordinateUncertaintyInMeters: 50,
  },
  occurrence: {
    individualCount: '3',
    recordedBy: 'Erik Eriksson',
    occurrenceId: 'urn:lsid:artportalen.se:sighting:131551987',
  },
  event:          { startDate: '2025-05-03T08:30:00' },
  identification: { verified: true, uncertainIdentification: false },
};

const norm = normalizeSosResults({ records: [SOS_OBS] })[0];
eq('vernacularName från sv-array', norm.vernacularName, 'Kungsörn');
eq('scientificName',               norm.scientificName, 'Aquila chrysaetos');
eq('speciesKey',                   norm.speciesKey,     999);
eq('individualCount parsas',       norm.individualCount, 3);
eq('reporter från recordedBy',     norm.reporter,       'Erik Eriksson');
eq('lat/lng-koordinater',          norm.lat,            64.5);
eq('coordUncertainty',             norm.coordUncertainty, 50);
eq('artportalenId från occurrenceId', norm.artportalenId, '131551987');
eq('startDT med tid',              norm.startDT,        '2025-05-03 08:30');
assert('verified = true',          norm.verified === true);
assert('uncertainId = false',      norm.uncertainId === false);
assert('_source = proxy',          norm._source === 'proxy');

// Fallback för vernacularName
const OBS_NO_SV = {
  taxon: { vernacularNames: [{ language: 'en', name: 'Osprey' }], scientificName: 'Pandion haliaetus' },
  location: {}, occurrence: {}, event: {}, identification: {},
};
const normFb = normalizeSosResults({ records: [OBS_NO_SV] })[0];
eq('vernacularName fallback till en', normFb.vernacularName, 'Osprey');

// IndividualCount fallback till 1
const OBS_NO_COUNT = {
  taxon: { scientificName: 'X' }, location: {}, occurrence: {}, event: {}, identification: {},
};
eq('individualCount defaultar till 1',
  normalizeSosResults({ records: [OBS_NO_COUNT] })[0].individualCount, 1);

// Datum utan klockslag
const OBS_DATE_ONLY = {
  taxon: {}, location: {}, occurrence: {},
  event: { startDate: '2025-06-01T00:00:00' }, identification: {},
};
eq('startDT utan klockslag → bara datum',
  normalizeSosResults({ records: [OBS_DATE_ONLY] })[0].startDT, '2025-06-01');

// ══════════════════════════════════════════════════════════════════════════
// buildSpeciesMap
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}buildSpeciesMap – GBIF-gruppering per taxon${RST}`);

const GBIF_OBS = [
  { speciesKey: 1, vernacularName: 'Kungsörn', scientificName: 'Aquila chrysaetos', individualCount: 2, locality: 'Sjön', eventTime: '08:30:00' },
  { speciesKey: 1, vernacularName: 'Kungsörn', scientificName: 'Aquila chrysaetos', individualCount: 1, verbatimLocality: 'Åsen' },
  { speciesKey: 2, vernacularName: 'Osprey',   scientificName: 'Pandion haliaetus', individualCount: 3 },
  { /* inget speciesKey – ska hoppas */        scientificName: 'X' },
];
const spMap = buildSpeciesMap(GBIF_OBS);

assert('obs utan speciesKey hoppas över', Object.keys(spMap).length === 2);
eq('observations räknas per art', spMap[1].observations, 2);
eq('individualCount summeras',    spMap[1].individualCount, 3);
assert('localities aggregeras (Set)', spMap[1].localities.size === 2);
assert('verbatimLocality läggs till', spMap[1].localities.has('Åsen'));
assert('eventTime trimmas till HH:MM', spMap[1].times.includes('08:30'));
eq('andra artens observations',   spMap[2].observations, 1);

// ══════════════════════════════════════════════════════════════════════════
// sortedSpecies
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}sortedSpecies – sortering${RST}`);

const SP_MAP = {
  1: { vernacular: 'Blåmes',   scientific: 'Cyanistes caeruleus', individualCount: 10, observations: 5 },
  2: { vernacular: 'Kungsörn', scientific: 'Aquila chrysaetos',   individualCount: 2,  observations: 8 },
  3: { vernacular: 'Ärla',     scientific: 'Motacilla',           individualCount: 6,  observations: 3 },
};

_setSortBy('count');
const byCount = sortedSpecies(SP_MAP);
assert('sortBy count: störst först', byCount[0].individualCount === 10 && byCount[1].individualCount === 6);

_setSortBy('obs');
const byObs = sortedSpecies(SP_MAP);
assert('sortBy obs: flest obs först', byObs[0].observations === 8);

_setSortBy('name');
const byName = sortedSpecies(SP_MAP);
assert('sortBy name: alfabetisk (Blåmes < Kungsörn)', byName[0].vernacular === 'Blåmes');

// ══════════════════════════════════════════════════════════════════════════
// filteredSpecies
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}filteredSpecies – textsökning${RST}`);

const SP_ARR = [
  { vernacular: 'Kungsörn', scientific: 'Aquila chrysaetos' },
  { vernacular: 'Blåmes',   scientific: 'Cyanistes caeruleus' },
];

_setFilterText('');
assert('tom filter → alla',          filteredSpecies(SP_ARR).length === 2);
_setFilterText('kungsörn');
assert('filter på vernacular',       filteredSpecies(SP_ARR).length === 1);
_setFilterText('aquila');
assert('filter på scientific',       filteredSpecies(SP_ARR).length === 1);
_setFilterText('xxxxxx');
assert('inget match → tom array',    filteredSpecies(SP_ARR).length === 0);
_setFilterText('');

// ══════════════════════════════════════════════════════════════════════════
// filteredRows
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}filteredRows – multi-fälts textsökning${RST}`);

const ROWS = [
  { vernacularName: 'Kungsörn', scientificName: 'Aquila chrysaetos', locality: 'Sjön', site: 'Vindeln', reporter: 'Anna' },
  { vernacularName: 'Blåmes',   scientificName: 'Cyanistes caeruleus', locality: 'Skogen', site: 'Umeå', reporter: 'Björn' },
];

_setFilterText('');
assert('tom filter → alla rader', filteredRows(ROWS).length === 2);
_setFilterText('blåmes');
assert('filter på vernacularName', filteredRows(ROWS).length === 1);
_setFilterText('vindeln');
assert('filter på locality', filteredRows(ROWS).length === 1);
_setFilterText('umeå');
assert('filter på site', filteredRows(ROWS).length === 1);
_setFilterText('björn');
assert('filter på reporter', filteredRows(ROWS).length === 1);
_setFilterText('xxxxxx');
assert('inget match → tom', filteredRows(ROWS).length === 0);
_setFilterText('');

// ══════════════════════════════════════════════════════════════════════════
// sortedRows
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}sortedRows – kolumnsortering${RST}`);

const SORT_ROWS = [
  { vernacularName: 'Kungsörn', scientificName: 'A', individualCount: 5,  locality: 'Sjön',   site: 'V', reporter: 'Björn', startDT: '2025-05-02' },
  { vernacularName: 'Blåmes',   scientificName: 'B', individualCount: 12, locality: 'Skogen', site: 'U', reporter: 'Anna',  startDT: '2025-05-01' },
];

_setTableSortCol('antal'); _setTableSortDir('desc');
const byAntal = sortedRows(SORT_ROWS);
assert('sort antal desc: störst först', byAntal[0].individualCount === 12);

_setTableSortCol('antal'); _setTableSortDir('asc');
const byAntalAsc = sortedRows(SORT_ROWS);
assert('sort antal asc: minst först', byAntalAsc[0].individualCount === 5);

_setTableSortCol('rap'); _setTableSortDir('asc');
const byRap = sortedRows(SORT_ROWS);
assert('sort reporter asc: Anna < Björn', byRap[0].reporter === 'Anna');

_setTableSortCol('art'); _setTableSortDir('asc');
const byArt = sortedRows(SORT_ROWS);
assert('sort art default: Blåmes < Kungsörn', byArt[0].vernacularName === 'Blåmes');

// ══════════════════════════════════════════════════════════════════════════
// groupRows
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}groupRows – artgruppering${RST}`);

const GRP_ROWS = [
  { vernacularName: 'Kungsörn', scientificName: 'A', individualCount: 2 },
  { vernacularName: 'Kungsörn', scientificName: 'A', individualCount: 3 },
  { vernacularName: 'Blåmes',   scientificName: 'B', individualCount: 1 },
];
const groups = groupRows(GRP_ROWS);

eq('antal grupper',               groups.length, 2);
eq('Kungsörn-gruppen har 2 rader', groups[0].rows.length, 2);
eq('totalCount summeras rätt',    groups[0].totalCount, 5);
eq('Blåmes-gruppen totalCount',   groups[1].totalCount, 1);

// ══════════════════════════════════════════════════════════════════════════
// rlBadge
// ══════════════════════════════════════════════════════════════════════════
console.log(`\n${HEAD}rlBadge – rödlistebadge${RST}`);

assert('LC returnerar tom sträng',  rlBadge('LC') === '');
assert('null returnerar tom sträng', rlBadge(null) === '');
assert('CR ger badge-HTML',         rlBadge('CR').includes('rl-cr'));
assert('CR innehåller texten CR',   rlBadge('CR').includes('>CR<'));
assert('VU ger rl-vu klass',        rlBadge('VU').includes('rl-vu'));
assert('NT ger rl-nt klass',        rlBadge('NT').includes('rl-nt'));
assert('okänd kod → tom sträng',    rlBadge('XYZ') === '');

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
