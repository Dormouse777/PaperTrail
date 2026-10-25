"""Small process-local bridge; business modules remain in the main workspace."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "PUBLIC_ARGUMENTS_INVALID\n")


def activate_modules():
    # Keep the legacy relative layout intact, including source-inlining assets.
    # Installed commands run in a dedicated process. This is not an embedding API.
    root = Path(__file__).resolve().parent / "_legacy"
    paths = (root / "Paperdownload", root / "RAGFlow/scripts", root / "DIFY", root)
    if any(not path.is_dir() for path in paths):
        raise RuntimeError("PACKAGE_CONTENT_INCOMPLETE")
    for path in reversed(paths):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    return root


def _public_configuration(path):
    from workflow_configuration import read_configuration, workflow_settings
    config = read_configuration(path)
    settings = workflow_settings(config, path)
    # A public candidate never inherits or selects the private Docker identity.
    if settings["credential_container"] is not None:
        raise ValueError("PUBLIC_PROFILE_REQUIRES_ENVIRONMENT_CREDENTIALS")
    return config, settings


def main(argv=None):
    parser = QuietParser(description="Literature workflow local candidate; no services start automatically.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="Describe effects and validation limits")
    commands.add_parser("demo", help="Offline synthetic fixture: chunks, evidence and citations")
    preview = commands.add_parser("preview", help="Offline DOI routing; no downloads or credentials")
    preview.add_argument("--doi", required=True)
    preview.add_argument("--metadata-file", type=Path)
    inspect = commands.add_parser("inspect-configuration", help="Validate settings without reading credentials or writing data")
    inspect.add_argument("--configuration", type=Path, required=True)
    acquire = commands.add_parser("acquire", help="Existing coordinator; explicit configuration required, preview by default")
    acquire.add_argument("--request", type=Path, required=True)
    acquire.add_argument("--configuration", type=Path, required=True)
    acquire.add_argument("--download-dir", type=Path)
    for flag in ("execute", "confirm-download", "confirm-publisher", "noncommercial-research", "download-only", "no-index"):
        acquire.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        root = activate_modules()
        if args.command == "demo":
            from .demo import run_demo
            result = run_demo()
        elif args.command == "inspect-configuration":
            _, settings = _public_configuration(args.configuration)
            result = {"valid": True, "embedding_enabled": settings["embedding_enabled"],
                      "credential_source": "environment", "path_base": "configuration_directory",
                      "network_requests": 0, "credential_reads": 0, "writes": False}
        elif args.command == "capabilities":
            from literature_access import capabilities
            result = capabilities(json.loads((root / "Paperdownload/publisher_registry.json").read_text("utf-8")))
            result["model_authorization"].update(standing=[], standing_scope="none_in_public_cli")
            result["configuration"].update(required_for_acquire=True, credential_source="environment_only")
            result["candidate_validation"] = {"full_service_chain": "not_verified_in_clean_environment",
                "demo": "synthetic_fixture_only", "installed_surface": "command_line_only",
                "mcp_and_deployment": "not_in_this_distribution"}
        else:
            from literature_access import main as workflow_main
            if args.command == "preview":
                forwarded = ["preview", "--doi", args.doi]
                if args.metadata_file:
                    forwarded += ["--metadata-file", str(args.metadata_file)]
            else:
                _public_configuration(args.configuration)
                forwarded = ["acquire", "--request", str(args.request), "--configuration", str(args.configuration)]
                if args.download_dir:
                    forwarded += ["--download-dir", str(args.download_dir)]
                for flag in ("execute", "confirm_download", "confirm_publisher", "noncommercial_research", "download_only", "no_index"):
                    if getattr(args, flag):
                        forwarded.append("--" + flag.replace("_", "-"))
            return workflow_main(forwarded)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError):
        # Do not reflect configuration paths, credentials or upstream exceptions.
        print(json.dumps({"status": "stopped", "reason_code": "PUBLIC_INPUT_OR_CONFIGURATION_INVALID"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
