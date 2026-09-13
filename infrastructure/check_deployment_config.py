#!/usr/bin/env python3
"""Assert the production overlay actually produces a production stack.

Run from the repository root:

    python infrastructure/check_deployment_config.py

Exists because of a bug class that has now bitten this project twice, and
that no unit test can catch: **Compose MERGES sequences across files.**

A key re-declared in an overlay is appended to the base file's list, not
substituted for it. So `ports: []` does not unpublish a port, and a
`volumes:` list naming only the production mounts does not remove the
development source mount - it adds to it. Both read as obviously correct
and both did the opposite:

  * `ports: []` left the API published on 8000, reachable with no TLS, no
    edge rate limiting, and free to assert its own X-Forwarded-Proto.
  * the `volumes:` list left `./backend:/app/backend` in place, so
    production ran the working tree instead of the built image - directly
    under a comment claiming "No source bind mount: the image is the
    artifact".

The fix in both cases is the `!override` tag. This script checks the
OUTCOME rather than the tag, because the outcome is what matters and a
future refactor could achieve it another way.

It asserts against `docker compose config` - Compose's own merge, not a
reimplementation of its rules here.
"""

import json
import subprocess
import sys

COMPOSE = [
    "docker",
    "compose",
    "-f",
    "docker-compose.yml",
    "-f",
    "docker-compose.prod.yml",
    "config",
    "--format",
    "json",
]

# Services that run application code from the image.
CODE_SERVICES = ("backend", "worker", "worker-index", "migrate")

# Services that must not be reachable from outside the compose network.
INTERNAL_SERVICES = ("backend", "worker", "worker-index", "db", "redis")

# The only service allowed to publish to the host.
EDGE_SERVICE = "nginx"
EDGE_PORTS = {"80", "443"}


def load_config():
    result = subprocess.run(COMPOSE, capture_output=True, text=True)
    if result.returncode != 0:
        print("docker compose config failed:\n" + result.stderr, file=sys.stderr)
        sys.exit(2)
    return json.loads(result.stdout)


def check(config):
    failures = []
    services = config.get("services", {})

    for name in CODE_SERVICES:
        service = services.get(name)
        if service is None:
            continue
        for volume in service.get("volumes", []) or []:
            target = volume.get("target", "")
            if volume.get("type") != "bind":
                continue
            # Mounting the source tree over the image means the deploy runs
            # whatever happens to be on the host, not what was built and
            # tested. Read-only config and artifact mounts are fine.
            if target.rstrip("/") in ("/app", "/app/backend", "/app/ai"):
                failures.append(
                    f"{name}: source bind mount {volume.get('source')} -> {target} "
                    f"survives into production; the image is meant to be the artifact. "
                    f"Use `volumes: !override` in docker-compose.prod.yml."
                )

    for name in INTERNAL_SERVICES:
        service = services.get(name)
        if service is None:
            continue
        published = [
            port for port in (service.get("ports") or []) if port.get("published")
        ]
        if published:
            listed = ", ".join(str(port.get("published")) for port in published)
            failures.append(
                f"{name}: publishes port(s) {listed} to the host. Only "
                f"{EDGE_SERVICE} should be reachable. Use `ports: !override []`."
            )

    edge = services.get(EDGE_SERVICE)
    if edge is None:
        failures.append(f"{EDGE_SERVICE} service is missing from the production stack.")
    else:
        published = {
            str(port.get("published")) for port in (edge.get("ports") or []) if port.get("published")
        }
        missing = EDGE_PORTS - published
        if missing:
            failures.append(
                f"{EDGE_SERVICE}: does not publish {sorted(missing)}; nothing would "
                f"serve traffic."
            )

    backend = services.get("backend") or {}
    if backend.get("environment", {}).get("DJANGO_DEBUG") != "False":
        failures.append("backend: DJANGO_DEBUG is not pinned to False in production.")

    return failures


def main():
    failures = check(load_config())

    if failures:
        print("Production configuration check FAILED:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        sys.exit(1)

    print("Production configuration check passed:")
    print("  - no source bind mount reaches the application containers")
    print("  - only nginx is published to the host, on 80 and 443")
    print("  - DJANGO_DEBUG is pinned to False")


if __name__ == "__main__":
    main()
