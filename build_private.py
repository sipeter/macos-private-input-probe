"""Run a reviewed private build and return large results to its private release."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile

PHASE = 'configuration'
CHUNK_BYTES = 1024 ** 3


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(command, cwd, log, env, timeout=1200, progress=None):
    with Path(log).open('ab') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    code = process.wait(timeout=min(45, max(1, deadline - time.monotonic())))
                    if code:
                        raise RuntimeError('Private command failed')
                    return
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
                    print('Private operation is active.', flush=True)
                    if progress:
                        progress()
        except BaseException:
            if process.poll() is None:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise


def extract_source(archive, target):
    target.mkdir()
    with zipfile.ZipFile(archive) as z:
        if sum(i.file_size for i in z.infolist()) > 1024 ** 3:
            raise ValueError('Source exceeds limit')
        for entry in z.infolist():
            path = target / entry.filename
            mode = entry.external_attr >> 16
            if ('\\' in entry.orig_filename or ':' in entry.filename
                    or not path.resolve().is_relative_to(target.resolve())
                    or mode & 0o170000 == 0o120000):
                raise ValueError('Unsupported source member')
        z.extractall(target)


def split_file(source, directory, limit=CHUNK_BYTES):
    parts = []
    with Path(source).open('rb') as stream:
        index = 0
        while block := stream.read(min(limit, 1024 ** 2)):
            path = directory / f'result.part{index:03d}'
            size = 0
            with path.open('xb') as output:
                while block:
                    output.write(block)
                    size += len(block)
                    if size == limit:
                        break
                    block = stream.read(min(limit - size, 1024 ** 2))
            parts.append({'name': path.name, 'bytes': size, 'sha256': digest(path)})
            index += 1
    return parts


def pack_result(source, destination):
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(source.rglob('*')):
            if path.is_symlink():
                raise ValueError('Unsupported result member')
            if path.is_file():
                z.write(path, path.relative_to(source).as_posix())


def execute(work):
    global PHASE
    spec = json.loads(os.environ.pop('PRIVATE_BUILD_SPEC'))
    token = os.environ.pop('PRIVATE_TRANSFER_TOKEN')
    repo = spec['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid repository')
    if not re.fullmatch(r'[0-9a-f]{64}', spec['source_sha256']):
        raise ValueError('Invalid source identity')
    # Child builds receive no repository token or Actions control-file paths.
    build_env = {k: v for k, v in os.environ.items()
                 if not k.startswith(('GITHUB_', 'ACTIONS_', 'RUNNER_'))
                 and k not in ('GH_TOKEN', 'GITHUB_TOKEN')}
    transfer_env = dict(build_env, GH_TOKEN=token, GH_PROMPT_DISABLED='1')
    del token
    result = work / 'result'
    result.mkdir()
    log = result / 'private.log'
    source = work / 'source'
    status = 'failed'
    run_id = os.environ['GITHUB_RUN_ID']
    attempt = os.environ['GITHUB_RUN_ATTEMPT']
    if not re.fullmatch(r'[0-9]+', run_id) or not re.fullmatch(r'[0-9]+', attempt):
        raise ValueError('Invalid run identity')
    tag = f'private-build-{run_id}-{attempt}'
    # Check visibility before any upload. Do not print the response.
    meta = subprocess.run(['gh', 'api', 'repos/' + repo], env=transfer_env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    if json.loads(meta.stdout).get('private') is not True:
        raise ValueError('Result repository must be private')
    live_tag = f'private-live-{run_id}-{attempt}'
    run(['gh', 'release', 'create', live_tag, '--repo', repo, '--title', 'Private build progress',
         '--notes', 'Temporary private diagnostic snapshots.'], work, log, transfer_env)
    last_snapshot = 0

    def progress():
        nonlocal last_snapshot
        if time.monotonic() - last_snapshot < 120:
            return
        last_snapshot = time.monotonic()
        snapshot = work / 'progress.log'
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 65536))
            snapshot.write_bytes(stream.read())
        with (work / 'progress-transfer.log').open('ab') as output:
            subprocess.run(['gh', 'release', 'upload', live_tag, str(snapshot), '--repo', repo, '--clobber'],
                           env=transfer_env, stdout=output, stderr=subprocess.STDOUT, timeout=90)
    try:
        PHASE = 'private-input-transfer'
        for asset in spec['assets']:
            name = asset['name']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
                raise ValueError('Invalid asset name')
            run(['gh', 'release', 'download', asset['release'], '--repo', repo,
                 '--pattern', name, '--dir', str(work)], work, log, transfer_env)
            if digest(work / name) != asset['sha256']:
                raise ValueError('Private asset identity mismatch')
        extract_source(work / 'source.zip', source)
        if digest(work / 'source.zip') != spec['source_sha256']:
            raise ValueError('Source identity mismatch')
        PHASE = 'private-cache-transfer'
        if spec.get('cache_url'):
            from urllib.parse import urlsplit
            url = urlsplit(spec['cache_url'])
            if url.scheme != 'https' or not url.hostname.endswith('.blob.core.windows.net'):
                raise ValueError('Unsupported cache location')
            with urllib.request.urlopen(spec['cache_url'], timeout=120) as response:
                with (work / 'cache.tzst').open('xb') as stream:
                    shutil.copyfileobj(response, stream, 1024 ** 2)
            if (work / 'cache.tzst').stat().st_size != spec['cache_bytes']:
                raise ValueError('Cache size mismatch')
            (result / 'cache-transfer.json').write_text(json.dumps({
                'sha256': digest(work / 'cache.tzst'), 'bytes': spec['cache_bytes']}))
        PHASE = 'private-compilation'
        build_env.update(PRIVATE_INPUT_DIR=str(work), PRIVATE_RESULT_DIR=str(result))
        run(['bash', 'private-build.sh'], source, log, build_env, timeout=4 * 3600, progress=progress)
        status = 'passed'
    except Exception as error:
        # No traceback or exception text is emitted to public output.
        with log.open('a') as stream:
            stream.write('\nPrivate operation exception: ' + repr(error) + '\n')
    finally:
        if source.exists() and (source / 'collect-private.py').is_file():
            try:
                run(['python3', 'collect-private.py'], source, log, build_env, timeout=1200)
            except Exception:
                status = 'failed'
    last_phase = PHASE
    PHASE = 'private-result-transfer'
    report = {'status': status, 'phase': last_phase, 'source_sha256': spec['source_sha256'],
              'run_id': run_id, 'run_attempt': attempt, 'public_revision': os.environ['GITHUB_SHA']}
    (result / 'transport-report.json').write_text(json.dumps(report, indent=2) + '\n')
    archive = work / 'result.zip'
    pack_result(result, archive)
    parts = split_file(archive, work)
    manifest = work / 'result-manifest.json'
    manifest.write_text(json.dumps({**report, 'bytes': archive.stat().st_size,
                                   'sha256': digest(archive), 'parts': parts}, indent=2) + '\n')
    upload = ['gh', 'release', 'create', tag, '--repo', repo, '--title', 'Private build result',
              '--notes', 'Private build output. Inspect result-manifest.json before use.', str(manifest)]
    upload += [str(work / p['name']) for p in parts]
    run(upload, work, work / 'transfer.log', transfer_env, timeout=2400)
    if status != 'passed':
        raise RuntimeError('Private build failed; inspect private release')


def main():
    try:
        with tempfile.TemporaryDirectory(prefix='private-build-') as directory:
            execute(Path(directory))
    except Exception:
        print(f'Build failed at {PHASE}. Inspect the private result store.', flush=True)
        return 1
    print('Build passed. Results are in the private release.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
