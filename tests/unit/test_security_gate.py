"""Tests for the dependency and image vulnerability release gate."""

import json
import subprocess
from datetime import date

import pytest

from scripts.security_gate import (
    _audit_site_packages,
    _docker_image_digest,
    collect_trivy,
    evaluate_pip_audit,
    evaluate_trivy,
    load_exceptions,
    run_json_command,
    validate_trivy_report,
)


def test_pip_audit_findings_always_fail() -> None:
    """Verify every known production Python vulnerability is a gate failure."""
    findings = evaluate_pip_audit(
        {
            "dependencies": [
                {
                    "name": "example",
                    "version": "1.0.0",
                    "vulns": [{"id": "CVE-2026-0001", "fix_versions": ["1.0.1"]}],
                }
            ]
        }
    )

    assert findings == ["Python vulnerability CVE-2026-0001 affects example 1.0.0"]


def test_pip_audit_report_without_dependencies_fails_closed() -> None:
    """Verify an empty Python audit response cannot pass as a clean graph."""
    assert evaluate_pip_audit({}) == ["pip-audit report is missing dependencies"]
    assert evaluate_pip_audit({"dependencies": []}) == ["pip-audit report has no dependencies"]


def test_pip_audit_dependency_without_vulnerability_list_fails_closed() -> None:
    """Verify each audited dependency has an explicit vulnerability result list."""
    assert evaluate_pip_audit({"dependencies": [{"name": "example", "version": "1.0.0"}]}) == [
        "pip-audit report has invalid vulnerabilities for example"
    ]


def test_pip_audit_target_is_the_installed_site_packages_directory(tmp_path) -> None:
    """Verify the audit scans installed distributions instead of the empty venv root."""
    assert _audit_site_packages(tmp_path).name == "site-packages"
    assert _audit_site_packages(tmp_path).parent.parent.parent == tmp_path


