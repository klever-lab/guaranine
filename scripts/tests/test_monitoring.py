"""Grafana dashboard reads must remain available during SQLite writes."""
import configparser
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class MonitoringTests(unittest.TestCase):
    def resources(self):
        return list(yaml.safe_load_all((ROOT / 'kubernetes/monitoring.yaml').read_text()))

    def test_configured_sqlite_allows_dashboard_reads_while_a_writer_is_active(self):
        config = next(resource for resource in self.resources() if resource['kind'] == 'ConfigMap'
                      and resource['metadata']['name'] == 'grafana')
        settings = configparser.ConfigParser(interpolation=None)
        settings.read_string(config['data']['grafana.ini'])
        mode = 'WAL' if settings.getboolean('database', 'wal', fallback=False) else 'DELETE'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'grafana.db'
            writer = sqlite3.connect(path, timeout=0)
            reader = sqlite3.connect(path, timeout=0)
            try:
                writer.execute('PRAGMA journal_mode=' + mode)
                writer.execute('CREATE TABLE dashboard (title TEXT)')
                writer.execute("INSERT INTO dashboard VALUES ('Saved dashboard')")
                writer.commit()
                writer.execute('BEGIN EXCLUSIVE')
                writer.execute("UPDATE dashboard SET title='New dashboard'")
                self.assertEqual(reader.execute('SELECT title FROM dashboard').fetchone()[0], 'Saved dashboard')
            finally:
                reader.close()
                writer.close()

    def test_prometheus_scrapes_symphony_metrics_with_matching_egress_policy(self):
        resources = self.resources()
        prom_config = next(r for r in resources if r['kind'] == 'ConfigMap'
                           and r['metadata']['name'] == 'prometheus')
        prom_yml = yaml.safe_load(prom_config['data']['prometheus.yml'])
        symphony_job = next(job for job in prom_yml['scrape_configs'] if job['job_name'] == 'symphony')
        self.assertEqual(
            symphony_job['static_configs'][0]['targets'],
            ['symphony.symphony.svc.cluster.local:8081'],
        )

        prom_policy = next(r for r in resources if r['kind'] == 'NetworkPolicy'
                           and r['metadata']['name'] == 'prometheus')
        expected_rule = {
            'to': [{
                'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'symphony'}},
                'podSelector': {'matchLabels': {'app': 'symphony'}},
            }],
            'ports': [{'protocol': 'TCP', 'port': 8081}],
        }
        self.assertIn(expected_rule, prom_policy['spec']['egress'])

    def test_grafana_dashboards_are_valid_json_mounted_and_include_symphony(self):
        resources = self.resources()
        grafana_cm = next(r for r in resources if r['kind'] == 'ConfigMap'
                          and r['metadata']['name'] == 'grafana')
        dashboards = {}
        for key, raw in grafana_cm['data'].items():
            if key.endswith('.json'):
                parsed = json.loads(raw)
                panel_ids = [panel['id'] for panel in parsed['panels']]
                self.assertEqual(len(panel_ids), len(set(panel_ids)), f'Duplicate panel IDs in {key}')
                dashboards[key] = parsed

        self.assertIn('symphony.json', dashboards)
        sym = dashboards['symphony.json']
        self.assertEqual(sym['uid'], 'guaranine-symphony')
        self.assertIn('guaranine', sym['tags'])
        exprs = [target.get('expr', '') for panel in sym['panels'] for target in panel.get('targets', [])]
        self.assertTrue(any('symphony_metric_available' in expr for expr in exprs))

        overview = dashboards['overview.json']
        pods_panel = next(p for p in overview['panels'] if p['title'] == 'Running pods by service')
        service_override = next(o for o in pods_panel['fieldConfig']['overrides']
                                if o['matcher']['options'] == 'Service')
        mappings = next(prop['value'][0]['options'] for prop in service_override['properties']
                        if prop['id'] == 'mappings')
        self.assertEqual(mappings['symphony'], {'text': 'Symphony'})
        services_panel = next(p for p in overview['panels'] if p['title'] == 'Services')
        self.assertIn('https://symphony.tail3a78b9.ts.net', services_panel['options']['content'])

        grafana_deploy = next(r for r in resources if r['kind'] == 'Deployment'
                              and r['metadata']['name'] == 'grafana')
        mounts = grafana_deploy['spec']['template']['spec']['containers'][0]['volumeMounts']
        self.assertTrue(any(
            m.get('subPath') == 'symphony.json'
            and m.get('mountPath') == '/etc/grafana/dashboards/symphony.json'
            for m in mounts
        ))


if __name__ == '__main__':
    unittest.main()
