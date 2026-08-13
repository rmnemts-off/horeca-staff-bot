"""What the container entry point refuses to start (TZ 9: credentials from the environment).

`docker/entrypoint.sh` is executed with `/bin/sh` instead of being read as text: a guard
asserted by grepping for its own source line proves only that the line is still there.
Nothing is actually started — `RUN_MIGRATIONS=0` and no command, so the script walks its
checks and reaches its own `exit`. No Docker, no database.

The rule under test: `barpoint` is the password fallback of `docker-compose.yml`, present
so that `docker compose up` works on a fresh clone. In production it must be a startup
failure with a readable reason, not a stand that comes up quietly on a password anyone can
read in the repository.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.repo_scan import REPO_ROOT

ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"

#: The fallback of docker-compose.yml. Spelled out here so that changing it in one place
#: and not the other shows up as a failing test rather than as a silent hole. Not a
#: secret: it is the value production is checked *against* and refuses to run on.
LOCAL_STAND_PASSWORD = "barpoint"  # noqa: S105 - the known-bad default, not a credential

#: A minimal environment: no inherited DATABASE_URL, no inherited APP_ENV.
BASE_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "RUN_MIGRATIONS": "0"}

POSTGRES_PARTS = {
    "POSTGRES_USER": "barpoint",
    "POSTGRES_DB": "barpoint",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
}


def run_entrypoint(**environment: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, a shell script of this repository
        ["/bin/sh", str(ENTRYPOINT)],
        env={**BASE_ENVIRONMENT, **environment},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_production_refuses_the_local_stand_password() -> None:
    result = run_entrypoint(
        APP_ENV="prod",
        POSTGRES_PASSWORD=LOCAL_STAND_PASSWORD,
        **POSTGRES_PARTS,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "prod" in result.stderr and "password" in result.stderr, result.stderr


def test_production_refuses_the_local_stand_password_inside_database_url() -> None:
    """A ready-made DSN takes priority over the POSTGRES_* parts, so it is checked too."""
    result = run_entrypoint(
        APP_ENV="prod",
        DATABASE_URL=f"postgresql+asyncpg://barpoint:{LOCAL_STAND_PASSWORD}@db:5432/barpoint",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "DATABASE_URL" in result.stderr, result.stderr


def test_production_starts_with_a_real_password() -> None:
    result = run_entrypoint(
        APP_ENV="prod",
        DATABASE_URL="postgresql+asyncpg://barpoint:not-the-default@db:5432/barpoint",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_the_local_stand_is_left_alone(app_env: str) -> None:
    """The guard fires in production only; a guard that fires everywhere gets removed."""
    result = run_entrypoint(
        APP_ENV=app_env,
        POSTGRES_PASSWORD=LOCAL_STAND_PASSWORD,
        **POSTGRES_PARTS,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_default_password_is_still_the_one_compose_falls_back_to() -> None:
    """If the fallback in compose changes, this test is the reminder to change the guard."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    assert f"POSTGRES_PASSWORD:-{LOCAL_STAND_PASSWORD}" in compose
    assert f'LOCAL_STAND_PASSWORD="{LOCAL_STAND_PASSWORD}"' in entrypoint