def test_trivy_fixed_high_finding_fails_without_exception() -> None:
    """Verify a fixable severe image finding cannot be ignored."""
    findings = evaluate_trivy(
        {
            "Results": [
                {
                    "Target": "sns-media-list:candidate",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-0002",
                            "PkgName": "libexample",
                            "InstalledVersion": "1.0.0",
                            "FixedVersion": "1.0.1",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ]
        },
        exceptions=[],
        today=date(2026, 7, 24),
    )

    assert findings == ["Fixable HIGH CVE-2026-0002 affects libexample 1.0.0; upgrade to 1.0.1"]


def test_trivy_unfixed_finding_requires_exact_expiring_exception() -> None:
    """Verify an exact complete exception permits only an unfixed severe finding."""
    finding = {
        "VulnerabilityID": "CVE-2026-0003",
        "PkgName": "ffmpeg",
        "InstalledVersion": "5.1.9",
        "FixedVersion": "",
        "Severity": "CRITICAL",
    }
    exceptions = [
        {
            "id": "CVE-2026-0003",
            "package": "ffmpeg",
            "installed_version": "5.1.9",
            "target": "image",
            "artifact_digest": "sha256:fixture",
            "owner": "service-maintainers",
            "expires": "2026-08-01",
            "reason": "No fixed package is published.",
            "mitigation": "Generated previews remain disabled.",
        }
    ]

    assert (
        evaluate_trivy(
            {
                "Metadata": {"ImageID": "sha256:fixture"},
                "ArtifactID": "sha256:fixture",
                "Results": [{"Target": "image", "Vulnerabilities": [finding]}],
            },
            exceptions=exceptions,
            today=date(2026, 7, 24),
        )
        == []
    )


def test_trivy_exception_requires_exact_installed_target_and_artifact() -> None:
    """Verify an exception cannot follow a changed package or candidate image."""
    finding = {
        "VulnerabilityID": "CVE-2026-0007",
        "PkgName": "ffmpeg",
        "InstalledVersion": "5.1.9",
        "FixedVersion": "",
        "Severity": "CRITICAL",
    }
    exception = {
        "id": "CVE-2026-0007",
        "package": "ffmpeg",
        "installed_version": "5.1.8",
        "target": "image",
        "artifact_digest": "sha256:fixture",
        "owner": "service-maintainers",
        "expires": "2026-08-01",
        "reason": "No fixed package is published.",
        "mitigation": "Generated previews remain disabled.",
    }

    findings = evaluate_trivy(
        {
            "Metadata": {"ImageID": "sha256:fixture"},
            "ArtifactID": "sha256:fixture",
            "Results": [{"Target": "image", "Vulnerabilities": [finding]}],
        },
        exceptions=[exception],
        today=date(2026, 7, 24),
    )

    assert findings == [
        "Unfixed CRITICAL CVE-2026-0007 affects ffmpeg 5.1.9; "
        "add an exact expiring exception with reason and mitigation"
    ]


@pytest.mark.parametrize(
    "exception",
    [
        {
            "id": "CVE-2026-0004",
            "package": "ffmpeg",
            "expires": "2026-07-23",
            "reason": "expired",
            "mitigation": "disabled",
        },
        {
            "id": "CVE-2026-0004",
            "package": "ffmpeg",
            "expires": "2026-08-01",
            "reason": "",
            "mitigation": "disabled",
        },
        {
            "id": "CVE-*",
            "package": "ffmpeg",
            "expires": "2026-08-01",
            "reason": "wildcard",
            "mitigation": "disabled",
        },
    ],
)
def test_trivy_rejects_expired_incomplete_or_wildcard_exceptions(exception: dict[str, str]) -> None:
    """Verify exception validation cannot become a broad permanent suppression."""
    finding = {
        "VulnerabilityID": "CVE-2026-0004",
        "PkgName": "ffmpeg",
        "InstalledVersion": "5.1.9",
        "FixedVersion": "",
        "Severity": "HIGH",
    }

    findings = evaluate_trivy(
        {"Results": [{"Target": "image", "Vulnerabilities": [finding]}]},
        exceptions=[exception],
        today=date(2026, 7, 24),
    )

    assert findings


def test_exception_manifest_rejects_non_object_entries(tmp_path) -> None:
    """Verify malformed exception manifests fail closed instead of being ignored."""
    manifest = tmp_path / "exceptions.json"
    manifest.write_text('["broad-ignore"]', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        load_exceptions(manifest)


@pytest.mark.parametrize(
    "entry",
    [
        {
            "id": "CVE-*",
            "package": "ffmpeg",
            "expires": "2026-08-01",
            "reason": "wildcard",
            "mitigation": "disabled",
        },
        {
            "id": "CVE-2026-0005",
            "package": "ffmpeg",
            "expires": "not-a-date",
            "reason": "invalid date",
            "mitigation": "disabled",
        },
        {
            "id": "CVE-2026-0005",
            "package": "ffmpeg",
            "expires": "2026-08-01",
            "reason": "",
            "mitigation": "disabled",
        },
    ],
)
def test_exception_manifest_rejects_invalid_entries(tmp_path, entry: dict[str, str]) -> None:
    """Verify every manifest entry is exact, complete, and date-valid before evaluation."""
    manifest = tmp_path / "exceptions.json"
    manifest.write_text(json.dumps([entry]), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid vulnerability exception"):
        load_exceptions(manifest)


def test_exception_manifest_rejects_duplicate_exact_entries(tmp_path) -> None:
    """Verify duplicate identities cannot create ambiguous exception ownership."""
    entry = {
        "id": "CVE-2026-0006",
        "package": "ffmpeg",
        "installed_version": "5.1.9",
        "target": "image",
        "artifact_digest": "sha256:fixture",
        "owner": "service-maintainers",
        "expires": "2026-08-01",
        "reason": "unfixed",
        "mitigation": "disabled",
    }
    manifest = tmp_path / "exceptions.json"
    manifest.write_text(json.dumps([entry, entry]), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        load_exceptions(manifest)


def test_collect_trivy_uses_pinned_container_when_binary_is_unavailable(monkeypatch) -> None:
    """Verify live scans remain reproducible when only Docker is available."""
    commands: list[list[str]] = []
    report = {
        "ArtifactName": "sns-media-list:candidate",
        "ArtifactID": "sha256:report",
        "Metadata": {"ImageID": "sha256:image"},
        "Results": [
            {
                "Target": "image",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [],
                "Vulnerabilities": [],
            }
        ],
    }

    monkeypatch.setattr("scripts.security_gate.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "scripts.security_gate.run_json_command",
        lambda command: commands.append(list(command)) or report,
    )

    assert collect_trivy("sns-media-list:candidate") == report
    assert commands == [
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "aquasec/trivy@sha256:bcc376de8d77cfe086a917230e818dc9f8528e3c852f7b1aff648949b6258d1c",
            "image",
            "--quiet",
            "--format",
            "json",
            "--severity",
            "HIGH,CRITICAL",
            "sns-media-list:candidate",
        ]
    ]


def test_trivy_report_without_results_fails_closed() -> None:
    """Verify an empty or malformed scanner response cannot pass the gate."""
    assert evaluate_trivy({}, exceptions=[], today=date(2026, 7, 24)) == [
        "Trivy report is missing Results"
    ]


def test_trivy_report_requires_complete_scan_metadata() -> None:
    """Verify a truncated result cannot pass as a clean candidate scan."""
    report = {
        "ArtifactName": "sns-media-list:candidate",
        "ArtifactID": "sha256:report",
        "Metadata": {"ImageID": "sha256:image"},
        "Results": [{"Target": "rootfs", "Class": "os-pkgs", "Type": "debian"}],
    }

    with pytest.raises(ValueError, match="Packages"):
        validate_trivy_report(report, expected_image="sns-media-list:candidate")


def test_trivy_report_binds_to_the_candidate_image_digest() -> None:
    """Verify the scanner report must describe the expected immutable image."""
    report = {
        "ArtifactName": "sns-media-list:candidate",
        "ArtifactID": "sha256:report",
        "Metadata": {"ImageID": "sha256:image"},
        "Results": [
            {
                "Target": "image",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [{"Name": "example"}],
                "Vulnerabilities": [],
            }
        ],
    }

    with pytest.raises(ValueError, match="digest"):
        validate_trivy_report(
            report,
            expected_image="sns-media-list:candidate",
            expected_digest="sha256:other",
        )


def test_trivy_report_rejects_malformed_vulnerability_severity() -> None:
    """Verify malformed vulnerability entries cannot be filtered into a pass."""
    report = {
        "ArtifactID": "sha256:report",
        "Results": [
            {
                "Target": "image",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [{"Name": "example"}],
                "Vulnerabilities": [{"Severity": None}],
            }
        ],
    }

    assert evaluate_trivy(report, exceptions=[], today=date(2026, 7, 24)) == [
        "Trivy report contains an invalid vulnerability for image"
    ]


def test_candidate_digest_inspection_fails_without_docker(monkeypatch) -> None:
    """Verify image-bound report checks cannot silently degrade without Docker."""
    monkeypatch.setattr("scripts.security_gate.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="Docker"):
        _docker_image_digest("sns-media-list:candidate")


def test_security_command_nonzero_exit_fails_closed(monkeypatch) -> None:
    """Verify a scanner process failure is not interpreted as an empty report."""
    completed = subprocess.CompletedProcess(["scanner"], 1, stdout="{}", stderr="scanner failed")
    monkeypatch.setattr("scripts.security_gate.subprocess.run", lambda *args, **kwargs: completed)

    with pytest.raises(RuntimeError, match="exit code 1"):
        run_json_command(["scanner"])
