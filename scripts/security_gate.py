"""Gate production Python and container vulnerabilities with narrow exceptions."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

_WILDCARD_MARKERS = frozenset("*?[")
_TRIVY_SEVERITIES = frozenset({"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"})
_TRIVY_IMAGE = (
    "aquasec/trivy@sha256:bcc376de8d77cfe086a917230e818dc9f8528e3c852f7b1aff648949b6258d1c"
)


def evaluate_pip_audit(report: Any) -> list[str]:
    """Return one failure for every vulnerability in a pip-audit report."""
    if not isinstance(report, Mapping):
        return ["pip-audit report must be a JSON object"]
    if "dependencies" not in report:
        return ["pip-audit report is missing dependencies"]
    dependencies = report["dependencies"]
    findings: list[str] = []
    if not isinstance(dependencies, list):
        return ["pip-audit report has an invalid dependencies field"]
    if not dependencies:
        return ["pip-audit report has no dependencies"]
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            findings.append("pip-audit report contains an invalid dependency entry")
            continue
        name = str(dependency.get("name", "unknown"))
        version = str(dependency.get("version", "unknown"))
        if "vulns" not in dependency:
            findings.append(f"pip-audit report has invalid vulnerabilities for {name}")
            continue
        vulnerabilities = dependency["vulns"]
        if not isinstance(vulnerabilities, list):
            findings.append(f"pip-audit report has invalid vulnerabilities for {name}")
            continue
        for vulnerability in vulnerabilities:
            if isinstance(vulnerability, Mapping):
                identifier = str(vulnerability.get("id", "unknown"))
            else:
                identifier = "unknown"
            findings.append(f"Python vulnerability {identifier} affects {name} {version}")
    return findings


def evaluate_trivy(
    report: Any,
    *,
    exceptions: Sequence[Mapping[str, Any]],
    today: date,
) -> list[str]:
    """Return failures for fixable or unaccepted High/Critical Trivy findings."""
    if not isinstance(report, Mapping):
        return ["Trivy report must be a JSON object"]
    if "Results" not in report:
        return ["Trivy report is missing Results"]
    results = report["Results"]
    findings: list[str] = []
    if not isinstance(results, list):
        return ["Trivy report has an invalid Results field"]
    if not results:
        return ["Trivy report has no scan results"]
    for result in results:
        if not isinstance(result, Mapping):
            findings.append("Trivy report contains an invalid result entry")
            continue
        target = str(result.get("Target", "unknown"))
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            findings.append(f"Trivy report has invalid vulnerabilities for {target}")
            continue
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, Mapping):
                findings.append(f"Trivy report contains an invalid vulnerability for {target}")
                continue
            required_fields = ("VulnerabilityID", "PkgName", "InstalledVersion", "Severity")
            if any(
                not isinstance(vulnerability.get(field), str) or not vulnerability[field].strip()
                for field in required_fields
            ):
                findings.append(f"Trivy report contains an invalid vulnerability for {target}")
                continue
            severity = vulnerability["Severity"].strip().upper()
            if severity not in _TRIVY_SEVERITIES:
                findings.append(f"Trivy report contains an invalid vulnerability for {target}")
                continue
            if severity not in {"HIGH", "CRITICAL"}:
                continue
            identifier = vulnerability["VulnerabilityID"]
            package = vulnerability["PkgName"]
            installed = vulnerability["InstalledVersion"]
            fixed_value = vulnerability.get("FixedVersion", "")
            if fixed_value is not None and not isinstance(fixed_value, str):
                findings.append(f"Trivy report contains an invalid vulnerability for {target}")
                continue
            fixed = fixed_value.strip() if isinstance(fixed_value, str) else ""
            if fixed:
                findings.append(
                    f"Fixable {severity} {identifier} affects {package} {installed}; "
                    f"upgrade to {fixed}"
                )
                continue
            exception = _find_exception(
                exceptions,
                identifier,
                package,
                installed,
                target,
                _trivy_image_digest(report),
            )
            if exception is None:
                findings.append(
                    f"Unfixed {severity} {identifier} affects {package} {installed}; "
                    "add an exact expiring exception with reason and mitigation"
                )
                continue
            findings.extend(_validate_exception(exception, identifier, package, today))
    return findings


def _find_exception(
    exceptions: Sequence[Mapping[str, Any]],
    identifier: str,
    package: str,
    installed: str,
    target: str,
    artifact_digest: str,
) -> Mapping[str, Any] | None:
    """Find an exception matching the exact candidate finding identity."""
    for exception in exceptions:
        if not isinstance(exception, Mapping):
            continue
        if (
            exception.get("id") == identifier
            and exception.get("package") == package
            and exception.get("installed_version") == installed
            and exception.get("target") == target
            and exception.get("artifact_digest") == artifact_digest
        ):
            return exception
    return None


def _validate_exception(
    exception: Mapping[str, Any], identifier: str, package: str, today: date
) -> list[str]:
    """Validate one exact unfixed vulnerability exception and its expiry."""
    errors = _exception_validation_errors(exception)
    if errors:
        return [f"Invalid exception for {identifier} {package}: {errors[0]}"]
    expires = date.fromisoformat(exception["expires"])
    if expires < today:
        return [f"Expired exception for {identifier} {package}"]
    return []


def _exception_validation_errors(exception: Mapping[str, Any]) -> list[str]:
    """Return schema errors for one exception without checking its expiry date."""
    errors: list[str] = []
    for field in (
        "id",
        "package",
        "installed_version",
        "target",
        "artifact_digest",
        "owner",
        "reason",
        "mitigation",
        "expires",
    ):
        value = exception.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} is required")
    for field in ("id", "package"):
        value = exception.get(field)
        if isinstance(value, str) and _WILDCARD_MARKERS.intersection(value):
            errors.append(f"{field} must be exact")
    expires = exception.get("expires")
    if isinstance(expires, str) and expires.strip():
        try:
            date.fromisoformat(expires)
        except ValueError:
            errors.append("expires must be an ISO date")
    artifact_digest = exception.get("artifact_digest")
    if isinstance(artifact_digest, str) and not artifact_digest.startswith("sha256:"):
        errors.append("artifact_digest must be an immutable digest")
    return errors


def load_json(path: Path) -> Any:
    """Load one JSON report without printing its contents."""
    return json.loads(path.read_text(encoding="utf-8"))


def _trivy_image_digest(report: Mapping[str, Any]) -> str:
    """Return the immutable image digest recorded in Trivy metadata."""
    metadata = report.get("Metadata")
    if not isinstance(metadata, Mapping):
        return ""
    value = metadata.get("ImageID")
    return value if isinstance(value, str) else ""


def _audit_site_packages(environment: Path) -> Path:
    """Return the installed-distribution directory inside a Python virtualenv."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return environment / "lib" / f"python{version}" / "site-packages"


