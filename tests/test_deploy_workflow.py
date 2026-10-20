"""Offline workflow wiring, pinned source, permissions and repository variables."""

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
VARIABLES = {'CLOUD_ID', 'FOLDER_ID', 'SA_ID', 'FUNCTION_ID', 'FUNCTION_SA_ID', 'BUCKET',
             'LOCKBOX_SECRET_ID', 'LOCKBOX_VERSION_ID', 'YC_OIDC_AUDIENCE', 'ZONT_CONTACT'}


class DeployWorkflowTests(unittest.TestCase):
    def test_repository_variables_use_allowed_names_and_preserve_helper_audience(self):
        workflow = (ROOT / '.github/workflows/deploy-prod.yml').read_text()
        variables = set(re.findall(r'\$\{\{ vars\.([A-Z_]+) \}\}', workflow))
        self.assertEqual(variables, VARIABLES)
        self.assertFalse(any(name.startswith('GITHUB_') for name in variables))
        self.assertIn('GITHUB_OIDC_AUDIENCE: ${{ vars.YC_OIDC_AUDIENCE }}', workflow)

    def test_workflow_only_deploys_checked_master_with_minimal_permissions(self):
        workflow = (ROOT / '.github/workflows/deploy-prod.yml').read_text()
        for value in ('workflows: [Project checks]', "github.repository == 'gorpsys/zontcontrol'",
                      "github.ref == 'refs/heads/master'", 'permissions: {}',
                      'cancel-in-progress: false', 'queue: max', 'id-token: write',
                      'ref: ${{ needs.gate.outputs.sha }}', 'ref: ${{ github.sha }}',
                      'CLOUD_PACKAGE_SHA: ${{ needs.gate.outputs.sha }}',
                      'persist-credentials: false', 'YC_CLI_VERSION: 1.40.0',
                      'python3 -m scripts.yandex_deploy', 'make yandex-archive',
                      'FUNCTION_TIMEOUT: 420s', "FUNCTION_CONCURRENCY: '1'",
                      'LOCKBOX_KEY: z_token', 'LOCKBOX_ENVIRONMENT_VARIABLE: ZONT_TOKEN'):
            self.assertIn(value, workflow)
        for variable in VARIABLES:
            self.assertIn('${{ vars.' + variable + ' }}', workflow)
        gate = workflow.split('  deploy:', 1)[0]
        self.assertNotIn('id-token:', gate)
        for forbidden in ('environment:', 'secrets.', 'Sandbox', '--async-max-retries',
                          'trigger create', 'd4e6d0l53hkivgrmfano', 'ajela8pd8l3bjeupa04s'):
            self.assertNotIn(forbidden, workflow)

    def test_example_matches_the_single_workflow_configuration_contract(self):
        data = json.loads((ROOT / 'deploy/yandex.example.json').read_text())
        self.assertEqual(set(data['repository_variables']), VARIABLES)
        self.assertEqual(data['repository_variables']['FUNCTION_ID'], 'd4e9enfbuhk2h59a0fj0')
        self.assertEqual(data['repository_variables']['SA_ID'], 'aje715nu0u832o93e0hd')
        self.assertEqual(data['repository_variables']['LOCKBOX_SECRET_ID'], 'e6q7k3r798m1od3jc647')
        self.assertEqual(data['repository_variables']['LOCKBOX_VERSION_ID'], 'e6qi0n3ucqk2stmtdalb')
        self.assertEqual(data['fixed']['FUNCTION_TIMEOUT'], '420s')
        self.assertEqual(data['fixed']['FUNCTION_CONCURRENCY'], '1')
        self.assertEqual(data['fixed']['LOCKBOX_KEY'], 'z_token')
