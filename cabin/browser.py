"""Bounded browser actions for trusted local project UIs.

Playwright is optional. This module never installs a package/browser, controls
the personal desktop, or executes model-supplied JavaScript. Every context has
an ephemeral loopback-only static origin confined to one project workspace.
"""
import importlib.util
import mimetypes
import queue
import re
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ASSET_TYPES = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.svg', '.png', '.jpg',
               '.jpeg', '.gif', '.webp', '.ico', '.txt', '.woff', '.woff2', '.ttf', '.otf'}
ACTIONS = {'open', 'inspect', 'click', 'fill', 'select', 'scroll', 'screenshot', 'errors', 'assert_text', 'close'}


def contained_asset(folder, name):
    if not isinstance(name, str) or not name or len(name) > 1000 or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('Use a relative project asset path')
    root = Path(folder).resolve()
    raw = Path(name)
    if raw.is_absolute() or any(p in ('..', '.') or p.startswith('.') for p in raw.parts):
        raise ValueError('Hidden paths and parent traversal are not browser assets')
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Browser assets cannot follow symbolic links')
    path = current.resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in ASSET_TYPES:
        raise ValueError('Choose an existing public project asset')
    if path.stat().st_size > 5_000_000:
        raise ValueError('Browser asset exceeds 5 MB')
    return path


def same_origin(url, origin):
    try:
        parsed = urlsplit(url)
        base = urlsplit(origin)
        return (parsed.scheme, parsed.hostname, parsed.port) == (base.scheme, base.hostname, base.port) and not parsed.username and not parsed.password
    except ValueError:
        return False


class AssetHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise ValueError('Absolute URLs are not supported')
            name = unquote(parsed.path).lstrip('/') or 'index.html'
            path = contained_asset(self.server.folder, name)
            content = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(content)
        except (ValueError, OSError):
            self.send_error(404, 'Project asset unavailable')
        except (BrokenPipeError, ConnectionResetError):
            pass


def locator_for(page, arguments):
    target = arguments.get('target', arguments)
    if not isinstance(target, dict):
        raise ValueError('Target must name a role, label, or test_id')
    if isinstance(target.get('test_id'), str) and target['test_id']:
        return page.get_by_test_id(target['test_id'][:300])
    if isinstance(target.get('label'), str) and target['label']:
        return page.get_by_label(target['label'][:300], exact=True)
    if isinstance(target.get('role'), str) and isinstance(target.get('name'), str) and target['name']:
        if target['role'] not in {'button', 'link', 'textbox', 'checkbox', 'radio', 'combobox', 'option', 'tab', 'menuitem', 'switch', 'spinbutton'}:
            raise ValueError('Choose a supported interactive accessibility role')
        return page.get_by_role(target['role'], name=target['name'][:300], exact=True)
    raise ValueError('Target requires label, test_id, or role plus name')


