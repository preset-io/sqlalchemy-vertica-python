"""Publish an immutable wheel; only PR retries may reuse identical bytes."""
import hashlib
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def publish_wheel(s3, bucket, key, body, allow_identical=False):
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch='*')
    except ClientError as error:
        if not allow_identical or error.response['Error']['Code'] != 'PreconditionFailed':
            raise
        print('Artifact already exists; verifying byte equality without overwriting.')

    response = s3.get_object(Bucket=bucket, Key=key)
    try:
        stored = response['Body'].read()
    finally:
        response['Body'].close()
    if stored != body:
        raise RuntimeError(
            'Stored wheel differs from this commit\'s freshly built artifact; '
            'refusing to overwrite or accept it (local sha256={}, stored sha256={}).'.format(
                hashlib.sha256(body).hexdigest(), hashlib.sha256(stored).hexdigest(),
            )
        )
    print('Published wheel verified byte-for-byte: ' + key)
    return hashlib.sha256(stored).hexdigest()


def main():
    wheel = os.environ['WHEEL']
    receipt = Path('published.sha256')
    # Do not leave a previous build's success receipt after a failed retry.
    if receipt.exists():
        receipt.unlink()
    digest = publish_wheel(
        boto3.client('s3'), 'preset-pypi', os.environ['KEY'],
        Path('dist', wheel).read_bytes(),
        allow_identical=os.environ.get('ALLOW_IDENTICAL_PR_ARTIFACT') == 'true',
    )
    receipt.write_text(digest + '  ' + wheel + '\n')


if __name__ == '__main__':
    main()
