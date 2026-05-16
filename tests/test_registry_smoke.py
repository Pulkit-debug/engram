"""Quick sanity check on the extractor registry."""

from pathlib import Path

from engram.extractors.base import get_extractor


def test_registry_resolves_known_files():
    cases = [
        ("Dockerfile", "DockerfileExtractor"),
        ("Dockerfile.prod", "DockerfileExtractor"),
        ("docker-compose.yml", "DockerComposeExtractor"),
        ("compose.yaml", "DockerComposeExtractor"),
        ("Chart.yaml", "HelmChartExtractor"),
        ("values.yaml", "HelmValuesExtractor"),
        ("values-prod.yaml", "HelmValuesExtractor"),
        ("Jenkinsfile", "JenkinsfileExtractor"),
        ("Jenkinsfile.prod", "JenkinsfileExtractor"),
        ("Makefile", "MakefileExtractor"),
        ("deploy.sh", "ShellExtractor"),
        ("script.bash", "ShellExtractor"),
        (".env", "EnvFileExtractor"),
        (".env.production", "EnvFileExtractor"),
        ("app.py", "PythonExtractor"),
        ("index.ts", "JsTsExtractor"),
        ("index.tsx", "JsTsExtractor"),
        ("server.js", "JsTsExtractor"),
        ("main.tf", "TerraformExtractor"),
        ("variables.tfvars", "TerraformExtractor"),
        ("k8s/deployment.yaml", "YAMLExtractor"),
    ]
    failures = []
    for filename, expected in cases:
        got = get_extractor(Path(filename))
        actual = type(got).__name__ if got else "None"
        if actual != expected:
            failures.append(f"{filename}: expected {expected}, got {actual}")
    assert not failures, "\n".join(failures)


def test_unknown_file_returns_none():
    assert get_extractor(Path("random.xyz")) is None
    assert get_extractor(Path("photo.png")) is None
