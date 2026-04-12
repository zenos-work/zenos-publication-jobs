import os
import sys
import types
import unittest


class _DummyResponse:
    def __init__(self, body='', status=200, ok=True):
        self.status = status
        self.ok = ok
        self._body = body

    async def text(self):
        return self._body


workers_stub = types.ModuleType('workers')
workers_stub.WorkerEntrypoint = object
workers_stub.Response = _DummyResponse


async def _default_fetch(*_args, **_kwargs):
    return _DummyResponse(status=200, ok=True)


workers_stub.fetch = _default_fetch
sys.modules['workers'] = workers_stub

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import index as jobs_index  # noqa: E402


class CourseCertificateJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_course_certificate_job_issues_missing_certificates(self):
        inserted = []

        async def fake_d1_all(_env, _sql, _params=()):
            return [
                {'enrollment_id': 'e-1', 'course_id': 'c-1', 'user_id': 'u-1'},
                {'enrollment_id': 'e-2', 'course_id': 'c-2', 'user_id': 'u-2'},
            ]

        async def fake_d1_run(_env, sql, params=()):
            inserted.append((sql, params))
            return {'ok': True}

        jobs_index._d1_all = fake_d1_all
        jobs_index._d1_run = fake_d1_run

        env = types.SimpleNamespace(COURSE_CERTIFICATE_BASE_URL='https://zenos.test/certs')
        result = await jobs_index._run_course_certificate_job(env, source='test')

        self.assertTrue(result['ok'])
        self.assertEqual(result['job'], 'course-certificates')
        self.assertEqual(result['details']['issued'], 2)
        self.assertEqual(len(inserted), 2)


if __name__ == '__main__':
    unittest.main()
