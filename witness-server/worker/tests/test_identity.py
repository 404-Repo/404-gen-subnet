from __future__ import annotations

import pytest
from bittensor_wallet import Keypair

from identity import miner_prefix


def test_round_and_hotkey_define_the_transcript_group(
    identity: tuple[Keypair, str],
) -> None:
    hotkey, other = identity
    first = miner_prefix("1", hotkey.ss58_address)
    assert first == f"rounds/1/{hotkey.ss58_address}"
    assert first != miner_prefix("2", hotkey.ss58_address)
    assert miner_prefix("0", hotkey.ss58_address) == f"rounds/0/{hotkey.ss58_address}"
    assert miner_prefix("test-0", hotkey.ss58_address) == (
        f"rounds/test-0/{hotkey.ss58_address}"
    )
    assert first != miner_prefix("1", other)
    with pytest.raises(ValueError):
        miner_prefix("../1", hotkey.ss58_address)
    with pytest.raises(ValueError):
        miner_prefix("1", "../another-miner")
