"""Orchestrate variable resolution for a Zuul job."""

import contextlib
import os
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from deepdiff import DeepDiff

from config_resolver.job_registry import build_registry
from config_resolver.parent_walker import resolve_parents


class Resolver:
    """Resolve Zuul job variables across one or more repo directories."""

    def __init__(self, repo_dirs, project_repos=None) -> None:
        if not isinstance(repo_dirs, list):
            raise TypeError(
                f"repo_dirs must be a list, got {type(repo_dirs).__name__}"
            )
        self.registry = build_registry(repo_dirs)
        candidates = _discover_project_repos(repo_dirs)
        self.project_repos = {
            name: paths[0]
            for name, paths in candidates.items()
            if len(paths) == 1
        }
        self.ambiguous_projects = {
            name: paths for name, paths in candidates.items() if len(paths) > 1
        }
        for name, path in (project_repos or {}).items():
            self.project_repos[name] = path
            self.ambiguous_projects.pop(name, None)

    def resolve_job(self, job_name):
        """Return (merged_vars, vars_sources, parent_chain) for a job."""
        return resolve_parents(
            job_name,
            self.registry,
            self.project_repos,
            self.ambiguous_projects,
        )


def _run_git(*args, cwd=None, timeout=30):
    """Run a git command, stripping inherited GIT_* env vars."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_")
        or k
        in {
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_EXEC_PATH",
            "GIT_SSL_CAINFO",
        }
    }
    return subprocess.run(
        ("git", *args),
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
        timeout=timeout,
        env=env,
    )


def _project_names(host, path):
    """Return qualified and unqualified project names for a remote path."""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return set()
    return {path, f"{host}/{path}"}


def _remote_project_names(remote):
    """Extract project names from network Git URLs and scp-style URLs."""
    if "://" in remote:
        parsed = urlsplit(remote)
        return _project_names(parsed.hostname, parsed.path)
    match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", remote)
    if match:
        return _project_names(match.group(1), match.group(2))
    return set()


def _discover_project_repos(repo_dirs):
    """Index checkout project names without fetching from the network."""
    candidates = {}
    seen_dirs = set()
    for repo_dir in repo_dirs:
        repo_dir = os.path.abspath(repo_dir)
        real_dir = os.path.realpath(repo_dir)
        if real_dir in seen_dirs:
            continue
        seen_dirs.add(real_dir)
        names = set()

        # Zuul checkouts normally use src/<host>/<project> regardless of
        # which URL Git has configured for the executor's local mirror.
        parts = repo_dir.split(os.sep)
        for index, part in enumerate(parts):
            if part == "src" and len(parts) >= index + 4:
                names.update(
                    _project_names(
                        parts[index + 1], "/".join(parts[index + 2 :])
                    )
                )

        if os.path.exists(os.path.join(repo_dir, ".git")):
            try:
                remote = _run_git("remote", "get-url", "origin", cwd=repo_dir)
            except (FileNotFoundError, subprocess.SubprocessError):
                pass
            else:
                names.update(_remote_project_names(remote.stdout.strip()))

        for name in names:
            candidates.setdefault(name, []).append(repo_dir)
    return candidates


def _diff_registries(current_resolver, ref_resolver):
    """Compute per-job variable diffs between two resolvers."""
    all_jobs = sorted(
        set(current_resolver.registry) | set(ref_resolver.registry)
    )
    result = {}

    for job_name in all_jobs:
        in_current = job_name in current_resolver.registry
        in_ref = job_name in ref_resolver.registry

        if not in_current:
            ref_vars, _, _ = ref_resolver.resolve_job(job_name)
            result[job_name] = {"status": "removed", "vars": ref_vars}
            continue

        if not in_ref:
            cur_vars, _, _ = current_resolver.resolve_job(job_name)
            result[job_name] = {"status": "added", "vars": cur_vars}
            continue

        cur_vars, _, _ = current_resolver.resolve_job(job_name)
        ref_vars, _, _ = ref_resolver.resolve_job(job_name)
        diff = DeepDiff(ref_vars, cur_vars, ignore_order=True)
        if diff:
            result[job_name] = {
                "status": "changed",
                "diff": diff.to_dict(),
            }

    return result


def compute_diff(repo_dir, ref, project_repos=None):
    """Compare resolved variables between the working tree and a ref.

    Creates a temporary git worktree at the given ref, resolves every
    job on both sides, and returns a per-job diff of the results.
    """
    project_repos = project_repos or {}
    current_resolver = Resolver([repo_dir], project_repos=project_repos)

    with tempfile.TemporaryDirectory() as tmp_dir:
        worktree_path = os.path.join(tmp_dir, "worktree")
        _run_git(
            "worktree",
            "add",
            "--detach",
            worktree_path,
            ref,
            cwd=repo_dir,
        )
        try:
            ref_project_repos = {
                project: (
                    worktree_path
                    if os.path.realpath(path) == os.path.realpath(repo_dir)
                    else path
                )
                for project, path in current_resolver.project_repos.items()
            }
            ref_resolver = Resolver(
                [worktree_path], project_repos=ref_project_repos
            )
            result = _diff_registries(current_resolver, ref_resolver)
        finally:
            with contextlib.suppress(subprocess.CalledProcessError):
                _run_git(
                    "worktree",
                    "remove",
                    "--force",
                    worktree_path,
                    cwd=repo_dir,
                )

    return result
