"""Portable contract checks. Run against the installed wheel, not the workspace."""
from contextlib import redirect_stdout
from io import StringIO
import importlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

from papertrail.cli import activate_modules, main

LEGACY = activate_modules()


class CandidateTests(unittest.TestCase):
    def call(self, *args):
        stream = StringIO()
        with redirect_stdout(stream):
            status = main(list(args))
        return status, json.loads(stream.getvalue())

    def profile(self, workflow=None):
        # Ordinary mkdir avoids restrictive tempfile ACLs in the host sandbox.
        self.test_root = Path.cwd() / ("candidate-test-" + uuid.uuid4().hex)
        self.test_root.mkdir()
        self.assertTrue(self.test_root.resolve().is_relative_to(Path.cwd().resolve()))
        self.addCleanup(shutil.rmtree, self.test_root)
        path = self.test_root / "profile.json"
        path.write_text(json.dumps({"workflow": workflow or {}, "sources": [], "material_targets": {}}), "utf-8")
        return path

    def test_public_capabilities_have_no_personal_grant(self):
        code, result = self.call("capabilities")
        self.assertEqual(code, 0)
        self.assertEqual(result["model_authorization"]["standing"], [])
        self.assertTrue(result["configuration"]["required_for_acquire"])
        self.assertFalse(result["configuration"]["explicit_profile_defaults"]["embedding_enabled"])

    def test_offline_preview_cannot_open_network_or_process(self):
        with patch("socket.create_connection", side_effect=AssertionError("network")), patch("subprocess.run", side_effect=AssertionError("process")), patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code, result = self.call("preview", "--doi", "10.1000/example")
        self.assertEqual(code, 0)
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["credential_reads"], 0)
        self.assertFalse(result["writes"])

    def test_acquisition_requires_configuration_before_workflow(self):
        import literature_access
        with patch.object(literature_access, "main", side_effect=AssertionError("workflow")), redirect_stdout(StringIO()), patch("sys.stderr", new=StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["acquire", "--request", "missing.json", "--execute"])
        self.assertEqual(error.exception.code, 2)

    def test_public_profile_forbids_container_credential_lookup(self):
        path = self.profile({"credential_source": "legacy_container"})
        import literature_access
        with patch("subprocess.run", side_effect=AssertionError("container")), patch.object(literature_access, "main", side_effect=AssertionError("workflow")):
            code, result = self.call("acquire", "--configuration", str(path), "--request", "missing.json", "--execute")
        self.assertEqual(code, 2)
        self.assertNotIn(str(path), json.dumps(result))

    def test_configuration_inspection_does_not_create_data_or_read_keys(self):
        path = self.profile()
        import zotero_scope_processing
        with patch.object(zotero_scope_processing, "runtime_credentials", side_effect=AssertionError("credentials")):
            code, result = self.call("inspect-configuration", "--configuration", str(path))
        self.assertEqual(code, 0)
        self.assertFalse(result["embedding_enabled"])
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_acquire_preview_uses_profile_without_creating_ledger(self):
        path = self.profile()
        request = path.parent / "request.json"
        request.write_text(json.dumps({"version": 1, "paper": {"doi": "10.1000/example", "title": "Synthetic request"}}), "utf-8")
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code, result = self.call("acquire", "--configuration", str(path), "--request", str(request))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "preview")
        self.assertFalse((path.parent / "data").exists())

    def test_invalid_config_does_not_reflect_secret_value(self):
        path = self.profile({"transport_base_url": "https://secret-user:secret-password@example.org"})
        code, result = self.call("inspect-configuration", "--configuration", str(path))
        self.assertEqual(code, 2)
        self.assertNotIn("secret", json.dumps(result))

    def test_invalid_arguments_do_not_echo_credentials(self):
        stream = StringIO()
        with patch("sys.stderr", new=stream):
            with self.assertRaises(SystemExit) as error:
                main(["capabilities", "--api-key=example-secret-do-not-echo"])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn("example-secret", stream.getvalue())

    def test_demo_real_pdf_readback_and_explicit_fixture_limits(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), patch("subprocess.run", side_effect=AssertionError("process")):
            code, result = self.call("demo")
        self.assertEqual(code, 0)
        self.assertTrue(all(result["checks"].values()))
        self.assertFalse(result["scientific_evidence"])
        self.assertEqual(result["pdf_pages_read"], 1)
        self.assertEqual({c["section"] for c in result["chunks"]}, {"METHODS", "RESULTS"})
        self.assertIn("mineru", result["not_executed"])
        self.assertEqual(result["model_calls"], 0)

    def test_chunk_ids_stable_and_references_excluded(self):
        from structure_aware_chunker import chunk_document
        # Locate through the installed module, not the source/test directory.
        import papertrail
        fixture = Path(papertrail.__file__).parent / "fixtures/canonical.json"
        document = json.loads(fixture.read_text("utf-8"))
        first = chunk_document(document)
        self.assertEqual(first, chunk_document(document))
        self.assertFalse(any(c["section"] == "REFERENCES" for c in first))
        self.assertTrue(any(c["section"] == "REFERENCES" for c in chunk_document(document, include_references=True)))
        self.assertEqual({b for c in first for b in c["mineru_block_ids"]}, {"fixture-b2", "fixture-b4"})

    def test_evidence_rejects_missing_hash(self):
        from evidence_gate import main as gate
        item = {"chunk_id": "c", "document_id": "d", "title": "Synthetic", "text": "Fixture", "score": 1.0, "page_start": 0, "page_end": 0, "mineru_block_ids": ["b"]}
        result = gate("q", 200, json.dumps({"query": "q", "dataset_id": "fixture", "results": [item]}))
        self.assertFalse(result["has_evidence"])
        self.assertEqual(result["reason"], "invalid_response")

    def test_demo_rejects_modified_fixture_bytes(self):
        from papertrail.demo import run_demo
        with patch.object(Path, "read_bytes", return_value=b"tampered-fixture"):
            with self.assertRaisesRegex(ValueError, "DEMO_FIXTURE_HASH_MISMATCH"):
                run_demo()

    def test_generated_dify_nodes_have_all_source_dependencies(self):
        from build_code_nodes import build_evidence_source, build_citation_source
        for grouped in (False, True):
            namespace = {}
            exec(compile(build_evidence_source(grouped=grouped), "<generated-evidence>", "exec"), namespace)
            self.assertTrue(callable(namespace["main"]))
        for strict in (False, True):
            namespace = {}
            exec(compile(build_citation_source(enforce_literal_counts=strict), "<generated-citations>", "exec"), namespace)
            self.assertFalse(namespace["main"]("Uncited.", "[]", "") ["citation_ok"] if strict else namespace["main"]("Uncited.", "[]")["citation_ok"])

    def test_shipped_business_modules_import_without_workspace_paths(self):
        # Import every selected module, including lazy downstream paths.
        names = []
        for folder in ("Paperdownload", "RAGFlow/scripts", "DIFY"):
            for source in (LEGACY / folder).rglob("*.py"):
                parts = list(source.relative_to(LEGACY / folder).with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                if parts:
                    names.append(".".join(parts))
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), patch("subprocess.run", side_effect=AssertionError("process")):
            for name in names:
                module = importlib.import_module(name)
                self.assertTrue(Path(module.__file__).resolve().is_relative_to(LEGACY.resolve()), name)
            module = importlib.import_module("institution.acceptance_record")
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(LEGACY.resolve()))

    def test_core_dependency_installed_in_same_environment(self):
        import pypdf
        self.assertEqual(pypdf.__version__, "6.10.0")
        self.assertTrue(Path(pypdf.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))

    def test_unexported_profile_cannot_supply_private_defaults(self):
        import workflow_defaults
        import workflow_configuration
        from paper_bundle import DEFAULT_PAPER_DIRECTORY
        self.assertFalse(workflow_defaults.LOCAL_PROFILE.exists())
        self.assertEqual(workflow_defaults.LOCAL_DEFAULTS, {})
        self.assertEqual(DEFAULT_PAPER_DIRECTORY, workflow_defaults.PROJECT / 'data/papers')
        settings = workflow_configuration.workflow_settings({}, Path.cwd() / 'profile.json', legacy_defaults=True)
        self.assertFalse(settings['embedding_enabled'])
        self.assertIsNone(settings['credential_container'])
        self.assertIsNone(settings['tokenizer_json'])

    def test_source_content_checker_rejects_exposures_without_echo(self):
        import importlib.util
        checker = Path(__file__).resolve().parent.parent / 'tools/check_public_content.py'
        spec = importlib.util.spec_from_file_location('public_content_checker', checker)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = self.profile()
        fake = 'sk-' + 'A' * 30
        (path.parent / 'exposure.txt').write_text(fake, 'utf-8')
        (path.parent / '.env').write_text('EXAMPLE=fixture', 'utf-8')
        (path.parent / 'negative.txt').write_text('--api-key=example-secret-do-not-echo', 'utf-8')
        (path.parent / 'url.txt').write_text('https://example.invalid/?sig=' + 'X' * 32, 'utf-8')
        result = module.scan(path.parent)
        self.assertFalse(result['clean_under_rules'])
        self.assertEqual({r['rule'] for r in result['findings']}, {'provider_token', 'private_runtime_file', 'signed_query'})
        self.assertFalse(any(r['path'] == 'negative.txt' for r in result['findings']))
        self.assertNotIn(fake, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
