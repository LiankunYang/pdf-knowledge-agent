"""用合成PDF检查保存规则，不请求模型。"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_maintenance as km
from knowledge_agent import load_catalog


def make_pdf(path, extra=False, changed=False):
    text = 'Issuer Alpha\nCertificate DOC-001\nHeat BATCH-001\nPressure 11 bar' if changed else 'Issuer Alpha\nCertificate DOC-001\nHeat BATCH-001\nPressure 10 bar'
    if extra:
        text += '\nTemperature 20 C'
    doc = pymupdf.open()
    doc.new_page().insert_text((40, 60), text)
    doc.save(path)
    doc.close()


class SaveRules(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.library = self.folder / 'library'
        for name in ['', 'pages', 'sources']:
            (self.library / name).mkdir(parents=True, exist_ok=True)
        self.env = patch('dotenv.dotenv_values', return_value={'DEEPSEEK_API_KEY': 'offline-test-placeholder'})
        self.model = patch('langchain_deepseek.ChatDeepSeek')
        self.env.start()
        self.mock_model = self.model.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.model.stop)
        self.addCleanup(self.temp.cleanup)

    def session(self, name, **options):
        path = self.folder / name
        make_pdf(path, **options)
        session = km.MaintenanceSession(str(path), self.library)
        session.read_pdf()
        session.search_knowledge('DOC-001', '查找同一证书')
        return session

    def commit(self, session, action='new', target='', extra=False, changed=False, identity=None, omit_pressure=False):
        facts = [{'key': 'certificate', 'value': 'DOC-001', 'pdf_page': 1, 'quote': 'Certificate DOC-001'}]
        if not omit_pressure:
            value = '11 bar' if changed else '10 bar'
            facts.append({'key': 'pressure', 'value': value, 'pdf_page': 1, 'quote': 'Pressure ' + value})
        if extra:
            facts.append({'key': 'temperature', 'value': '20 C', 'pdf_page': 1, 'quote': 'Temperature 20 C'})
        body = '# 合成证书知识页\n\n## 已提取信息 [PDF第1页]\n\n' + ('记录出具方、证书号、批次、压力和适用条件。' * 20)
        return json.loads(session.commit_page(
            action=action, target_page_id=target,
            identity=identity or {'issuer': 'Issuer Alpha', 'document_ids': ['DOC-001'], 'object_ids': ['BATCH-001']},
            facts=facts, title='合成证书', summary='离线检查样本', category='test',
            report_markdown=body, used_pages=[1],
            source_quotes=[{'pdf_page': 1, 'quote': 'Issuer Alpha'}, {'pdf_page': 1, 'quote': 'Certificate DOC-001'}],
            related_page_ids=[], decision_reason='合成PDF保存规则检查', change_summary=['保留来源与事实']
        ))

    def test_new_update_history_and_duplicate(self):
        first = self.session('one.pdf')
        created = self.commit(first)
        self.assertEqual(created['status'], 'new')
        page_id = created['page_id']
        original = (self.library / 'pages' / (page_id + '.md')).read_bytes()
        full = self.session('full.pdf', extra=True)
        full.read_library_page(page_id, '更新前核对旧事实')
        updated = self.commit(full, 'update', page_id, extra=True)
        self.assertEqual(updated['status'], 'update')
        self.assertEqual(updated['revision'], 2)
        self.assertEqual((self.library / 'history' / page_id / 'v001.original.md').read_bytes(), original)
        metadata = json.loads((self.library / 'pages' / (page_id + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(len(metadata['facts']), 3)
        with patch.object(km, 'create_deep_agent', side_effect=AssertionError('Duplicate must not create an Agent')):
            duplicate = km.run_maintenance(str(first.source), 'changed task', self.library)
        self.assertEqual(duplicate['status'], 'duplicate_reused')
        self.assertFalse(duplicate['actual_cloud_run'])
        self.assertEqual(load_catalog(self.library)['pages'][0]['revision'], 2)

    def test_conflict_keeps_existing_page(self):
        old = self.session('original.pdf')
        created = self.commit(old)
        page_id = created['page_id']
        page = self.library / 'pages' / (page_id + '.md')
        digest = hashlib.sha256(page.read_bytes()).hexdigest()
        changed = self.session('changed.pdf', changed=True)
        changed.read_library_page(page_id, '核对旧值')
        result = self.commit(changed, 'update', page_id, changed=True)
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertEqual(hashlib.sha256(page.read_bytes()).hexdigest(), digest)
        self.assertTrue(Path(result['markdown']).exists())

    def test_identity_and_missing_old_fact_rejected(self):
        old = self.session('old.pdf')
        page_id = self.commit(old)['page_id']
        new = self.session('new.pdf', extra=True)
        new.read_library_page(page_id, '核对旧资料')
        mismatch = self.commit(new, 'update', page_id, extra=True,
            identity={'issuer': 'Different Issuer', 'document_ids': ['DOC-001'], 'object_ids': ['BATCH-001']})
        self.assertEqual(mismatch['status'], 'not_saved')
        missing = self.commit(new, 'update', page_id, extra=True, omit_pressure=True)
        self.assertEqual(missing['status'], 'needs_confirmation')
        self.assertEqual(load_catalog(self.library)['pages'][0]['revision'], 1)

    def test_invalid_quote_rejected(self):
        session = self.session('badquote.pdf')
        session.read_pages[1]['plain_text'] = 'no matching evidence'
        session.read_pages[1]['layout_text'] = 'no matching evidence'
        result = self.commit(session)
        self.assertEqual(result['status'], 'not_saved')
        self.assertEqual(load_catalog(self.library)['pages'], [])


if __name__ == '__main__':
    unittest.main()
