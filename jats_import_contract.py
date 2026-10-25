"""Offline source verification for XML pre-chunks; never a PDF compatibility shim."""
from jats_adapter import adapt_jats_bundle, chunk_jats_document, validate_jats_provenance


def validate_jats_rows(rows):
    """Rebuild the deterministic chunks from their hash-bound original XML.

    File-level permission evidence is a binding, not proof that the account is
    currently entitled. Publisher execution still requires fresh policy review.
    """
    if not rows:
        raise ValueError('JATS_CHUNKS_EMPTY')
    provenance = rows[0]['provenance']
    validate_jats_provenance(provenance)
    document = adapt_jats_bundle(provenance['source_xml'], provenance['doi'],
                                source_status=provenance['source_status'])
    policies = {(row.get('target_tokens'), row.get('max_tokens'),
                 row.get('index_references')) for row in rows}
    if len(policies) != 1:
        raise ValueError('JATS_CHUNK_POLICIES_DIFFER')
    target, maximum, references = policies.pop()
    if (type(target) is not int or type(maximum) is not int
            or not 1 <= target <= maximum <= 20000 or type(references) is not bool):
        raise ValueError('JATS_CHUNK_POLICY_INVALID')
    expected = {row['chunk_id']: row for row in chunk_jats_document(document,
        target_tokens=target, max_tokens=maximum, include_references=references)}
    source_fields = ('resource_type', 'source', 'source_parser', 'source_status',
        'source_xml', 'source_sidecar', 'source_xml_sha256', 'doi', 'endpoint',
        'permission_evidence', 'jats_product_evidence')
    trace_fields = ('document_id', 'title', 'raw_text', 'embedding_text',
        'mineru_block_ids', 'jats_block_ids', 'xml_anchors', 'locations',
        'page_start', 'page_end', 'section', 'section_raw', 'subsection', 'content_type')
    for row in rows:
        if any(row['provenance'].get(k) != provenance.get(k) for k in source_fields):
            raise ValueError('JATS_SOURCE_BINDING_DIFFERS')
        if any(row['provenance'].get(k) is not None for k in
               ('source_pdf', 'source_pdf_sha256', 'sourcePdf', 'sourcePdfSha256')):
            raise ValueError('JATS_CLAIMS_PDF')
        original = expected.get(row.get('chunk_id'))
        if original is None or any(row.get(k) != original.get(k) for k in trace_fields):
            raise ValueError('JATS_CHUNK_DIFFERS_FROM_ORIGINAL_XML')
    return provenance


def validate_jats_result_trace(result):
    """Validate restored XML trace structure, not remote/account entitlement."""
    import re
    if (result.get('resource_type') != 'jats_xml'
            or result.get('source_parser') != 'jats_adapter'
            or not re.fullmatch(r'[0-9a-f]{64}', str(result.get('source_xml_sha256') or ''))
            or result.get('source_pdf_sha256') is not None
            or result.get('page_start') is not None or result.get('page_end') is not None):
        raise ValueError('JATS_RETRIEVAL_SOURCE_INVALID')
    ids, anchors, locations = (result.get(k) for k in ('jats_block_ids', 'xml_anchors', 'locations'))
    if (not isinstance(ids, list) or not ids or ids != result.get('mineru_block_ids')
            or not isinstance(anchors, list) or not anchors or not isinstance(locations, list)
            or not locations or any(not isinstance(v, str) or not v.strip() for v in ids + anchors)):
        raise ValueError('JATS_RETRIEVAL_TRACE_INVALID')
    for location in locations:
        if (not isinstance(location, dict) or location.get('block_id') not in ids
                or location.get('xml_anchor') not in anchors
                or location.get('page_index') is not None or location.get('bbox') is not None):
            raise ValueError('JATS_RETRIEVAL_LOCATION_INVALID')
