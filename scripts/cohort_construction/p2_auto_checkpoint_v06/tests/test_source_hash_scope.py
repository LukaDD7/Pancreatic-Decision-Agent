"""Synthetic raw/display hash provenance controls (CE-AUD-008)."""
import hashlib

from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import resolve_model_quotes, supported
from scripts.cohort_construction.p2_auto_checkpoint.semantic import mask_text
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_engine import source


def test_invalid_display_lookup_preserves_raw_source_hash_and_rejection():
    s=source('C','患者张示例。决定检查。')
    display=mask_text(s['text'],['张示例'])
    raw_hash=s['source_sha256'];display_hash=hashlib.sha256(display.encode()).hexdigest()
    assert raw_hash!=display_hash
    invalid=resolve_model_quotes([dict(source_id='C',quote='不存在的患者事实')],[s],['张示例'])
    q=invalid['invalid'][0]
    assert q['status']=='QUOTE_NOT_IN_SOURCE'
    assert q['source_sha256']==raw_hash
    assert q['lookup_display_source_sha256']==display_hash
    assert not q['raw_quote_verified'] and not supported(invalid)


def test_outside_allowed_display_quote_is_not_relabelled_as_raw_verified():
    s=source('C','患者张示例。决定检查。')
    display=mask_text(s['text'],['张示例'])
    invalid=resolve_model_quotes([dict(source_id='C',quote=display)],[s],['张示例'],{'C':[]})
    q=invalid['invalid'][0]
    assert q['status']=='QUOTE_OUTSIDE_ALLOWED_SPANS'
    assert q['source_sha256']==s['source_sha256']
    assert q['submitted_masked_quote']==display
    assert q['display_quote_verified'] and not q['raw_quote_verified']
    assert q['quote_text_scope']=='length_preserving_masked_display'
    assert not supported(invalid)


def test_unknown_source_does_not_acquire_an_invented_source_hash():
    invalid=resolve_model_quotes([dict(source_id='UNKNOWN',quote='x')],[source()],[])
    q=invalid['invalid'][0]
    assert q['status']=='UNKNOWN_SOURCE_ID'
    assert 'source_sha256' not in q and 'lookup_display_source_sha256' not in q


def test_valid_raw_mapping_keeps_exact_original_quote_and_hash():
    s=source('C','患者张示例。决定检查。')
    display=mask_text(s['text'],['张示例'])
    result=resolve_model_quotes([dict(source_id='C',quote=display)],[s],['张示例'])
    q=result['valid'][0]
    assert q['quote']==s['text'] and q['source_sha256']==s['source_sha256']
    assert q['raw_quote_verified'] and supported(result)
