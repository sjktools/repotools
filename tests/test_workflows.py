import pytest

from repoutils import workflows


@pytest.mark.parametrize(
    ("text", "jobs"),
    [
        ("on: push\n", []),
        ("jobs: {}\n", []),
        ("jobs:\n  a:\n    environment:\n      url: x\n", [("a", workflows.UNKNOWN)]),
        ("jobs:\n  a:\n    environment: 'prod'\n  b: {}\nafter:\n  c:\n", [("a", "prod"), ("b", workflows.UNKNOWN)]),
        ("jobs:\n\n  a:\n    steps:\n      - environment: no\n", [("a", None)]),
        # Quoted keys are YAML too.
        ('jobs:\n  a:\n    environment: {"name": "staging"}\n', [("a", "staging")]),
        ("jobs:\n  a:\n    environment:\n      'name': prod\n", [("a", "prod")]),
        ("jobs:\n  a:\n    environment: {\"name': x}\n", [("a", workflows.UNKNOWN)]),
        # A quoted name may hold a comma, and a quoted job id is a job's.
        ("jobs:\n  'a':\n    environment: {name: \"prod,us\", url: x}\n", [("a", "prod,us")]),
        ("jobs:\n  \"a\":\n    environment:\n      name: 'prod,us'\n      url: x\n", [("a", "prod,us")]),
        # Quotes escaped inside a quoted name are decoded.
        ("jobs:\n  a:\n    environment: {name: 'prod''us'}\n", [("a", "prod'us")]),
        ('jobs:\n  a:\n    environment: {name: "pr\\"od", url: x}\n', [("a", 'pr"od')]),
        ("jobs:\n  a:\n    environment: 'it''s'\n", [("a", "it's")]),
        ('jobs:\n  a:\n    environment: "\\x41"\n', [("a", workflows.UNKNOWN)]),
        # An anchor or tag doesn't make a job a flow mapping, but what follows it can.
        ("jobs:\n  a: &a\n    environment: prod\n  b: !!map {}\n", [("a", "prod"), ("b", workflows.UNKNOWN)]),
        # A line that isn't a job's id starts no job.
        ("jobs:\n  - a\n  b:\n", [("b", None)]),
    ],
)
def test_jobs(text: str, jobs: list[tuple[str, str | None]]) -> None:
    lines = [workflows.without_comment(line) for line in text.splitlines()]
    assert [(job.id, job.environment) for job in workflows.jobs("w.yml", lines)] == jobs
