import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cabin.browser import BrowserSessions, AssetHandler, contained_asset, locator_for, same_origin
from http.server import ThreadingHTTPServer


class BrowserBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'index.html').write_text('<label>Name<input aria-label="Name"></label><button>Save</button>',encoding='utf-8')

    def test_asset_paths_reject_escape_hidden_and_nonpublic_files(self):
        (self.root/'.env').write_text('private')
        (self.root/'secret.key').write_text('private')
        (self.root/'app.py').write_text('print(1)')
        self.assertEqual(self.root/'index.html',contained_asset(self.root,'index.html'))
        for path in ['../index.html','.env','secret.key','app.py','.git/config','file:///etc/passwd','C:/test.html','a\\b.html']:
            with self.subTest(path=path),self.assertRaises(ValueError): contained_asset(self.root,path)

    def test_navigation_is_confined_to_exact_origin(self):
        origin='http://127.0.0.1:1234'
        self.assertTrue(same_origin(origin+'/index.html?x=1',origin))
        for url in ['https://127.0.0.1:1234','http://127.0.0.1:1235','http://localhost:1234','https://example.com','file:///tmp/x','http://evil@127.0.0.1:1234']:
            self.assertFalse(same_origin(url,origin))

    def test_static_server_serves_only_contained_assets(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),AssetHandler); server.folder=self.root
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for route,status in [('/index.html',200),('/%2e%2e/index.html',404),('/.env',404)]:
                conn=http.client.HTTPConnection(*server.server_address)
                conn.request('GET',route); response=conn.getresponse();response.read()
                self.assertEqual(status,response.status);conn.close()
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_only_semantic_targets_are_accepted(self):
        class Page:
            def get_by_role(self,*args,**kwargs):return ('role',args,kwargs)
            def get_by_label(self,*args,**kwargs):return ('label',args,kwargs)
            def get_by_test_id(self,*args):return ('test',args)
        page=Page()
        self.assertEqual('role',locator_for(page,{'role':'button','name':'Save'})[0])
        self.assertEqual('label',locator_for(page,{'target':{'label':'Name'}})[0])
        self.assertEqual('test',locator_for(page,{'test_id':'save'})[0])
        with self.assertRaises(ValueError):locator_for(page,{'selector':'body script'})

    def test_missing_runtime_is_explicit_and_driver_closes(self):
        browser=BrowserSessions(SimpleNamespace(root=self.root))
        self.addCleanup(browser.close)
        with patch.object(browser,'capabilities',return_value={'available':False,'requirements':'Install optional runtime.'}):
            with self.assertRaisesRegex(ValueError,'Install optional runtime'):
                browser.perform('demo',1,self.root,'open',{'path':'index.html'})
        browser.close()
        self.assertTrue(browser.stopped)

    def test_calls_from_different_threads_share_one_driver_thread(self):
        browser=BrowserSessions(SimpleNamespace(root=self.root));self.addCleanup(browser.close)
        observed=[]
        with patch.object(browser,'_perform',side_effect=lambda *args: observed.append(threading.get_ident()) or {'ok':True}):
            threads=[threading.Thread(target=lambda:browser.perform('demo',1,self.root,'inspect',{})) for _ in range(3)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(5)
        self.assertEqual(3,len(observed));self.assertEqual(1,len(set(observed)))

    def test_assertion_records_actual_failure_and_fill_uses_locator(self):
        browser=BrowserSessions(SimpleNamespace(root=self.root));self.addCleanup(browser.close)
        class Locator:
            def inner_text(self,**kwargs):return 'Actual page'
            def fill(self,text):self.value=text
        body=Locator();field=Locator()
        page=SimpleNamespace(url='http://127.0.0.1:1234/index.html', locator=lambda *args:body,
                             get_by_label=lambda *args,**kwargs:field)
        browser.sessions[('demo',1)]={'folder':self.root,'page':page,'origin':'http://127.0.0.1:1234'}
        with patch.object(browser,'_observe',return_value={'text':'Actual page'}):
            result=browser._perform('demo',1,self.root,'assert_text',{'text':'Missing'})
            self.assertFalse(result['passed'])
            browser._perform('demo',1,self.root,'fill',{'label':'Name','text':'Alice'})
            self.assertEqual('Alice',field.value)
        browser.sessions.clear()

    def test_installed_browser_can_fill_click_inspect_and_capture(self):
        browser=BrowserSessions(SimpleNamespace(root=self.root));self.addCleanup(browser.close)
        if not browser.capabilities()['available']:
            self.skipTest('Optional Playwright and an installed Chromium browser are required')
        (self.root/'index.html').write_text('''<!doctype html><title>Browser fixture</title>
        <label>Name<input id="name"></label><button id="save">Save</button><p id="output"></p>
        <script>document.querySelector('#save').onclick=()=>document.querySelector('#output').textContent='Saved '+document.querySelector('#name').value;</script>''',encoding='utf-8')
        opened=browser.perform('demo',1,self.root,'open',{'path':'index.html'})
        self.assertEqual('Browser fixture',opened['title'])
        browser.perform('demo',1,self.root,'fill',{'label':'Name','text':'Alice'})
        browser.perform('demo',1,self.root,'click',{'role':'button','name':'Save'})
        self.assertTrue(browser.perform('demo',1,self.root,'assert_text',{'text':'Saved Alice'})['passed'])
        shot=browser.perform('demo',1,self.root,'screenshot',{})
        self.assertEqual(b'\x89PNG\r\n\x1a\n',Path(shot['screenshot_path']).read_bytes()[:8])


if __name__=='__main__':unittest.main()