def validate_trivy_report(
    report: Any,
    *,
    expected_image: str | None = None,
    expected_digest: str | None = None,
) -> None:
    """Validate scanner identity and non-empty results before evaluating findings."""
    if not isinstance(report, Mapping):
        raise ValueError("Trivy report must be a JSON object")
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise ValueError("Trivy report must contain non-empty Results")
    artifact_name = report.get("ArtifactName")
    if not isinstance(artifact_name, str) or not artifact_name:
        raise ValueError("Trivy report is missing ArtifactName")
    if expected_image is not None and artifact_name != expected_image:
        raise ValueError(
            f"Trivy report artifact {artifact_name!r} does not match {expected_image!r}"
        )
    artifact_id = report.get("ArtifactID")
    if not isinstance(artifact_id, str) or not artifact_id.startswith("sha256:"):
        raise ValueError("Trivy report is missing an immutable ArtifactID")
    metadata = report.get("Metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("Trivy report is missing image metadata")
    image_digest = _trivy_image_digest(report)
    if not image_digest.startswith("sha256:"):
        raise ValueError("Trivy report is missing an immutable image digest")
    if expected_digest is not None and image_digest != expected_digest:
        raise ValueError(
            f"Trivy report image digest {image_digest!r} does not match {expected_digest!r}"
        )
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("Trivy report contains an invalid result entry")
        for field in ("Target", "Class", "Type"):
            if not isinstance(result.get(field), str) or not result[field]:
                raise ValueError(f"Trivy report result is missing {field}")
        if not isinstance(result.get("Packages"), list):
            raise ValueError("Trivy report result is missing Packages")
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is not None and not isinstance(vulnerabilities, list):
            raise ValueError("Trivy report result has invalid Vulnerabilities")


def load_exceptions(path: Path) -> list[Mapping[str, Any]]:
    """Load and validate the exception list from a repository-owned JSON file."""
    if not path.exists():
        return []
    value = load_json(path)
    if not isinstance(value, list):
        raise ValueError("vulnerability exception manifest must be a JSON array")
    exceptions: list[Mapping[str, Any]] = []
    identities: set[tuple[str, str, str, str, str]] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ValueError(f"vulnerability exception entry {index} must be a JSON object")
        errors = _exception_validation_errors(entry)
        if errors:
            raise ValueError(f"invalid vulnerability exception entry {index}: {errors[0]}")
        identity = (
            entry["id"],
            entry["package"],
            entry["installed_version"],
            entry["target"],
            entry["artifact_digest"],
        )
        if identity in identities:
            raise ValueError(f"duplicate vulnerability exception for {identity[0]} {identity[1]}")
        identities.add(identity)
        exceptions.append(entry)
    return exceptions


def run_json_command(command: Sequence[str]) -> Any:
    """Run a security tool and parse its JSON output without invoking a shell."""
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"security command failed with exit code {result.returncode}")
    return json.loads(result.stdout)


