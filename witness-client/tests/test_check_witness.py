"""The public timing sample reuses one client and never retries a failed request."""

import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from witness_client import WitnessError

SCRIPT = Path(__file__).resolve().parents[1] / "examples/check_witness.py"


@pytest.mark.parametrize("failure", [False, True])
def test_repeated_public_requests_reuse_one_client(
    monkeypatch: pytest.MonkeyPatch, failure: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    client_type = MagicMock()
    client = client_type.return_value.__enter__.return_value
    client.create.return_value.output_text = "witness test OK"
    client.create.return_value.usage = {"input_tokens": 13, "output_tokens": 8}
    if failure:
        client.create.side_effect = WitnessError("Provider rejected")
    monkeypatch.setattr("witness_client.WitnessClient", client_type)
    monkeypatch.setattr("bittensor_wallet.Wallet", MagicMock())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--wallet-name",
            "fixture",
            "--wallet-hotkey",
            "fixture",
            "--model",
            "fixture",
            "--subscription",
            "--requests",
            "3",
        ],
    )
    main = cast(Callable[[], None], runpy.run_path(str(SCRIPT))["main"])
    if failure:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
        assert client.create.call_count == 1
    else:
        main()
        assert client.create.call_count == 3
        assert "Request 3: witness test OK" in capsys.readouterr().out
    assert client_type.call_count == 1
    for call in client.create.call_args_list:
        assert call.kwargs["input"] == "Reply with exactly: witness test OK"
