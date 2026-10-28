"""Package application code only; never include workspaces, passwords or QA data."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'releases' / 'The-Workshop.zip'


def build():
    OUTPUT.parent.mkdir(exist_ok=True)
    sources = [ROOT / name for name in ('README.md', 'ARCHITECTURE.md', 'IMPROVEMENTS.md', 'requirements-browser.txt', 'run.py', 'start.ps1', 'start.bat', '.gitignore')]
    for directory in ('cabin', 'tests', 'scripts'):
        sources.extend(p for p in (ROOT / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    with zipfile.ZipFile(OUTPUT, 'w', zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(sources):
            archive.write(source, 'The Workshop/' + source.relative_to(ROOT).as_posix())
    print(f'Release: {OUTPUT}\nFiles: {len(sources)}\nBytes: {OUTPUT.stat().st_size}')


if __name__ == '__main__':
    build()
