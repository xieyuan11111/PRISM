"""Public-template hygiene checks for the Graphiti/Neo4j spike (Phase A).

The deploy templates and the spike plan must stay publishable as-is: relative
references and placeholders only, no absolute host paths, no real credentials
or personal identifiers, and the suggested service/database/ports must match
what the config and docs promise.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "deploy" / "graphiti-spike"
PLAN_DOC = REPO_ROOT / "docs" / "graphiti-spike-plan.md"

TEMPLATE_FILES = (
    TEMPLATE_DIR / "compose.yaml",
    TEMPLATE_DIR / ".env.example",
    TEMPLATE_DIR / "check_ports.py",
)

HOST_ABSOLUTE_PATTERNS = (
    # A drive letter followed by a slash, but not the 'x:/' inside a URL
    # scheme such as http:// or bolt://.
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    re.compile(r"(?m)^\s*/home/"),
    re.compile(r"(?m)^\s*/Users/"),
    re.compile(r"\\Users\\", re.IGNORECASE),
)
PERSONAL_MARKERS = ("hermes", "alice", "bob", "\\home\\", "/tmp/", "D:\\", "E:\\")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWTs
)


def test_pyproject_keeps_default_dependencies_empty_and_graphiti_opt_in():
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        pyproject = tomllib.load(handle)

    assert pyproject["project"]["dependencies"] == []
    extras = pyproject["project"]["optional-dependencies"]
    assert "graphiti" in extras
    assert extras["graphiti"] == [
        "graphiti-core==0.29.3",
        "neo4j==6.3.0",
        "httpx==0.28.1",
    ]
    # Versions are pinned only because these exact packages were exercised by
    # the isolated live spike; the extra remains opt-in.


def test_pyproject_graphiti_extra_is_documented_as_live_pinned():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "graphiti-core" in text
    assert "0.29.3" in text
    assert "6.3.0" in text
    assert "0.28.1" in text
    assert "opt-in" in text


def test_template_files_contain_no_absolute_host_paths_or_secrets():
    for path in TEMPLATE_FILES:
        text = path.read_text(encoding="utf-8")
        for pattern in HOST_ABSOLUTE_PATTERNS:
            assert not pattern.search(text), f"{path.name}: {pattern.pattern!r} found"
        for marker in PERSONAL_MARKERS:
            assert marker not in text.lower(), f"{path.name}: personal marker {marker!r}"
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"{path.name}: secret pattern found"
        # No email addresses or quoted literal passwords.
        assert "@" not in text.replace("${", ""), f"{path.name}: email-like token"


def test_env_template_holds_placeholders_only():
    env_text = (TEMPLATE_DIR / ".env.example").read_text(encoding="utf-8")
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, value = stripped.partition("=")
        assert name.startswith("PRISM_GRAPHITI_"), line
        # Values are empty, compose substitutions, clearly marked
        # placeholders, or safe default tokens (names/numbers) - never a
        # real-looking credential.
        safe_default = bool(re.fullmatch(r"[a-z0-9_.-]+", value))
        assert value == "" or "${" in value or "replace-with" in value or safe_default, line


def test_env_template_password_line_is_a_placeholder():
    env_text = (TEMPLATE_DIR / ".env.example").read_text(encoding="utf-8")
    password_line = next(
        line for line in env_text.splitlines() if line.startswith("PRISM_GRAPHITI_PASSWORD=")
    )
    assert "replace-with" in password_line


def test_env_template_database_defaults_to_community_single_database():
    env_text = (TEMPLATE_DIR / ".env.example").read_text(encoding="utf-8")
    database_line = next(
        line
        for line in env_text.splitlines()
        if line.startswith("PRISM_GRAPHITI_DATABASE=")
    )
    # Community Edition serves one built-in database; the template must not
    # presuppose a custom prism_spike database.
    assert database_line == "PRISM_GRAPHITI_DATABASE=neo4j"
    assert "prism_spike" not in env_text


def test_compose_template_never_configures_a_custom_default_database():
    compose = (TEMPLATE_DIR / "compose.yaml").read_text(encoding="utf-8")
    # Community Edition is single-database: configuring
    # server.default_database=prism_spike could prevent first start, so the
    # template must not set a custom default database at all.
    assert "prism_spike" not in compose
    assert "default_database" not in compose
    assert "NEO4J_server" not in compose
    # Database isolation is a PRISM-owned container with its own home/volume/
    # ports - never multiple database names on a shared instance.
    assert "PRISM-OWNED" in compose or "PRISM-owned" in compose
    assert "single" in compose.lower()
    assert "separate" in compose.lower() or "own" in compose.lower()


def test_compose_template_uses_suggested_names_ports_and_placeholders():
    compose = (TEMPLATE_DIR / "compose.yaml").read_text(encoding="utf-8")

    assert "prism-graphiti-spike" in compose
    assert "7475" in compose  # suggested host HTTP port
    assert "7688" in compose  # suggested host Bolt port
    assert "7474" in compose  # in-container HTTP
    assert "7687" in compose  # in-container Bolt
    assert "${PRISM_GRAPHITI_PASSWORD" in compose
    # The password must arrive through substitution, never as a literal.
    assert "neo4j/" not in compose.replace("${PRISM_GRAPHITI_PASSWORD:?", "").replace(
        "PRISM_GRAPHITI_PASSWORD is required", ""
    ) or "NEO4J_AUTH" in compose
    assert "PHASE B VERIFY" in compose
    assert "no" in compose.lower() and "guarantee" in compose.lower()


def test_check_ports_script_is_stdlib_only_and_honest_about_preflight():
    script = (TEMPLATE_DIR / "check_ports.py").read_text(encoding="utf-8")

    assert "import socket" in script
    assert "Standard library only" in script
    assert "7475" in script
    assert "7688" in script
    assert "does NOT guarantee" in script or "does not guarantee" in script


def test_spike_plan_uses_relative_references_only():
    text = PLAN_DOC.read_text(encoding="utf-8")

    for pattern in HOST_ABSOLUTE_PATTERNS:
        assert not pattern.search(text), f"plan doc: {pattern.pattern!r} found"
    for marker in PERSONAL_MARKERS:
        assert marker not in text.lower(), f"plan doc: marker {marker!r}"
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), f"plan doc: secret pattern found"
    # Repo references are relative (deploy/..., docs/..., src/..., tests/...).
    assert "deploy/graphiti-spike/" in text
    assert "docs/graphiti-spike-plan.md" in text


def test_spike_plan_does_not_presuppose_a_custom_community_database():
    text = PLAN_DOC.read_text(encoding="utf-8")
    plain = re.sub(r"\s+", " ", re.sub(r"(?m)^\s*>\s?", "", text.replace("**", "")))
    assert "prism_spike" not in text
    assert "default_database" not in text
    # The plan states where database isolation comes from (the separate
    # PRISM-owned container, not multi-database names) and that the Community
    # container serves the single built-in neo4j database.
    assert "single" in text.lower()
    assert "neo4j" in plain.lower()
    assert "isolation" in plain.lower()
    assert "separate" in plain.lower() or "own container" in plain.lower()


def test_spike_plan_covers_names_ports_preflight_side_effects_rollback_acceptance():
    text = PLAN_DOC.read_text(encoding="utf-8")

    assert "prism-graphiti-spike" in text
    assert "7475" in text and "7688" in text
    assert "preflight" in text.lower()
    assert "guarantee" in text.lower()  # ports are not guaranteed free
    assert "side effect" in text.lower()
    assert "rollback" in text.lower()
    assert "acceptance criteria" in text.lower()
    plain = re.sub(r"\s+", " ", re.sub(r"(?m)^\s*>\s?", "", text.replace("**", "")))
    assert "three opt-in integration tests" in plain
    assert "PRISM_GRAPHITI_PASSWORD" in text
    assert "PRISM_GRAPHITI_URI" in text


def test_readme_is_honest_about_real_graphiti_validation_boundary():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    # The README records the live spike result AND the remaining boundary:
    # the three live tests passed with deterministic model clients, so real
    # Neo4j/Graphiti behavior was exercised without an external provider,
    # while real-provider extraction stays a not-yet-verified item.
    assert "three live tests" in readme
    assert "not-yet-verified" in readme
    assert "embedding provider is called" in readme
    assert "deploy/graphiti-spike/" in readme
    assert "docs/graphiti-spike-plan.md" in readme
    assert "PRISM_GRAPHITI_URI" in readme
    assert "tests/test_graphiti_integration.py" in readme


def test_env_template_requires_group_equal_to_database_not_a_second_tenant():
    env_text = (TEMPLATE_DIR / ".env.example").read_text(encoding="utf-8")
    assert "PRISM_GRAPHITI_GROUP=neo4j" in env_text
    # graphiti-core 0.29.3 realises a Neo4j group as a database, so the
    # template must never suggest a group different from the Community
    # database (the old "prism-spike" group example is gone) and must say the
    # two names stay equal.
    assert "prism-spike" not in env_text
    assert "equal" in env_text.lower()


def test_no_template_or_doc_uses_prism_spike_as_a_group_id():
    # The pre-fix group id example implied a second tenant inside one
    # Community instance; templates/plan/README now use group == database ==
    # "neo4j" and must not resurrect the old group id.
    for path in (
        PLAN_DOC,
        REPO_ROOT / "README.md",
        TEMPLATE_DIR / "compose.yaml",
        TEMPLATE_DIR / ".env.example",
    ):
        text = path.read_text(encoding="utf-8")
        assert "prism-spike" not in text, path.name


def test_plan_config_example_keeps_group_equal_to_database():
    text = PLAN_DOC.read_text(encoding="utf-8")
    example = text.split('"graphiti": {', 1)[1].split("}", 1)[0]
    assert '"database": "neo4j"' in example
    assert '"group_id": "neo4j"' in example
    # The rule itself is stated: graphiti 0.29.3 treats group_id as the
    # database selection and GraphitiConfig rejects an enabled mismatch.
    assert "group_id == database" in text or "equal to it" in text


def test_readme_config_example_uses_community_group_equal_to_database():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    example = readme.split('"graphiti": {', 1)[1].split("}", 1)[0]
    assert '"database": "neo4j"' in example
    assert '"group_id": "neo4j"' in example
    plain = re.sub(r"\s+", " ", readme)
    assert "realises a Neo4j group as a database" in plain


def test_plan_does_not_promise_two_group_isolation_on_one_community_instance():
    text = PLAN_DOC.read_text(encoding="utf-8")
    # The old acceptance item claimed two groups on the shared PRISM-owned
    # instance never see each other - impossible on Community, where a group
    # is a database and only one database exists.
    assert "two groups on the shared PRISM-owned instance never" not in text
    assert "not a Community isolation mechanism" in text
    assert "PRISM-dedicated instance" in text or "instance isolation" in text
    assert "schema marker" in text.lower()
    assert "NOT an" in text  # two-group isolation is explicitly not an item


def test_no_tracked_doc_mentions_external_private_systems():
    # 开源边界: docs never reference any private memory system or personal
    # agent framework as a dependency.
    for path in (PLAN_DOC, REPO_ROOT / "README.md"):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("private memory", "openclaw", "my memory system"):
            assert forbidden not in text
