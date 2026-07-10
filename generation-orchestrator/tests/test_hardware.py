from generation_orchestrator.hardware import select_hardware


def test_prefers_rtx_when_both_declared() -> None:
    """The orchestrator's preference (cheaper RTX first) decides, not the miner's order."""
    assert select_hardware(["4xH200", "4xRTX6000Pro"], log_id="m") == ("RTX6000Pro", 4)
    assert select_hardware(["4xRTX6000Pro", "4xH200"], log_id="m") == ("RTX6000Pro", 4)


def test_single_declaration_is_honored() -> None:
    assert select_hardware(["4xH200"], log_id="m") == ("H200", 4)
    assert select_hardware(["4xRTX6000Pro"], log_id="m") == ("RTX6000Pro", 4)


def test_unknown_tokens_fall_back_to_default() -> None:
    assert select_hardware(["8xB200"], log_id="m") == ("H200", 4)