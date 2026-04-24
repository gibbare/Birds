"""
Enhetstester – proxy.py kärnlogik
===================================
Kör: python -m pytest tests/test_proxy_logic.py -v

Täcker:
  • _gbif_rank          – rank-uppslag + GBIF-mock
  • _apply_rank_corrections – flytta taxa mellan sp/sub/hyb-buckets
  • _se_rep_empty       – korrekt datastruktur för ny reporter
  • _SV_NAME_MERGES     – gråkråka/svartkråka slås ihop till kråka
  • _merge_se_records   – hela merge-logiken inkl. hybriddetektering,
                          namnsammanslagning, dagräkning, månadsspårning
  • _build_compact_se   – sub/hyb-räknare och listor i kompaktformat
"""

import sys
import os
import importlib.util
from unittest.mock import patch, MagicMock

import pytest

# ── Ladda proxy.py ────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROXY = os.path.join(_ROOT, 'proxy.py')

spec = importlib.util.spec_from_file_location('proxy', _PROXY)
_proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_proxy)

# Genvägar till testade symboler
_gbif_rank             = _proxy._gbif_rank
_apply_rank_corrections = _proxy._apply_rank_corrections
_se_rep_empty          = _proxy._se_rep_empty
_SV_NAME_MERGES        = _proxy._SV_NAME_MERGES
_merge_se_records      = _proxy._merge_se_records
_build_compact_se      = _proxy._build_compact_se


# ─────────────────────────────────────────────────────────────────────────────
# Hjälpfunktioner
# ─────────────────────────────────────────────────────────────────────────────

def _rec(sv_name='kungsfiskare', sci_name='Alcedo atthis', taxon_id='100004',
         reporter='Anders Andersson', ind=1, date='2026-04-01',
         locality='Umeå fjärd'):
    """Bygg ett minimalt SOS API-liknande record."""
    return {
        'taxon': {
            'id': int(taxon_id) if taxon_id.isdigit() else None,
            'vernacularName': sv_name,
            'scientificName': sci_name,
        },
        'occurrence': {
            'reportedBy': reporter,
            'individualCount': ind,
        },
        'event': {
            'startDate': f'{date}T08:00:00',
        },
        'location': {
            'locality': locality,
        },
    }

def _merge(records, rank_cache=None):
    """Kör _merge_se_records och returnera reporters-dict."""
    reporters = {}
    _merge_se_records(reporters, records, '2026-04-01', rank_cache=rank_cache or {})
    return reporters


# ═════════════════════════════════════════════════════════════════════════════
# _gbif_rank
# ═════════════════════════════════════════════════════════════════════════════

class TestGbifRank:

    def test_tomt_namn_ger_sp(self):
        assert _gbif_rank('') == 'sp'

    def test_none_ger_sp(self):
        assert _gbif_rank(None) == 'sp'

    def test_unicode_kryss_ger_hyb(self):
        """× i vetenskapligt namn → hybrid utan GBIF-anrop."""
        assert _gbif_rank('Anas × boschas') == 'hyb'

    def test_bokstav_x_med_mellanslag_ger_hyb(self):
        """' x ' i vetenskapligt namn → hybrid utan GBIF-anrop."""
        assert _gbif_rank('Larus fuscus x argentatus') == 'hyb'

    def test_gbif_subspecies_ger_sub(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'matchType': 'EXACT',
            'rank': 'SUBSPECIES',
        }
        with patch('requests.get', return_value=mock_resp):
            assert _gbif_rank('Parus major major') == 'sub'

    def test_gbif_species_ger_sp(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'matchType': 'EXACT',
            'rank': 'SPECIES',
        }
        with patch('requests.get', return_value=mock_resp):
            assert _gbif_rank('Parus major') == 'sp'

    def test_gbif_form_ger_sp(self):
        """FORM (t.ex. tamduva) ska INTE räknas som underart."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'matchType': 'EXACT',
            'rank': 'FORM',
        }
        with patch('requests.get', return_value=mock_resp):
            assert _gbif_rank('Columba livia domestica') == 'sp'

    def test_gbif_variety_ger_sp(self):
        """VARIETY ska behandlas som art, inte underart."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'matchType': 'EXACT',
            'rank': 'VARIETY',
        }
        with patch('requests.get', return_value=mock_resp):
            assert _gbif_rank('Columba livia var. domestica') == 'sp'

    def test_gbif_hybrid_rank_ger_hyb(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'matchType': 'EXACT',
            'rank': 'HYBRID',
        }
        with patch('requests.get', return_value=mock_resp):
            assert _gbif_rank('Anas platyrhynchos x Anas acuta') == 'hyb'

    def test_gbif_matchtype_none_ger_sp(self):
        """Okänt taxon (NONE) → behandla som vanlig art."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {'matchType': 'NONE'}
        with patch('requests.get', return_value=mock_resp):
            assert _gbif_rank('Okänd fågel exotica') == 'sp'

    def test_gbif_http_fel_ger_sp(self):
        """Nätverksfel → fallback till 'sp', ingen krasch."""
        with patch('requests.get', side_effect=Exception('timeout')):
            assert _gbif_rank('Parus major') == 'sp'

    def test_gbif_ej_ok_status_ger_sp(self):
        """Icke-200 svar → fallback till 'sp'."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        with patch('requests.get', return_value=mock_resp):
            assert _gbif_rank('Parus major') == 'sp'


