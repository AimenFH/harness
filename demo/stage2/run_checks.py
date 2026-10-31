"""Trusted container-side runner. This file is mounted read-only at /protected."""

import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

os.environ.pop('PYTEST_ADDOPTS', None)
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
import pytest  # Import the installed runner before exposing target modules.

sys.path.insert(0, '/work')
suite = sys.argv[1]
if suite not in ('acceptance', 'regression'):
    raise SystemExit('unknown protected suite')
root = Path('/protected') / suite
paths = ([str(root / 'acceptance_expiry.py')] if suite == 'acceptance'
         else [str(root / 'tests/unit'), str(root / 'tests/integration')])
junit = Path('/tmp') / f'{suite}.xml'
code = pytest.main(['-c', '/dev/null', '--rootdir', str(root), '--confcutdir', str(root),
                    '--import-mode=importlib', '-p', 'no:cacheprovider',
                    '--junitxml', str(junit), '-q', '--tb=short', *paths])
report = {'suite': suite, 'exit_code': int(code), 'tests': 0, 'passed': 0,
          'failed': 0, 'errors': 0, 'skipped': 0, 'cases': []}
if junit.exists():
    for case in ET.parse(junit).iter('testcase'):
        result = 'passed'
        message = ''
        for tag, outcome in [('failure', 'failed'), ('error', 'errors'), ('skipped', 'skipped')]:
            item = case.find(tag)
            if item is not None:
                result = outcome
                message = item.text or item.get('message', '')
                break
        report['tests'] += 1
        report[result] += 1
        report['cases'].append({'name': case.get('name'), 'result': result, 'detail': message})
else:
    report['errors'] += 1
    report['missing_junit'] = True
print('HARNESS_STAGE2_REPORT=' + json.dumps(report, ensure_ascii=True), flush=True)
raise SystemExit(int(code))
