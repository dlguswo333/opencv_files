"""Collect licenses, build details and matching FFmpeg source after INSTALL."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen


def git_revision(path):
    return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()


def download(entry, commit, cache):
    path = cache / Path(entry['path']).name
    data = path.read_bytes() if path.exists() else None
    if data is None:
        url = f"https://raw.githubusercontent.com/opencv/opencv_3rdparty/{commit}/{entry['path']}"
        with urlopen(url, timeout=120) as response:
            data = response.read()
    digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if len(data) != entry['size'] or digest != entry['sha']:
        raise ValueError(f'Source archive integrity check failed: {path.name}')
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def collect(root, install, vcpkg, triplet, cache):
    config = json.loads((root / 'tools/ffmpeg-sources.json').read_text())
    # Clear the old success record so a failed run cannot reuse it.
    output = install / 'redistribution'
    (output / 'build-record.json').unlink(missing_ok=True)
    manifest = (root / 'opencv/3rdparty/ffmpeg/ffmpeg.cmake').read_text()
    if config['binary_commit'] not in manifest:
        raise ValueError('OpenCV FFmpeg revision changed; update tools/ffmpeg-sources.json.')
    binaries = list(install.rglob('opencv_videoio_ffmpeg*.dll'))
    if len(binaries) != 1:
        raise ValueError('Expected exactly one FFmpeg DLL in this architecture install.')
    binary = binaries[0]
    arch = triplet.split('-')[0]
    if arch not in config['binary_sha256']:
        raise ValueError(f'No verified FFmpeg source mapping for {arch}.')
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    if binary_sha256 != config['binary_sha256'][arch]:
        raise ValueError('FFmpeg DLL does not match the pinned corresponding-source build.')
    share = vcpkg / 'installed' / triplet / 'share'
    if not (share / 'libavif/copyright').is_file():
        raise ValueError('libavif installed copyright is missing.')
    native_notices = install / 'etc/licenses'
    if not native_notices.is_dir() or not any(native_notices.rglob('*')):
        raise ValueError('OpenCV installed third-party licenses are missing.')
    archives = [(entry, download(entry, config['source_commit'], cache)) for entry in config['archives']]
    record = {
        'triplet': triplet,
        'opencv_files_revision': git_revision(root),
        'opencv_revision': git_revision(root / 'opencv'),
        'opencv_contrib_revision': git_revision(root / 'opencv_contrib'),
        'vcpkg_revision': git_revision(vcpkg),
        'ffmpeg_source_commit': config['source_commit'],
        'ffmpeg_binary': binary.relative_to(install).as_posix(),
        'ffmpeg_sha256': binary_sha256,
        'materials_sha256': {},
    }
    with tempfile.TemporaryDirectory(dir=install) as temp:
        staged = Path(temp)
        notices = staged / 'licenses'
        shutil.copytree(native_notices, notices / 'opencv-installed')
        for component in ('opencv', 'opencv_contrib'):
            shutil.copy2(root / component / 'LICENSE', notices / (component + '-LICENSE.txt'))
        for copyright_file in share.glob('*/copyright'):
            target = notices / 'vcpkg' / copyright_file.parent.name / 'copyright'
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copyright_file, target)
        for name in ('license.txt', 'readme.txt'):
            shutil.copy2(root / 'opencv/3rdparty/ffmpeg' / name, notices / ('ffmpeg-' + name))
        for entry, archive in archives:
            target = staged / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, target)
            # Copy license notices from source archives without running their code.
            with tarfile.open(archive, 'r:xz') as tar:
                for member in tar.getmembers():
                    if member.isfile() and Path(member.name).name in ('LICENSE', 'PATENTS', 'COPYING.LGPLv2.1'):
                        relative = Path(member.name)
                        if relative.is_absolute() or '..' in relative.parts:
                            raise ValueError('Unsafe path in source notice archive.')
                        dest = notices / 'ffmpeg-source-dependencies' / archive.name / relative
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(tar.extractfile(member).read())
        (staged / 'FFmpeg.txt').write_text(
            'FFmpeg is licensed under LGPL 2.1 or later.\n'
            'License: licenses/ffmpeg-license.txt\n'
            'Matching source, wrapper, dependencies and build scripts: sources/\n'
            'Keep these files when sharing the library. The LGPL allows users to\n'
            'modify or replace it and reverse engineer it to debug changes.\n')
        # Save installed versions and build settings.
        shutil.copy2(vcpkg / 'installed/vcpkg/status', staged / 'vcpkg-status.txt')
        shutil.copy2(install.parent / 'CMakeCache.txt', staged / 'CMakeCache.txt')
        for file in staged.rglob('*'):
            if file.is_file():
                record['materials_sha256'][file.relative_to(staged).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
        (staged / 'build-record.json').write_text(json.dumps(record, indent=2) + '\n')
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(staged, output)
    print(f'Prepared redistribution materials: {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--install', type=Path, required=True)
    parser.add_argument('--vcpkg', type=Path, required=True)
    parser.add_argument('--triplet', required=True)
    parser.add_argument('--cache', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    collect(root, args.install.resolve(), args.vcpkg.resolve(), args.triplet,
            args.cache or root / '.source-cache')