# ═════════════════════════════════════════════════════════════════════════════
# _se_rep_empty
# ═════════════════════════════════════════════════════════════════════════════

class TestSeRepEmpty:

    def test_returnerar_dict(self):
        r = _se_rep_empty()
        assert isinstance(r, dict)

    def test_obs_startar_pa_noll(self):
        assert _se_rep_empty()['obs'] == 0

    def test_art_startar_pa_noll(self):
        assert _se_rep_empty()['art'] == 0

    def test_monthly_ar_lista_med_tolv_nollor(self):
        m = _se_rep_empty()['monthly']
        assert isinstance(m, list)
        assert len(m) == 12
        assert all(v == 0 for v in m)

    def test_sp_ids_ar_set(self):
        assert isinstance(_se_rep_empty()['sp_ids'], set)

    def test_sub_ids_ar_set(self):
        assert isinstance(_se_rep_empty()['sub_ids'], set)

    def test_hyb_ids_ar_set(self):
        assert isinstance(_se_rep_empty()['hyb_ids'], set)

    def test_sp_obs_ar_dict(self):
        assert isinstance(_se_rep_empty()['sp_obs'], dict)

    def test_sub_obs_ar_dict(self):
        assert isinstance(_se_rep_empty()['sub_obs'], dict)

    def test_hyb_obs_ar_dict(self):
        assert isinstance(_se_rep_empty()['hyb_obs'], dict)

    def test_alla_obligatoriska_nycklar_finns(self):
        r = _se_rep_empty()
        for key in ['obs', 'monthly', 'art', 'sp_ids', 'sp_obs',
                    'sub_ids', 'sub_obs', 'hyb_ids', 'hyb_obs',
                    'pl_obs', 'dagar', 'lastObs']:
            assert key in r, f"Nyckel saknas: {key}"


# ═════════════════════════════════════════════════════════════════════════════
# _SV_NAME_MERGES
# ═════════════════════════════════════════════════════════════════════════════

class TestSvNameMerges:

    def test_grakraka_ger_kraka(self):
        assert _SV_NAME_MERGES['gråkråka'] == 'kråka'

    def test_svartkraka_ger_kraka(self):
        assert _SV_NAME_MERGES['svartkråka'] == 'kråka'

    def test_kraka_ger_kraka(self):
        """Direktobservationer av kråka ska hamna i samma bucket."""
        assert _SV_NAME_MERGES['kråka'] == 'kråka'

    def test_alla_nycklar_ar_lowercase(self):
        for k in _SV_NAME_MERGES:
            assert k == k.lower(), f"Nyckel borde vara lowercase: {k!r}"


# ═════════════════════════════════════════════════════════════════════════════
# _apply_rank_corrections
# ═════════════════════════════════════════════════════════════════════════════