def _docker_image_digest(image: str) -> str:
    """Return the local Docker image ID when the Docker CLI is available."""
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required to bind a candidate image digest")
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect candidate image {image!r}")
    digest = result.stdout.strip()
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"candidate image {image!r} has no immutable digest")
    return digest


def collect_pip_audit() -> Any:
    """Export the locked runtime graph and audit it with pip-audit."""
    with tempfile.TemporaryDirectory(prefix="sns-media-list-audit-") as directory:
        requirements = Path(directory) / "requirements.txt"
        audit_environment = Path(directory) / "venv"
        audit_report = Path(directory) / "pip-audit.json"
        export = subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        requirements.write_text(export.stdout, encoding="utf-8")
        subprocess.run(
            ["uv", "venv", str(audit_environment)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(audit_environment / "bin" / "python"),
                "--requirement",
                str(requirements),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            [
                "uv",
                "run",
                "pip-audit",
                "--path",
                str(_audit_site_packages(audit_environment)),
                "--format",
                "json",
                "--progress-spinner",
                "off",
                "--output",
                str(audit_report),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError(f"pip-audit failed with exit code {result.returncode}")
        return load_json(audit_report)


def collect_trivy(image: str) -> Any:
    """Scan the exact candidate image with Trivy at High/Critical severity."""
    if shutil.which("trivy"):
        command = ["trivy"]
    else:
        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            _TRIVY_IMAGE,
        ]
    command.extend(
        [
            "image",
            "--quiet",
            "--format",
            "json",
            "--severity",
            "HIGH,CRITICAL",
            image,
        ]
    )
    report = run_json_command(command)
    validate_trivy_report(
        report,
        expected_image=image,
    )
    return report


def main(arguments: Sequence[str] | None = None) -> int:
    """Run locked Python and candidate-image security gates."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pip-audit-json", type=Path)
    parser.add_argument("--trivy-json", type=Path)
    parser.add_argument("--image")
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=Path("security/vulnerability-exceptions.json"),
    )
    options = parser.parse_args(arguments)
    if options.trivy_json is None and options.image is None:
        parser.error("one of --trivy-json or --image is required")
    if options.trivy_json is not None and options.image is None:
        parser.error("--image is required when consuming a Trivy report")
    try:
        pip_report = (
            load_json(options.pip_audit_json) if options.pip_audit_json else collect_pip_audit()
        )
        trivy_report = (
            load_json(options.trivy_json) if options.trivy_json else collect_trivy(options.image)
        )
        validate_trivy_report(
            trivy_report,
            expected_image=options.image,
            expected_digest=_docker_image_digest(options.image) if options.image else None,
        )
        exceptions = load_exceptions(options.exceptions)
        findings = evaluate_pip_audit(pip_report) + evaluate_trivy(
            trivy_report,
            exceptions=exceptions,
            today=date.today(),
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"security gate could not run: {error}")
        return 2
    for finding in findings:
        print(finding)
    if findings:
        return 1
    print("security gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
