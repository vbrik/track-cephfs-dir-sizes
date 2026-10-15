import pytest

from trackdir.safeopen import SafeRoot, openat2_supported


@pytest.fixture(params=[True, False], ids=["openat2", "fallback"])
def use_openat2(request) -> bool:
    """Run a test once with openat2 and once with the per-component fallback."""
    if request.param and not openat2_supported():
        pytest.skip("this kernel lacks openat2")
    return request.param


@pytest.fixture
def safe_root(tmp_path, use_openat2):
    with SafeRoot(tmp_path, use_openat2=use_openat2) as root:
        yield root
