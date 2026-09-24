"""Exercise the same immutable publisher used by Jenkins without AWS access."""
import hashlib
import io
from pathlib import Path
import runpy
from unittest.mock import Mock

from botocore.exceptions import ClientError
import pytest

publish_wheel = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / 'ci' / 'publish_wheel.py')
)['publish_wheel']


def client(stored=b'wheel', error=None):
    s3 = Mock()
    s3.get_object.return_value = {'Body': io.BytesIO(stored)}
    if error:
        s3.put_object.side_effect = ClientError({'Error': {'Code': error}}, 'PutObject')
    return s3


def test_new_artifact_is_conditionally_written_and_verified():
    s3 = client()
    assert publish_wheel(s3, 'bucket', 'key', b'wheel') == hashlib.sha256(b'wheel').hexdigest()
    s3.put_object.assert_called_once_with(
        Bucket='bucket', Key='key', Body=b'wheel', IfNoneMatch='*',
    )
    s3.get_object.assert_called_once_with(Bucket='bucket', Key='key')


def test_identical_pr_retry_succeeds_without_overwrite():
    s3 = client(error='PreconditionFailed')
    assert publish_wheel(s3, 'bucket', 'key', b'wheel', True) == hashlib.sha256(b'wheel').hexdigest()
    s3.put_object.assert_called_once_with(
        Bucket='bucket', Key='key', Body=b'wheel', IfNoneMatch='*',
    )
    assert s3.get_object.return_value['Body'].closed


@pytest.mark.parametrize('error', [None, 'PreconditionFailed'])
def test_different_bytes_fail_even_for_pr(error):
    s3 = client(stored=b'different wheel', error=error)
    with pytest.raises(RuntimeError, match='Stored wheel differs.*refusing to overwrite'):
        publish_wheel(s3, 'bucket', 'key', b'wheel', True)
    assert s3.put_object.call_count == 1


def test_stable_retry_fails_even_if_bytes_match():
    s3 = client(error='PreconditionFailed')
    with pytest.raises(ClientError, match='PreconditionFailed'):
        publish_wheel(s3, 'bucket', 'key', b'wheel')
    s3.get_object.assert_not_called()
    assert s3.put_object.call_count == 1


@pytest.mark.parametrize('error', ['AccessDenied', 'ConditionalRequestConflict'])
def test_other_s3_errors_are_not_accepted(error):
    s3 = client(error=error)
    with pytest.raises(ClientError, match=error):
        publish_wheel(s3, 'bucket', 'key', b'wheel', True)
    s3.get_object.assert_not_called()


def test_unreadable_existing_artifact_fails_closed():
    s3 = client(error='PreconditionFailed')
    s3.get_object.side_effect = ClientError({'Error': {'Code': 'AccessDenied'}}, 'GetObject')
    with pytest.raises(ClientError, match='AccessDenied'):
        publish_wheel(s3, 'bucket', 'key', b'wheel', True)