class BrowserSessions:
    def __init__(self, store):
        self.store = store
        self.jobs = queue.Queue(maxsize=32)
        self.sessions = {}
        self.driver = None
        self.browser = None
        self.thread = None
        self.guard = threading.Lock()
        self.stopped = False

    def capabilities(self):
        from .visual import browser_binary
        try:
            installed = importlib.util.find_spec('playwright.sync_api') is not None
        except ModuleNotFoundError:
            installed = False
        binary = browser_binary()
        return {'available': bool(installed and binary), 'playwright_installed': installed,
                'browser_found': bool(binary), 'actions': sorted(ACTIONS),
                'requirements': 'Install Python playwright >=1.48 and Edge/Chrome/Chromium while online; set WORKSHOP_BROWSER if needed. No browser is downloaded automatically.',
                'scope': 'Dedicated headless project browser; external requests, downloads, popups and WebSockets blocked. Static project assets only; no backend startup.'}

    def _submit(self, operation, *args):
        future = Future()
        with self.guard:
            if self.stopped:
                raise ValueError('Browser session service has stopped')
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, name='workshop-browser-driver', daemon=True)
                self.thread.start()
            try:
                self.jobs.put_nowait((future, operation, args))
            except queue.Full:
                raise ValueError('Browser action queue is full')
        try:
            return future.result(timeout=35)
        except FutureTimeout:
            future.cancel()
            raise ValueError('Browser action timed out; wait for cleanup before retrying')

    def _loop(self):
        while True:
            future, operation, args = self.jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = operation(*args)
                future.set_result(result)
            except Exception as exc:
                future.set_exception(ValueError(str(exc)))
            if operation == self._shutdown:
                return

    def perform(self, pid, tid, folder, action, arguments=None):
        if not isinstance(pid, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', pid) or type(tid) is not int or tid < 0:
            raise ValueError('Invalid browser project/task identifier')
        if action not in ACTIONS or not isinstance(arguments or {}, dict):
            raise ValueError('Choose a supported browser action with object arguments')
        return self._submit(self._perform, pid, tid, Path(folder).resolve(), action, arguments or {})

    def close(self, pid=None):
        with self.guard:
            if self.thread is None or self.stopped:
                return
        return self._submit(self._close_project if pid else self._shutdown, pid) if pid else self._submit(self._shutdown)

    def _start(self):
        if self.browser:
            return
        capability = self.capabilities()
        if not capability['available']:
            raise ValueError('Interactive browser testing is unavailable. ' + capability['requirements'])
        from playwright.sync_api import sync_playwright
        from .visual import browser_binary
        self.driver = sync_playwright().start()
        try:
            self.browser = self.driver.chromium.launch(executable_path=browser_binary(), headless=True,
                args=['--disable-background-networking', '--no-first-run', '--disable-extensions'])
        except Exception:
            self.driver.stop()
            self.driver = None
            raise

    def _create(self, pid, tid, folder, arguments):
        self._start()
        # The target machine has limited RAM. Old task pages are disposable;
        # observations and screenshots are recorded by the harness separately.
        while len(self.sessions) >= 2:
            self._close_one(next(iter(self.sessions)))
        width, height = arguments.get('width', 1280), arguments.get('height', 900)
        if any(type(n) is not int or not 320 <= n <= 2560 for n in (width, height)):
            raise ValueError('Browser viewport dimensions must be 320–2560')
        server = ThreadingHTTPServer(('127.0.0.1', 0), AssetHandler)
        server.daemon_threads = True
        server.folder = folder
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        context = None
        try:
            context = self.browser.new_context(viewport={'width': width, 'height': height},
                                               accept_downloads=False, service_workers='block')
            context.set_default_timeout(5000)
            context.set_default_navigation_timeout(10000)
            context.route('**/*', lambda route: route.continue_() if same_origin(route.request.url, origin) else route.abort('blockedbyclient'))
            if not hasattr(context, 'route_web_socket'):
                raise ValueError('Browser testing requires Playwright >=1.48 to block WebSockets')
            context.route_web_socket('**/*', lambda socket: socket.close())
            page = context.new_page()
            errors = []
            def record_error(message):
                errors.append(str(message)[:2000])
                del errors[:-100]
            page.on('console', lambda message: record_error(message.text) if message.type == 'error' else None)
            page.on('pageerror', record_error)
            page.on('popup', lambda popup: popup.close())
            page.on('download', lambda download: download.cancel())
            page.on('dialog', lambda dialog: dialog.dismiss())
            session = dict(folder=folder, server=server, serving=serving, context=context, page=page,
                           origin=origin, errors=errors, width=width, height=height)
            self.sessions[(pid, tid)] = session
            return session
        except Exception:
            if context:
                context.close()
            server.shutdown(); server.server_close(); serving.join(timeout=2)
            raise

    def _observe(self, session):
        page = session['page']
        # Fixed harness script inspects rendered DOM; agents cannot supply code.
        controls = page.locator('button,input,textarea,select,a[href],[role="button"],[role="tab"]').evaluate_all('''elements => elements.slice(0,80).map(e => ({
            tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),name:e.getAttribute('aria-label') || e.innerText || e.labels?.[0]?.innerText || e.getAttribute('placeholder') || '',
            type:e.getAttribute('type'),test_id:e.getAttribute('data-testid'),disabled:e.disabled || false,
            value:e.type === 'password' ? '[redacted]' : (e.value || '').slice(0,300)
        })).map(e=>({...e,name:e.name.slice(0,300)}))''')
        return dict(url=page.url, title=page.title()[:300], text=page.locator('body').inner_text(timeout=3000)[:14000],
                    controls=controls, errors=session['errors'][-30:])

    def _perform(self, pid, tid, folder, action, arguments):
        key = (pid, tid)
        if action == 'close':
            self._close_one(key)
            return {'action': action, 'closed': True}
        session = self.sessions.get(key)
        if session and session['folder'] != folder:
            self._close_one(key)
            session = None
        if action == 'open':
            path = arguments.get('path', 'index.html')
            asset = contained_asset(folder, path)
            if asset.suffix.lower() not in ('.html', '.htm'):
                raise ValueError('Open an HTML entry file')
            session = session or self._create(pid, tid, folder, arguments)
            from urllib.parse import quote
            response = session['page'].goto(session['origin']+'/'+quote(asset.relative_to(folder).as_posix(), safe='/'), wait_until='domcontentloaded')
            if response and response.status >= 400:
                raise ValueError('Project page failed to load: HTTP ' + str(response.status))
        elif not session:
            raise ValueError('Open a project page before using browser controls')
        page = session['page']
        if not same_origin(page.url, session['origin']):
            raise ValueError('Browser left the permitted project origin; close and reopen it')
        result = {'action': action}
        if action == 'click':
            if 'x' in arguments or 'y' in arguments:
                x, y = arguments.get('x'), arguments.get('y')
                if any(type(n) not in (int, float) for n in (x, y)) or not 0 <= x < session['width'] or not 0 <= y < session['height']:
                    raise ValueError('Click coordinates must lie inside the browser viewport')
                page.mouse.click(x, y)
            else:
                locator_for(page, arguments).click()
        elif action == 'fill':
            value = arguments.get('text')
            if not isinstance(value, str) or len(value) > 10000:
                raise ValueError('Fill requires text of at most 10000 characters')
            locator_for(page, arguments).fill(value)
        elif action == 'select':
            value = arguments.get('value')
            if not isinstance(value, str) or len(value) > 1000:
                raise ValueError('Select requires an option value')
            locator_for(page, arguments).select_option(value=value)
        elif action == 'scroll':
            pixels = arguments.get('pixels', 600)
            direction = arguments.get('direction', 'down')
            if type(pixels) is not int or not 1 <= pixels <= 3000 or direction not in ('up', 'down'):
                raise ValueError('Scroll direction is up/down; pixels must be 1–3000')
            page.mouse.wheel(0, pixels if direction == 'down' else -pixels)
        elif action == 'screenshot':
            target = self.store.root / 'browser' / pid / f'{tid}.png'
            target.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target), full_page=False, timeout=5000)
            result['screenshot_path'] = str(target)
        elif action == 'assert_text':
            value = arguments.get('text')
            if not isinstance(value, str) or not value or len(value) > 2000:
                raise ValueError('assert_text requires expected text of 1–2000 characters')
            result['passed'] = value in page.locator('body').inner_text(timeout=3000)
            result['expected_text'] = value
        result.update(self._observe(session))
        return result

    def _close_one(self, key):
        session = self.sessions.pop(key, None)
        if session:
            try:
                session['context'].close()
            finally:
                session['server'].shutdown()
                session['server'].server_close()
                session['serving'].join(timeout=2)

    def _close_project(self, pid):
        for key in list(self.sessions):
            if key[0] == pid:
                self._close_one(key)

    def _shutdown(self):
        for key in list(self.sessions):
            self._close_one(key)
        if self.browser:
            self.browser.close()
        if self.driver:
            self.driver.stop()
        self.browser = self.driver = None
        with self.guard:
            self.stopped = True
