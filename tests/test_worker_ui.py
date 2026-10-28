import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from cabin.engine import Engine, git
from cabin.store import Store, now


class Model:
    def __init__(self, actions): self.actions=iter(actions)
    def generate(self,*args,**kwargs): return json.dumps(next(self.actions)),1


def action(kind,path='',content='',summary='Done'):
    return dict(action=kind,path=path,content=content,summary=summary)


@unittest.skipUnless(shutil.which('git'),'Git required')
class WorkerUiTests(unittest.TestCase):
    def test_failed_ui_assertion_blocks_finish_across_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            store.execute('INSERT INTO projects(id,name,brief,status,created,checks,max_steps,max_attempts,model) VALUES (?,?,?,?,?,?,?,?,?)',
                ('demo','Demo','UI acceptance','running',now(),json.dumps([[sys.executable,'-c','pass']]),2,2,'fake'))
            tid=store.execute('INSERT INTO tasks(project_id,title,description,created) VALUES (?,?,?,?)',('demo','UI','Saved must appear',now()))
            folder=store.folder('demo');folder.mkdir();(folder/'index.html').write_text('<button>Save</button>')
            git(folder,'init');git(folder,'add','-A');git(folder,'commit','-m','Baseline')
            engine=Engine(store)
            class Browser:
                def perform(self,*args):return dict(passed=False,expected_text='Saved',text='Save')
            engine.browser=Browser()
            engine.ollama=Model([action('browser','assert_text',json.dumps({'text':'Saved'})),action('finish')])
            task=store.rows('SELECT * FROM tasks WHERE id=?',(tid,))[0]
            engine._work(store.project('demo'),task,folder,autonomous=True)
            task=store.rows('SELECT * FROM tasks WHERE id=?',(tid,))[0]
            self.assertNotEqual('verified',task['status'])
            self.assertIn('Saved',json.loads(task['ui_failures']))
            engine.ollama=Model([action('finish'),action('finish')])
            engine._work(store.project('demo'),task,folder,autonomous=True)
            task=store.rows('SELECT * FROM tasks WHERE id=?',(tid,))[0]
            self.assertEqual('blocked',task['status'])
            history=store.rows("SELECT * FROM execution_history WHERE task_id=? AND kind='tool'",(tid,))
            self.assertTrue(any(json.loads(r['payload'])['action']=='browser' for r in history))


if __name__=='__main__':unittest.main()
