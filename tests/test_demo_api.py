from fastapi.testclient import TestClient

from app.demo.main import app

client = TestClient(app)


def test_demo_health_and_auth():
    assert client.get('/health').json() == {'status': 'ok', 'mode': 'demo'}
    me = client.get('/api/v1/auth/me').json()
    assert me['user']['email'] == 'demo@analytics.local'
    assert me['organizations'][0]['role'] == 'owner'


def test_demo_analytics_query():
    response = client.post(
        '/api/v1/analytics/query',
        json={
            'project_id': 'project-demo-0001',
            'query': {
                'type': 'trend',
                'series': [{'event': 'Started Trial'}],
                'metric': 'unique_users',
                'date_range': {'preset': 'last_30_days'},
                'interval': 'day',
                'filters': [],
            },
        },
    )
    payload = response.json()
    assert payload['query_type'] == 'trend'
    assert payload['result']['series'][0]['event'] == 'Started Trial'


def test_demo_report_mutation():
    before = client.get('/api/v1/reports', params={'project_id': 'project-demo-0001'}).json()
    created = client.post(
        '/api/v1/reports',
        json={
            'project_id': 'project-demo-0001',
            'name': 'Demo report',
            'description': 'Created in test',
            'query': {'type': 'trend'},
            'visualization': {'chart': 'line'},
        },
    ).json()
    after = client.get('/api/v1/reports', params={'project_id': 'project-demo-0001'}).json()
    assert len(after) == len(before) + 1
    assert after[0]['id'] == created['id']