#!/usr/bin/env bash
# Run the platform-sensitive tests on real POSIX.
#
# The target machine is Windows with no WSL distribution, so the POSIX branches of
# ProcessManager (SIGTERM/SIGKILL to a process group) and WriteBarrier (mode bits) were
# written but never executed against a real kernel — their unit tests substitute the
# syscalls. This runs them in a Linux container instead, which is the difference between
# "should work" and "was observed to work".
#
#   bash scripts/verify_posix.sh
#
# Needs a working Docker engine. Nothing else from the project environment is used:
# dependencies are installed inside the container.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# A TLS-inspecting corporate proxy breaks pip inside the container: the host trusts the
# interception root, the base image does not. Point pip at a CA bundle instead of
# disabling verification, which would make this container fetch packages over a
# connection nobody is checking.
#   AIWO_CA_BUNDLE=/path/to/roots.pem bash scripts/verify_posix.sh
# On Windows, export the machine's trust store first; see docs/START_HERE.md.
CA_BUNDLE="${AIWO_CA_BUNDLE:-${REPO_ROOT}/.ca-bundle.pem}"
CA_ARGS=()
CA_ENV=""
if [[ -f "${CA_BUNDLE}" ]]; then
  CA_ARGS=(-v "${CA_BUNDLE}:/ca.pem:ro")
  CA_ENV="export PIP_CERT=/ca.pem SSL_CERT_FILE=/ca.pem REQUESTS_CA_BUNDLE=/ca.pem;"
  echo "Using CA bundle: ${CA_BUNDLE}"
fi

# Only the tests whose behaviour is platform-dependent. The rest are platform-neutral
# and already run on Windows; repeating them here would spend minutes proving nothing.
TESTS="${*:-tests/test_process_manager.py tests/test_workspace_guard.py tests/test_worktree_manager.py}"

echo "Running on POSIX in a container: ${TESTS}"

docker run --rm \
  -v "${REPO_ROOT}:/repo:ro" \
  "${CA_ARGS[@]}" \
  -w /work \
  python:3.12-slim \
  bash -c "
    set -euo pipefail
    ${CA_ENV}
    export DEBIAN_FRONTEND=noninteractive
    # Copy rather than mount read-write: the tests create worktrees and chmod
    # directories, and none of that should touch the host checkout.
    cp -r /repo/. /work
    apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1
    git config --global user.email t@l
    git config --global user.name t
    git config --global init.defaultBranch main
    # Only what these tests import: the modules under test are stdlib-only, so the
    # heavier runtime dependencies are not installed here.
    pip install --quiet --no-input pytest
    python -m pytest ${TESTS} -o addopts='' -p no:cacheprovider -q
  "
