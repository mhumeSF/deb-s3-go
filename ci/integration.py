#!/usr/bin/env python3
"""Exercise the built CLI against real S3; --apt also installs signed packages."""
import argparse
import functools
import http.server
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid


def run(*args, expected=0, env=None):
    result = subprocess.run([str(a) for a in args], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, timeout=180)
    print(result.stdout, end='', flush=True)
    if result.returncode != expected:
        raise RuntimeError(f'{args[0:2]} exited {result.returncode}, expected {expected}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--apt', action='store_true')
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    bucket = os.environ.get('TEST_BUCKET', 'meadow-checks-4827')
    prefix = os.environ.get('TEST_PREFIX', f'ci/local/{uuid.uuid4().hex}')
    if not prefix.startswith('ci/') or len(prefix.split('/')) < 3 or '..' in prefix:
        raise ValueError('TEST_PREFIX must be a unique path under ci/, e.g. ci/run/attempt')
    uri = f's3://{bucket}/{prefix}/'
    env = dict(os.environ, AWS_DEFAULT_REGION='us-east-1', AWS_EC2_METADATA_DISABLED='true')
    aws = ['aws', '--region', 'us-east-1']
    with tempfile.TemporaryDirectory(prefix='deb-s3-integration-') as directory:
        root = Path(directory)
        # APT drops privileges to _apt when fetching local keys and indices.
        root.chmod(0o755)
        fixtures = root / 'fixtures'
        env['GNUPGHOME'] = str(root / 'gnupg')
        Path(env['GNUPGHOME']).mkdir(mode=0o700)
        ds = [binary, '--bucket', bucket, '--prefix', prefix, '--visibility', 'nil',
              '--codename', 'stable', '--by-hash']
        def cli(*arguments, expected=0):
            return run(*ds, *arguments, expected=expected, env=env)
        def keys():
            import json
            return json.loads(run(*aws, 's3api', 'list-objects-v2', '--bucket', bucket,
                                  '--prefix', prefix + '/', '--output', 'json', env=env)).get('Contents', [])
        try:
            run('python3', 'demo/make-debs.py', fixtures)
            run('gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', '',
                '--quick-gen-key', 'CI Test <ci@example.test>', 'ed25519', 'sign', '0', env=env)
            first = fixtures / 'demo_1.0-1_amd64.deb'
            cli('upload', '--lock', '--sign', '--fail-if-exists', first)
            cli('upload', '--lock', '--sign', '--fail-if-exists', first)
            conflict = cli('upload', '--lock', '--fail-if-exists',
                           fixtures / 'conflict/demo_1.0-1_amd64.deb', expected=1)
            assert 'different contents' in conflict, conflict
            assert '1.0-1' in cli('list', '--arch', 'amd64')
            assert 'Package: demo' in cli('show', 'demo', '1.0-1', 'amd64')
            cli('exists', 'demo', '1.0-1', 'amd64')
            cli('exists', 'demo', '9.9-9', 'amd64', expected=1)
            # A foreign lock must block writes, and remain owned by its creator.
            foreign = root / 'lock.json'
            foreign.write_text('{"token":"foreign","user":"ci","host":"ci"}')
            lockkey = prefix + '/dists/stable/lockfile'
            run(*aws, 's3api', 'put-object', '--bucket', bucket, '--key', lockkey, '--body', foreign, env=env)
            cli('upload', '--lock', '--lock-timeout', '1s', first, expected=1)
            run(*aws, 's3api', 'head-object', '--bucket', bucket, '--key', lockkey, env=env)
            run(*aws, 's3api', 'delete-object', '--bucket', bucket, '--key', lockkey, env=env)

            mirror = root / 'repo'
            def sync():
                run(*aws, 's3', 'sync', uri, mirror, '--delete', env=env)
            sync()
            release = mirror / 'dists/stable'
            run('gpg', '--verify', release / 'InRelease', env=env)
            run('gpg', '--verify', release / 'Release.gpg', release / 'Release', env=env)
            assert list(release.glob('main/binary-amd64/by-hash/SHA256/*'))

            server = None
            if args.apt:
                public_key = root / 'signing.asc'
                public_key.write_text(run('gpg', '--armor', '--export', 'ci@example.test', env=env))
                handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(mirror))
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                source = root / 'sources.list'
                source.write_text(f'deb [arch=amd64 signed-by={public_key} by-hash=force] http://127.0.0.1:{server.server_port} stable main\n')
                lists = root / 'lists'
                lists.mkdir(mode=0o755)
                apt = ['sudo', 'apt-get', '-o', f'Dir::Etc::sourcelist={source}',
                       '-o', 'Dir::Etc::sourceparts=-', '-o', f'Dir::State::lists={lists}',
                       '-o', 'APT::Get::List-Cleanup=0', '-o', 'APT::Update::Error-Mode=any']
                def install(version):
                    run(*apt, 'update')
                    run(*apt, 'install', '-y', '--no-install-recommends', f'demo={version}')
                    assert run('dpkg-query', '-W', '-f=${Version}', 'demo').strip() == version
                    assert Path('/usr/share/doc/demo/README').read_text().strip() == f'demo {version}'
                install('1.0-1')
            try:
                cli('upload', '--lock', '--sign', fixtures / 'demo_1.1-1_amd64.deb')
                cli('exists', 'demo', '1.0-1', 'amd64', expected=1)
                cli('exists', 'demo', '1.1-1', 'amd64')
                if args.apt:
                    sync()
                    install('1.1-1')
                cli('copy', 'demo', 'testing', 'main', '--arch', 'amd64', '--lock')
                cli('delete', 'demo', '--arch', 'amd64', '--lock')
                cli('clean', '--lock')
                pool = [item['Key'] for item in keys() if '/pool/' in item['Key']]
                assert len(pool) == 1 and '1.1-1' in pool[0], pool
                cli('--codename', 'testing', 'exists', 'demo', '1.1-1', 'amd64')
                cli('--codename', 'testing', 'verify')
                run(*aws, 's3api', 'delete-object', '--bucket', bucket, '--key', pool[0], env=env)
                assert 'demo' in cli('--codename', 'testing', 'verify')
                cli('--codename', 'testing', 'verify', '--fix-manifests')
                cli('--codename', 'testing', 'exists', 'demo', '1.1-1', 'amd64', expected=1)
                cli('clean', '--lock')
                assert not [item for item in keys() if '/pool/' in item['Key']]
                print('S3 integration passed' + (' including signed APT install and upgrade' if args.apt else ''), flush=True)
            finally:
                if server:
                    server.shutdown()
        finally:
            run(*aws, 's3', 'rm', uri, '--recursive', env=env)


if __name__ == '__main__':
    main()