class TestApplyRankCorrections:

    def _rep(self, sp_ids=None, sp_obs=None):
        r = _se_rep_empty()
        r['sp_ids'] = set(sp_ids or [])
        r['sp_obs'] = sp_obs or {}
        r['art'] = len(r['sp_ids'])
        return r

    def test_subspecies_flyttas_till_sub(self):
        rep = self._rep(sp_ids=['123'])
        reporters = {'Kalle': rep}
        _apply_rank_corrections(reporters, {'123': 'sub'})
        assert '123' not in rep['sp_ids']
        assert '123' in rep['sub_ids']

    def test_hybrid_flyttas_till_hyb(self):
        rep = self._rep(sp_ids=['456'])
        reporters = {'Kalle': rep}
        _apply_rank_corrections(reporters, {'456': 'hyb'})
        assert '456' not in rep['sp_ids']
        assert '456' in rep['hyb_ids']

    def test_art_stannar_i_sp(self):
        rep = self._rep(sp_ids=['789'])
        reporters = {'Kalle': rep}
        _apply_rank_corrections(reporters, {'789': 'sp'})
        assert '789' in rep['sp_ids']
        assert '789' not in rep['sub_ids']

    def test_okant_taxon_stannar_i_sp(self):
        """Taxa utan cache-post ska inte flyttas."""
        rep = self._rep(sp_ids=['999'])
        reporters = {'Kalle': rep}
        _apply_rank_corrections(reporters, {})
        assert '999' in rep['sp_ids']

    def test_art_raknas_om_korrekt(self):
        rep = self._rep(sp_ids=['1', '2', '3'])
        reporters = {'Kalle': rep}
        # Flytta 2 av 3 till underarter → art = 1
        _apply_rank_corrections(reporters, {'1': 'sub', '2': 'sub'})
        assert rep['art'] == 1

    def test_obs_data_foljer_med_vid_flytt(self):
        """sp_obs ska följa med när taxa moves till sub_obs."""
        rep = self._rep(
            sp_ids=['100'],
            sp_obs={'100': {'sv': 'talgoxe', 'obs': 5, 'ind': 7}}
        )
        reporters = {'Kalle': rep}
        _apply_rank_corrections(reporters, {'100': 'sub'})
        assert '100' in rep['sub_obs']
        assert rep['sub_obs']['100']['sv'] == 'talgoxe'
        assert rep['sub_obs']['100']['obs'] == 5

    def test_obs_data_raderas_fran_sp_obs(self):
        rep = self._rep(
            sp_ids=['100'],
            sp_obs={'100': {'sv': 'talgoxe', 'obs': 5, 'ind': 7}}
        )
        reporters = {'Kalle': rep}
        _apply_rank_corrections(reporters, {'100': 'sub'})
        assert '100' not in rep['sp_obs']

    def test_idempotent(self):
        """Kan köras två gånger utan att ändra resultatet."""
        rep = self._rep(sp_ids=['11'])
        reporters = {'Kalle': rep}
        cache = {'11': 'sub'}
        _apply_rank_corrections(reporters, cache)
        sub_after_1 = set(rep['sub_ids'])
        _apply_rank_corrections(reporters, cache)
        assert rep['sub_ids'] == sub_after_1


# ═════════════════════════════════════════════════════════════════════════════
# _merge_se_records
# ═════════════════════════════════════════════════════════════════════════════

