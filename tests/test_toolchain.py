"""Toolchain smoke tests.

hypothesis and pytest-asyncio are configured in pyproject.toml but a
configuration that is never exercised is a configuration that is never
verified. These two tests assert nothing about Study 001; they exist so that a
broken asyncio mode or a missing hypothesis profile fails in CI instead of
failing in the first ticket that depends on it. Delete them only when real
property-based and async tests take their place.
"""

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st


@given(st.lists(st.integers()))
def test_hypothesis_is_wired(values: list[int]) -> None:
    assert len(list(reversed(values))) == len(values)


@pytest.mark.asyncio
async def test_pytest_asyncio_is_wired() -> None:
    await asyncio.sleep(0)
    assert True
