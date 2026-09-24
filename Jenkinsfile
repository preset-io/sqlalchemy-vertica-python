// Preset fork publisher; same ci-user / preset-pypi path as sqlalchemy-drill.
// PR wheels are explicitly versioned +pr.<number>.<sha>; stable wheels may
// only be published from reviewed master. Never overwrite an artifact.
podTemplate(
    imagePullSecrets: ['preset-pull'],
    containers: [
        containerTemplate(name: 'ci', image: 'preset/ci:latest',
            ttyEnabled: true, command: 'cat'),
        containerTemplate(name: 'py-ci', image: 'preset/python:3.9.18-2024-02-21-ci',
            ttyEnabled: true, command: 'cat')
    ]
) {
    node(POD_LABEL) {
        checkout scm
        def revision = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
        def baseVersion = sh(script: "sed -n \"s/^__version__ = '\\(.*\\)'/\\1/p\" setup.py",
            returnStdout: true).trim()
        boolean isMaster = env.BRANCH_NAME == 'master'
        boolean isPR = env.CHANGE_ID != null
        if (!isMaster && !isPR) {
            error('Only master and pull-request builds publish; use a PR.')
        }
        def version = isMaster ? baseVersion : "${baseVersion}+pr.${env.CHANGE_ID}.${revision.take(12)}"
        def wheel = "sqlalchemy_vertica_python-${version}-py3-none-any.whl"
        def key = "sqlalchemy-vertica-python/${wheel}"

        container('py-ci') {
            stage('Test and build') {
                withEnv(["PUBLISH_VERSION=${version}"]) {
                    sh '''
                        set -eu
                        python -m venv .venv
                        .venv/bin/pip install 'sqlalchemy==2.0.52' 'vertica-python==0.10.2' pytest build
                        .venv/bin/pip install --no-deps .
                        .venv/bin/python -m pytest -q tests/unit
                        python - <<'PY'
import os
from pathlib import Path
path = Path('setup.py')
source = path.read_text()
lines = source.splitlines(keepends=True)
assert sum(line.startswith('__version__ = ') for line in lines) == 1
path.write_text(''.join('__version__ = ' + repr(os.environ['PUBLISH_VERSION']) + '\\n'
                        if line.startswith('__version__ = ') else line for line in lines))
PY
                        export SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct)
                        .venv/bin/python -m build --wheel
                        .venv/bin/pip install --force-reinstall --no-deps dist/*.whl
                        .venv/bin/python - <<'PY'
import importlib.metadata as im
import os
import sqlalchemy as sa
assert im.version('sqlalchemy-vertica-python') == os.environ['PUBLISH_VERSION']
engine = sa.create_engine('vertica+vertica_python://user@localhost/db')
assert engine.dialect.name == 'vertica'
engine.dispose()
PY
                    '''
                }
            }
        }
        container('ci') {
            stage('Publish immutable wheel') {
                withCredentials([[
                    $class: 'AmazonWebServicesCredentialsBinding',
                    credentialsId: 'ci-user',
                    accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                    secretKeyVariable: 'AWS_SECRET_ACCESS_KEY'
                ]]) {
                    withEnv(["WHEEL=${wheel}", "KEY=${key}"]) {
                        sh '''
                            set -eu
                            python -m pip install --quiet 'boto3>=1.36,<2'
                            python - <<'PY'
import hashlib
import os
from pathlib import Path
import boto3
body = Path('dist', os.environ['WHEEL']).read_bytes()
s3 = boto3.client('s3')
s3.put_object(Bucket='preset-pypi', Key=os.environ['KEY'], Body=body, IfNoneMatch='*')
stored = s3.get_object(Bucket='preset-pypi', Key=os.environ['KEY'])['Body'].read()
assert stored == body
Path('published.sha256').write_text(hashlib.sha256(stored).hexdigest() + '  ' + os.environ['WHEEL'] + '\\n')
PY
                        '''
                    }
                }
            }
        }
        archiveArtifacts artifacts: 'dist/*.whl,published.sha256', fingerprint: true
    }
}
