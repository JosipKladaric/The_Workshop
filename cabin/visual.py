"""Optional local HTML rendering through an already-installed Chromium browser.

This renders trusted project code. It is not a security sandbox and does not
install a browser or claim to prevent network access from page scripts.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from .engine import safe_path


def browser_binary():
    configured = os.environ.get('WORKSHOP_BROWSER')
    if configured and Path(configured).is_file():
        return configured
    for name in ('msedge', 'google-chrome', 'chromium', 'chromium-browser', 'chrome'):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if base:
            for relative in ('Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'):
                candidate = Path(base) / relative
                if candidate.is_file():
                    return str(candidate)
    return None


def render_html(folder, path, destination, width=1280, height=900):
    """Render one contained HTML file, returning the actual PNG path."""
    file = safe_path(folder, path)
    if file.suffix.lower() not in ('.html', '.htm') or not file.is_file():
        raise ValueError('Choose an existing project HTML file')
    if type(width) is not int or type(height) is not int or not 320 <= width <= 2560 or not 320 <= height <= 2560:
        raise ValueError('Screenshot dimensions must be between 320 and 2560 pixels')
    binary = browser_binary()
    if not binary:
        raise ValueError('No local Chromium browser found. Install Edge/Chrome/Chromium or set WORKSHOP_BROWSER before going offline.')
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workshop-browser-') as profile:
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = subprocess.run([binary, '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
            '--disable-extensions', '--disable-background-networking', '--hide-scrollbars',
            '--user-data-dir=' + profile, '--screenshot=' + str(destination),
            f'--window-size={width},{height}', '--virtual-time-budget=2000', file.as_uri()],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, **flags)
        if result.returncode or not destination.is_file() or destination.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
            raise ValueError('Local browser rendering failed: ' + result.stderr.decode(errors='replace')[-1000:])
    return destination
