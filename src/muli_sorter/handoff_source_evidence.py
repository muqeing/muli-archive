"""Compare completed-archive evidence across a receipt reader's device change.

This is NOT admission evidence and never validates a source for copy/deletion.
The caller must bind the original completed request to the unchanged persistent
roots and keep the ordinary target checks. Only the receipt reader's transient
st_dev fields may be projected back; the entire manifest digest must then equal
the archived digest. Receipt bytes, inode, times and source proofs stay exact.
"""
import re

from .review import digest


HEX = re.compile(r'[a-f0-9]{64}\Z')
FIELDS = frozenset((
    'schema', 'status', 'verified_at', 'batch_id', 'batch_uid', 'revision',
    'manifest_id', 'source_id', 'original_manifest_blake3',
    'original_ingest_complete_blake3', 'receipt_signature', 'receipt_blake3',
    'receipt_root',
))
VOLATILE = {'receipt_root', 'receipt_signature'}
MAX_PROOFS = 256


def _integers(values, length):
    return (type(values) is list and len(values) == length and
            all(type(value) is int and value >= 0 for value in values))


def _reader_shape(row):
    root, signature = row.get('receipt_root'), row.get('receipt_signature')
    if (type(root) is not dict or set(root) != {'path', 'dev', 'ino'} or
            type(root['path']) is not str or not root['path'].startswith('/') or
            not _integers([root['dev'], root['ino']], 2) or root['ino'] == 0 or
            type(signature) is not list or len(signature) != 3 or
            not _integers(signature[0], 2) or not _integers(signature[1], 5) or
            type(signature[2]) is not str or not HEX.fullmatch(signature[2])):
        return False
    return (signature[0] == [root['dev'], root['ino']] and
            signature[1][0] == root['dev'] and signature[1][1] != 0 and
            signature[2] == row['receipt_blake3'])


def matches(manifest, evidence, *, current_digest=None, cache=None):
    """Read-only comparison; cache is private to one immutable feedback build."""
    expected = evidence.get('content_digest')
    if (manifest.get('manifest_id') != evidence.get('manifest_id') or
            type(expected) is not str or not HEX.fullmatch(expected)):
        return False
    current_digest = digest(manifest) if current_digest is None else current_digest
    if current_digest == expected:
        return True
    current, old = manifest.get('_postcopy_evidence'), evidence.get('postcopy')
    if (type(current) is not dict or type(old) is not dict or set(old) != FIELDS or
            not FIELDS.issubset(current) or
            current['schema'] not in ('postcopy-verification/1', 'postcopy-verification/2') or
            current['status'] != 'completed' or
            any(type(current[key]) is not type(old[key]) or current[key] != old[key]
                for key in FIELDS - VOLATILE) or
            not _reader_shape(current) or not _reader_shape(old)):
        return False
    root, prior_root = current['receipt_root'], old['receipt_root']
    sig, prior_sig = current['receipt_signature'], old['receipt_signature']
    if (root['dev'] == prior_root['dev'] or root['path'] != prior_root['path'] or
            root['ino'] != prior_root['ino'] or sig[0][1:] != prior_sig[0][1:] or
            sig[1][1:] != prior_sig[1][1:] or sig[2] != prior_sig[2]):
        return False
    key = (current_digest, expected, digest(old))
    if cache is not None and key in cache:
        return True
    # Shallow projections share the already validated file evidence. Never copy
    # or rewrite the stored manifest, receipt or source file signature fields.
    projection = {**manifest, '_postcopy_evidence': {
        **current, 'receipt_root': prior_root, 'receipt_signature': prior_sig}}
    if digest(projection) != expected:
        return False
    if cache is not None:
        while len(cache) >= MAX_PROOFS:
            cache.pop(next(iter(cache)))
        cache[key] = True
    return True
