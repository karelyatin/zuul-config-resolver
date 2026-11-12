import subprocess

import pytest

from config_resolver.resolver import Resolver, _remote_project_names


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com/org/sub/repo.git",
        "ssh://git@gitlab.example.com/org/sub/repo.git",
        "git@gitlab.example.com:org/sub/repo.git",
        "gitlab.example.com:org/sub/repo",
    ],
)
def test_remote_url_project_names(url):
    assert _remote_project_names(url) == {
        "org/sub/repo",
        "gitlab.example.com/org/sub/repo",
    }


def _make_repo(path, remote):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote],
        check=True,
    )


def _write_child(repo, project="org/source"):
    (repo / ".zuul.yaml").write_text(
        "- job:\n    name: child\n    include-vars:\n"
        f"      - name: vars.yaml\n        project: {project}\n"
    )


def test_detects_remote_project_and_tracks_actual_checkout(tmp_path):
    source = tmp_path / "source-checkout"
    child = tmp_path / "child"
    _make_repo(source, "git@example.com:org/source.git")
    child.mkdir()
    (source / "vars.yaml").write_text("key: remote\n")
    _write_child(child)
    resolver = Resolver([str(source), str(child)])
    merged, sources, _ = resolver.resolve_job("child")
    assert merged["key"] == "remote"
    assert sources["key"]["selected"]["repo_dir"] == str(source)


@pytest.mark.parametrize("project", ["org/source", "example.com/org/source"])
def test_detects_zuul_checkout_layout_with_local_origin(tmp_path, project):
    source = tmp_path / "src" / "example.com" / "org" / "source"
    child = tmp_path / "child"
    _make_repo(source, "/var/lib/zuul/local-mirror")
    child.mkdir()
    (source / "vars.yaml").write_text("key: zuul\n")
    _write_child(child, project)
    merged, _, _ = Resolver([str(source), str(child)]).resolve_job("child")
    assert merged["key"] == "zuul"


def test_ambiguous_names_require_override(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    child = tmp_path / "child"
    _make_repo(first, "https://one.example.com/org/source.git")
    _make_repo(second, "https://two.example.com/org/source.git")
    child.mkdir()
    (first / "vars.yaml").write_text("key: first\n")
    (second / "vars.yaml").write_text("key: second\n")
    _write_child(child)
    repo_dirs = [str(first), str(second), str(child)]
    with pytest.raises(ValueError, match="Ambiguous checkout.*org/source"):
        Resolver(repo_dirs).resolve_job("child")
    merged, _, _ = Resolver(
        repo_dirs, project_repos={"org/source": str(second)}
    ).resolve_job("child")
    assert merged["key"] == "second"

    _write_child(child, "one.example.com/org/source")
    merged, _, _ = Resolver(repo_dirs).resolve_job("child")
    assert merged["key"] == "first"


def test_duplicate_repo_paths_do_not_create_ambiguity(tmp_path):
    source = tmp_path / "source"
    _make_repo(source, "https://example.com/org/source.git")
    (source / "vars.yaml").write_text("key: value\n")
    _write_child(source)
    merged, _, _ = Resolver([str(source), str(source / ".")]).resolve_job(
        "child"
    )
    assert merged["key"] == "value"
