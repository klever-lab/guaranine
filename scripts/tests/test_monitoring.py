"""Grafana dashboard reads must remain available during SQLite writes."""
import configparser
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


if __name__ == '__main__':
    unittest.main()
