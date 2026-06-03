import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None


ROOT = Path(__file__).resolve().parent
LOCAL_ENV = ROOT / ".deploy.local.env"


def load_env_file(path):
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def git(*args):
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {message}")
    return result.stdout.strip()


def verify_local_git(branch):
    status = git("status", "--porcelain")
    if status:
        raise RuntimeError("Working tree is not clean. Commit or stash changes before deploying.")

    current_branch = git("branch", "--show-current")
    if current_branch != branch:
        raise RuntimeError(f"Current branch is {current_branch!r}, expected {branch!r}.")

    git("fetch", "origin", branch)
    head = git("rev-parse", "HEAD")
    remote = git("rev-parse", f"origin/{branch}")
    if head != remote:
        raise RuntimeError(f"Local HEAD {head} does not match origin/{branch} {remote}. Push first.")
    return head


def quote(value):
    return shlex.quote(value)


def remote_commands(project_dir, repo_url, branch):
    project = quote(project_dir)
    repo = quote(repo_url)
    branch_q = quote(branch)
    return [
        f"mkdir -p {project}",
        f"cd {project} && if [ ! -d .git ]; then git init && git remote add origin {repo}; fi",
        f"cd {project} && git remote set-url origin {repo}",
        f"cd {project} && git fetch origin {branch_q}",
        f"cd {project} && git checkout -B {branch_q} origin/{branch_q}",
        f"cd {project} && git reset --hard origin/{branch_q}",
        f"cd {project} && git rev-parse HEAD",
    ]


def connect_ssh(host, user, password, key_path):
    if paramiko is None:
        raise RuntimeError("paramiko is not installed. Install it or use the existing Python environment that has it.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs = {"hostname": host, "username": user, "timeout": 30}
    if key_path:
        kwargs["key_filename"] = key_path
    elif password:
        kwargs["password"] = password
    else:
        raise RuntimeError("Set FRAU_DEPLOY_PASSWORD or FRAU_DEPLOY_KEY_PATH.")

    client.connect(**kwargs)
    return client


def run_remote(client, commands):
    deployed_commit = None
    for command in commands:
        print(f"remote$ {command}")
        _stdin, stdout, stderr = client.exec_command(command, get_pty=True)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        error = stderr.read().decode("utf-8", errors="replace").strip()
        exit_status = stdout.channel.recv_exit_status()
        if output:
            print(output)
        if exit_status != 0:
            raise RuntimeError(error or f"Remote command failed with exit code {exit_status}")
        if command.endswith("git rev-parse HEAD"):
            deployed_commit = output.splitlines()[-1].strip()
    return deployed_commit


def main():
    parser = argparse.ArgumentParser(description="Deploy the Frau Waffel static site safely.")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and print remote commands without connecting.")
    args = parser.parse_args()

    load_env_file(LOCAL_ENV)

    branch = env("FRAU_DEPLOY_BRANCH", "main")
    host = env("FRAU_DEPLOY_HOST", required=True)
    user = env("FRAU_DEPLOY_USER", required=True)
    password = env("FRAU_DEPLOY_PASSWORD")
    key_path = env("FRAU_DEPLOY_KEY_PATH")
    project_dir = env("FRAU_DEPLOY_PATH", required=True)
    repo_url = env("FRAU_DEPLOY_REPO", "https://github.com/primocreatus/frauwaffel.git")

    local_commit = verify_local_git(branch)
    commands = remote_commands(project_dir, repo_url, branch)

    print(f"Local commit verified: {local_commit}")
    print(f"Target: {user}@{host}:{project_dir}")
    print(f"Branch: {branch}")

    if args.dry_run:
        print("Dry run only. Remote commands:")
        for command in commands:
            print(f"  {command}")
        return 0

    client = connect_ssh(host, user, password, key_path)
    try:
        deployed_commit = run_remote(client, commands)
    finally:
        client.close()

    if deployed_commit != local_commit:
        raise RuntimeError(f"Deployed commit {deployed_commit} does not match local commit {local_commit}.")

    print(f"Deployment complete: {deployed_commit}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