class TestMergeSeRecords:

    # ── Grundläggande ──────────────────────────────────────────────────────

    def test_vanlig_art_hamnar_i_sp_bucket(self):
        rep = _merge([_rec()])[' Anders Andersson'.strip()]
        assert 'Anders Andersson' in _merge([_rec()])
        r = _merge([_rec()])['Anders Andersson']
        assert r['art'] == 1
        assert len(r['sp_ids']) == 1

    def test_obs_raknas_upp(self):
        recs = [_rec(), _rec()]  # 2 obs av samma art
        r = _merge(recs)['Anders Andersson']
        assert r['obs'] == 2

    def test_art_raknare_okar_inte_vid_aterseende(self):
        """Samma taxon-ID observerat två gånger → art = 1."""
        recs = [_rec(taxon_id='100004'), _rec(taxon_id='100004')]
        r = _merge(recs)['Anders Andersson']
        assert r['art'] == 1

    def test_olika_arter_ger_korrekt_art_antal(self):
        recs = [
            _rec(sv_name='kungsfiskare', taxon_id='1'),
            _rec(sv_name='talgoxe',      taxon_id='2'),
            _rec(sv_name='blåmes',       taxon_id='3'),
        ]
        r = _merge(recs)['Anders Andersson']
        assert r['art'] == 3

    def test_reporter_utan_namn_hoppas_over(self):
        """Record utan reporter ska inte skapa en post."""
        r = _merge([_rec(reporter='')])
        assert r == {}

    # ── Månadsfördelning ────────────────────────────────────────────────────

    def test_manad_spars_korrekt_april(self):
        r = _merge([_rec(date='2026-04-15')])['Anders Andersson']
        assert r['monthly'][3] == 1  # april = index 3

    def test_manad_spars_korrekt_januari(self):
        r = _merge([_rec(date='2026-01-05')])['Anders Andersson']
        assert r['monthly'][0] == 1  # januari = index 0

    def test_manad_spars_korrekt_december(self):
        r = _merge([_rec(date='2026-12-31')])['Anders Andersson']
        assert r['monthly'][11] == 1  # december = index 11

    # ── Dagräkning ─────────────────────────────────────────────────────────

    def test_samma_datum_okar_inte_dagar(self):
        recs = [_rec(date='2026-04-01'), _rec(date='2026-04-01')]
        r = _merge(recs)['Anders Andersson']
        assert r['dagar'] == 1

    def test_olika_datum_okar_dagar(self):
        recs = [_rec(date='2026-04-01'), _rec(date='2026-04-02')]
        r = _merge(recs)['Anders Andersson']
        assert r['dagar'] == 2

    # ── Hybriddetektering ──────────────────────────────────────────────────

    def test_hybrid_med_unicode_kryss_i_sv_namn(self):
        """'×' i sv_name → hyb-bucket, räknas EJ som art."""
        r = _merge([_rec(sv_name='grågås × kanadagås', taxon_id='9001')]
                   )['Anders Andersson']
        assert r['art'] == 0
        assert len(r['hyb_ids']) == 1
        assert len(r['sp_ids']) == 0

    def test_hybrid_med_bokstav_x_mellanslag_i_sv_namn(self):
        """' x ' i sv_name → hyb-bucket."""
        r = _merge([_rec(sv_name='grågås x kanadagås', taxon_id='9002')]
                   )['Anders Andersson']
        assert r['art'] == 0
        assert len(r['hyb_ids']) == 1

    def test_hybrid_med_kryss_i_sci_namn(self):
        """'×' i sci_name → hyb-bucket."""
        r = _merge([_rec(sv_name='korsand', sci_name='Anas × boschas',
                         taxon_id='9003')])['Anders Andersson']
        assert len(r['hyb_ids']) == 1
        assert len(r['sp_ids']) == 0

    def test_hybrid_via_rank_cache(self):
        """Taxon markerat 'hyb' i rank_cache → hyb-bucket."""
        r = _merge(
            [_rec(taxon_id='9004')],
            rank_cache={'9004': 'hyb'}
        )['Anders Andersson']
        assert len(r['hyb_ids']) == 1
        assert r['art'] == 0

    def test_hybrid_sparas_i_hyb_obs(self):
        r = _merge([_rec(sv_name='grågås x kanadagås', taxon_id='9002')]
                   )['Anders Andersson']
        assert len(r['hyb_obs']) == 1
        obs = list(r['hyb_obs'].values())[0]
        assert obs['sv'] == 'grågås x kanadagås'
        assert obs['obs'] == 1

    # ── Underarter ─────────────────────────────────────────────────────────

    def test_underart_via_rank_cache_ger_sub(self):
        r = _merge(
            [_rec(sv_name='nordlig talgoxe', taxon_id='5000')],
            rank_cache={'5000': 'sub'}
        )['Anders Andersson']
        assert r['art'] == 0
        assert len(r['sub_ids']) == 1

    def test_underart_sparas_i_sub_obs(self):
        r = _merge(
            [_rec(sv_name='nordlig talgoxe', taxon_id='5000')],
            rank_cache={'5000': 'sub'}
        )['Anders Andersson']
        obs = list(r['sub_obs'].values())[0]
        assert obs['sv'] == 'nordlig talgoxe'

    # ── Namnsammanslagning ─────────────────────────────────────────────────

    def test_grakraka_slogs_ihop_med_svartkraka(self):
        """Gråkråka och svartkråka ska hamna i SAMMA bucket med art='kråka'."""
        recs = [
            _rec(sv_name='gråkråka',   taxon_id='200'),
            _rec(sv_name='svartkråka', taxon_id='201'),
        ]
        r = _merge(recs)['Anders Andersson']
        # Båda ska använda svname:kråka-nyckeln → 1 unik art
        assert r['art'] == 1
        assert len(r['sp_ids']) == 1

    def test_kraka_direkt_hamnar_i_samma_bucket_som_grakraka(self):
        recs = [
            _rec(sv_name='kråka',      taxon_id='202'),
            _rec(sv_name='gråkråka',   taxon_id='200'),
        ]
        r = _merge(recs)['Anders Andersson']
        assert r['art'] == 1

    def test_sammanslagen_art_heter_kraka(self):
        r = _merge([_rec(sv_name='gråkråka', taxon_id='200')]
                   )['Anders Andersson']
        obs = list(r['sp_obs'].values())[0]
        assert obs['sv'] == 'kråka'

    def test_namnsammanslagning_raknear_inte_som_hybrid(self):
        r = _merge([_rec(sv_name='gråkråka', taxon_id='200')]
                   )['Anders Andersson']
        assert len(r['hyb_ids']) == 0

    # ── Individantal ────────────────────────────────────────────────────────

    def test_individantal_summeras(self):
        recs = [
            _rec(sv_name='gråhäger', taxon_id='300', ind=3),
            _rec(sv_name='gråhäger', taxon_id='300', ind=5),
        ]
        r = _merge(recs)['Anders Andersson']
        obs = list(r['sp_obs'].values())[0]
        assert obs['ind'] == 8

    # ── Flera reporters ─────────────────────────────────────────────────────

    def test_flera_reporters_hanteras_separat(self):
        recs = [
            _rec(reporter='Kalle', sv_name='talgoxe', taxon_id='1'),
            _rec(reporter='Lisa',  sv_name='blåmes',   taxon_id='2'),
        ]
        reporters = _merge(recs)
        assert reporters['Kalle']['art'] == 1
        assert reporters['Lisa']['art'] == 1
        assert 'Lisa' not in reporters['Kalle']['sp_ids']


