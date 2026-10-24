"""Read-only integrity/provenance audit of a local portable verification build."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_portable import audit, source_provenance
from bagu_trainer.version import __version__


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(output):
    from PyInstaller.archive.readers import CArchiveReader

    assert not subprocess.check_output(
        ['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT
    ).strip(), 'Audit requires a clean checkout'
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    tree = source_provenance()
    production = output / 'BaguTrainer'
    driver = output / 'BaguTrainerAcceptance'
    manifest = json.loads((production / 'BUILD_MANIFEST.json').read_text('utf-8'))
    provenance = json.loads((driver / '_internal/acceptance-provenance.json').read_text('utf-8'))
    assert manifest['version'] == __version__
    for record in (manifest, provenance):
        assert record['source_commit'] == commit, 'Commit mismatch'
        assert record['source_dirty'] is False, 'Dirty source'
        assert record['source_tree'] == tree, 'Source fingerprint mismatch'
    entries = audit(production)
    actual = {e['path']: e for e in entries if e['path'] != 'BUILD_MANIFEST.json'}
    expected = {e['path']: e for e in manifest['files']}
    assert len(expected) == len(manifest['files']), 'Duplicate manifest paths'
    assert actual == expected, 'Payload inventory, size or SHA-256 mismatch'
    modules = set()
    for path in (ROOT / 'bagu_trainer').rglob('*.py'):
        parts = list(path.relative_to(ROOT).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts.pop()
        modules.add('.'.join(parts))
    result = {'version': __version__, 'source_commit': commit, 'source_dirty': False,
              'source_tree_sha256': tree['sha256'], 'payload_files': len(expected),
              'artifacts': {}, 'modules': sorted(modules)}
    for directory, name in ((production, f'BaguTrainer-{__version__}-windows-x64'),
                            (driver, 'BaguTrainerAcceptance-windows-x64')):
        archive = output / (name + '.zip')
        digest = sha(archive)
        assert Path(str(archive) + '.sha256').read_text('ascii').split() == [digest, archive.name]
        inventory = {p.relative_to(directory).as_posix(): {'bytes': p.stat().st_size, 'sha256': sha(p)}
                     for p in sorted(directory.rglob('*')) if p.is_file()}
        with zipfile.ZipFile(archive) as zipped:
            assert zipped.testzip() is None, 'ZIP CRC failure'
            names = [i.filename for i in zipped.infolist() if not i.is_dir()]
            assert len(names) == len(set(names)), 'Duplicate ZIP members'
            assert set(names) == {directory.name + '/' + p for p in inventory}, 'ZIP inventory mismatch'
            for path, entry in inventory.items():
                data = zipped.read(directory.name + '/' + path)
                assert len(data) == entry['bytes'] and hashlib.sha256(data).hexdigest() == entry['sha256']
        binary = directory / (directory.name + '.exe')
        embedded = CArchiveReader(str(binary)).open_embedded_archive('PYZ.pyz')
        assert modules <= set(embedded.toc), 'Missing application modules: ' + str(modules - set(embedded.toc))
        result['artifacts'][archive.name] = {'path': str(archive), 'bytes': archive.stat().st_size,
            'sha256': digest, 'crc_verified': True, 'inventory': inventory,
            'application_modules_verified': len(modules)}
    result['passed'] = True
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    options = parser.parse_args()
    report = verify(options.output_dir.resolve())
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in {'artifacts', 'modules'}}))
