# Presence of this file puts the repository root on sys.path for pytest.
import pytest

# Enable asyncio mode for pytest-asyncio
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark test as async"
    )
