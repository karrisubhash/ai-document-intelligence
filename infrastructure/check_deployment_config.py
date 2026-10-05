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
import os
import shutil
import subprocess
import sys

# Values for the variables the compose files interpolate. This check asks
# one question - does the overlay MERGE into a production stack - and the
# answer does not depend on anyone's real credentials. Supplying them here
# makes the gate hermetic: it runs identically on a laptop with a populated
# .env, on a fresh CI checkout with none, and on a machine where .env holds
# something else entirely.
#
# It is also what made the gate runnable in CI at all. `GRAFANA_PASSWORD`
# is declared `${GRAFANA_PASSWORD:?...}` - correctly, because a Grafana
# with a default password is not a monitoring stack - and `APP_PORT` has no
# default, so `docker compose config` exited 2 before reading a single
# service. The build job's "Production configuration" step therefore could
# not pass, whatever the overlay said.
#
# NOTHING HERE IS A CREDENTIAL. These strings are never written to a
# deployment; they exist so the parser has something to substitute.
PLACEHOLDERS = {
    "POSTGRES_DB": "check_db",
    "POSTGRES_USER": "check_user",
    "POSTGRES_PASSWORD": "check_password",
    "APP_PORT": "8000",
    "GRAFANA_PASSWORD": "check_password",
    "NGINX_SERVER_NAME": "example.invalid",
}

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
    # The real environment wins where it is already set, so an operator
    # checking their own stack checks their own values.
    environment = {**PLACEHOLDERS, **os.environ}

    # `env_file: .env` is required by Compose, and .env is git-ignored, so on
    # a fresh checkout `config` exited before reading a service - the
    # placeholders above only cover interpolation. Check against the example
    # file a deployment starts from, and leave no file behind.
    created = not os.path.exists(".env")
    if created:
        shutil.copyfile(".env.example", ".env")
    try:
        result = subprocess.run(
            COMPOSE, capture_output=True, text=True, env=environment
        )
    finally:
        if created:
            os.remove(".env")
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
            # tested. Read-only config and artifact mounts are fine; any
            # WRITABLE bind under /app is not - matching only /app,
            # /app/backend and /app/ai exactly let a sub-path such as
            # ./backend/documents:/app/backend/documents through.
            if target.rstrip("/") == "/app" or (
                target.startswith("/app/") and not volume.get("read_only")
            ):
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
            str(port.get("published"))
            for port in (edge.get("ports") or [])
            if port.get("published")
        }
        missing = EDGE_PORTS - published
        if missing:
            failures.append(
                f"{EDGE_SERVICE}: does not publish {sorted(missing)}; nothing would "
                f"serve traffic."
            )

    for name in CODE_SERVICES:
        service = services.get(name)
        if service is None:
            continue
        environment = service.get("environment") or {}
        if environment.get("DJANGO_DEBUG") != "False":
            failures.append(
                f"{name}: DJANGO_DEBUG is not pinned to False in production."
            )
        # Every application service needs the deployment's settings. One
        # defined only in the overlay, without `env_file`, started with no
        # secret key or database settings and restarted forever - the index
        # worker did exactly that.
        missing = [
            key
            for key in ("DJANGO_SECRET_KEY", "POSTGRES_HOST")
            if key not in environment
        ]
        if missing:
            failures.append(
                f"{name}: has no {', '.join(missing)} in its environment; it cannot "
                f"start. Give it `env_file: [.env]` like the other application services."
            )

    # The development file runs these as root for its bind mounts. That is
    # the one setting most likely to leak into production by omission, and
    # nothing about a root container looks wrong until it matters.
    for name in CODE_SERVICES:
        service = services.get(name)
        if service is None:
            continue
        user = str(service.get("user") or "").split(":")[0]
        if user in ("", "root", "0"):
            failures.append(
                f"{name}: runs as {'root' if user else 'the image default'} in "
                f"production. Pin `user: app` in docker-compose.prod.yml."
            )

    # A tmpfs with Docker's default options is world-writable only on the
    # container's FIRST start. Every restart remounts it with the mode of the
    # mount point left in the container layer - root-owned 0755 - so an
    # unprivileged service writing there passes `up` and every smoke test,
    # then crash-loops after the first reboot. That happened to the backend's
    # Prometheus directory.
    for name in CODE_SERVICES:
        service = services.get(name)
        if service is None:
            continue
        for entry in service.get("tmpfs") or []:
            target, _, options = str(entry).partition(":")
            if "uid=" not in options and "mode=" not in options:
                failures.append(
                    f"{name}: tmpfs {target} has no uid=/mode= option; it becomes "
                    f"root-owned 0755 after a restart and the unprivileged user "
                    f"can no longer write to it. Use {target}:uid=10001,gid=10001,mode=0700."
                )

    # The official nginx image renders /etc/nginx/templates only when the
    # container command is `nginx` itself. A shell wrapper here once meant
    # the project's configuration was never loaded at all: the stock default
    # server answered on :80 and nothing listened on :443.
    command = (edge or {}).get("command")
    if command:
        first = command[0] if isinstance(command, list) else str(command).split()[0]
        if first not in ("nginx", "nginx-debug"):
            failures.append(
                f"{EDGE_SERVICE}: overrides the command with {first!r}; the image "
                f"then skips its config templating. Put extra behaviour in "
                f"/docker-entrypoint.d/ instead."
            )

    # The edge must be the image that carries the built frontend, not a
    # stock nginx that would proxy every path - including the app shell -
    # to Django.
    edge_build = (edge or {}).get("build") or {}
    if "infrastructure/nginx/Dockerfile" not in str(edge_build.get("dockerfile", "")):
        failures.append(
            f"{EDGE_SERVICE}: is not built from infrastructure/nginx/Dockerfile, so "
            f"the frontend would not be served."
        )

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
    print("  - every application service has the deployment's settings, DEBUG off")
    print("  - application containers run as the unprivileged user")
    print("  - their tmpfs mounts stay writable after a restart")
    print("  - the edge is built with the frontend")


if __name__ == "__main__":
    main()
