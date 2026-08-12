import os

import pytest

REQUIRED_ENV = ["DIGIEXPERT_API_KEY"]


@pytest.fixture(scope="session", autouse=True)
def _check_env():
    """Fail fast with one clear message instead of N confusing per-test
    failures if the environment isn't configured."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        pytest.exit(f"Missing required environment variables: {', '.join(missing)}", returncode=2)