# ═════════════════════════════════════════════════════════════════════════════
# _build_compact_se
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildCompactSe:

    def _make_data(self, reporters):
        return {'year': '2026', 'last_date': '2026-04-01', 'reporters': reporters}

    def _rep_with(self, sp=None, sub=None, hyb=None, obs=10):
        r = _se_rep_empty()
        r['obs'] = obs
        r['dagar'] = 1
        for tid, sv, n in (sp or []):
            r['sp_ids'].add(tid)
            r['sp_obs'][tid] = {'sv': sv, 'obs': n, 'ind': n}
            r['art'] += 1
        for tid, sv, n in (sub or []):
            r['sub_ids'].add(tid)
            r['sub_obs'][tid] = {'sv': sv, 'obs': n, 'ind': n}
        for tid, sv, n in (hyb or []):
            r['hyb_ids'].add(tid)
            r['hyb_obs'][tid] = {'sv': sv, 'obs': n, 'ind': n}
        return r

    def test_sub_raknare_korrekt(self):
        rep = self._rep_with(sub=[('1', 'talgoxe ssp.', 3)])
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        assert compact['reporters']['Kalle']['sub'] == 1

    def test_hyb_raknare_korrekt(self):
        rep = self._rep_with(hyb=[('99', 'grågås x kanadagås', 2)])
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        assert compact['reporters']['Kalle']['hyb'] == 1

    def test_subsp_lista_finns(self):
        rep = self._rep_with(sub=[('1', 'nordlig talgoxe', 4)])
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        subsp = compact['reporters']['Kalle']['subsp']
        assert isinstance(subsp, list)
        assert len(subsp) == 1
        assert subsp[0]['sv'] == 'nordlig talgoxe'

    def test_hybsp_lista_finns(self):
        rep = self._rep_with(hyb=[('99', 'grågås x kanadagås', 2)])
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        hybsp = compact['reporters']['Kalle']['hybsp']
        assert len(hybsp) == 1
        assert hybsp[0]['sv'] == 'grågås x kanadagås'

    def test_sp_lista_sorteras_pa_ind(self):
        """Artlistan ska sorteras efter antal individer, störst först."""
        rep = self._rep_with(sp=[
            ('1', 'talgoxe', 10),
            ('2', 'blåmes',  50),
            ('3', 'pilfink',  5),
        ])
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        sp_list = compact['reporters']['Kalle']['sp']
        assert sp_list[0]['sv'] == 'blåmes'
        assert sp_list[-1]['sv'] == 'pilfink'

    def test_noll_sub_ger_noll_raknare(self):
        rep = self._rep_with(sp=[('1', 'talgoxe', 5)])
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        assert compact['reporters']['Kalle']['sub'] == 0
        assert compact['reporters']['Kalle']['hyb'] == 0

    def test_art_raknare_inkluderar_inte_sub_eller_hyb(self):
        rep = self._rep_with(
            sp=[('1', 'talgoxe', 5)],
            sub=[('2', 'nordlig talgoxe', 3)],
            hyb=[('3', 'grågås x kanadagås', 1)],
        )
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        # art = 1 (bara sp), sub = 1, hyb = 1
        assert compact['reporters']['Kalle']['art'] == 1
        assert compact['reporters']['Kalle']['sub'] == 1
        assert compact['reporters']['Kalle']['hyb'] == 1

    def test_built_at_finns_i_output(self):
        rep = self._rep_with()
        data = self._make_data({'Kalle': rep})
        compact = _build_compact_se('2026', data)
        assert 'built_at' in compact
        assert compact['built_at']  # inte tom sträng


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import pytest as _pt
    _pt.main([__file__, '-v'])
