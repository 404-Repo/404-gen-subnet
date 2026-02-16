"""Tests for weights_setter.weights_service module."""

import pytest

from weights_service import WeightsResult, WeightsService


class TestWeightsResult:
    """Validate WeightsResult model."""

    def test_basic_construction(self) -> None:
        wr = WeightsResult(uids=[0, 1], weights=[0.3, 0.7])
        assert wr.uids == [0, 1]
        assert wr.weights == [0.3, 0.7]

    def test_single_uid(self) -> None:
        wr = WeightsResult(uids=[5], weights=[1.0])
        assert len(wr.uids) == 1

    def test_empty_lists(self) -> None:
        wr = WeightsResult(uids=[], weights=[])
        assert wr.uids == []

    def test_json_roundtrip(self) -> None:
        wr = WeightsResult(uids=[0, 1, 2], weights=[0.2, 0.3, 0.5])
        restored = WeightsResult.model_validate_json(wr.model_dump_json())
        assert restored == wr


class TestCalculateWeights:
    """Test the _calculate_weights method of WeightsService."""

    @staticmethod
    def _make_service(subnet_owner_uid: int = 0) -> WeightsService:
        """Create a WeightsService with minimal mocked dependencies."""
        from unittest.mock import MagicMock

        return WeightsService(
            subtensor=MagicMock(),
            repo="test/repo",
            branch="main",
            token="fake-token",
            set_weights_interval_sec=600,
            set_weights_iteration_timeout_sec=120,
            set_weights_min_retry_interval_sec=30,
            set_weights_max_retry_interval_sec=120,
            next_leader_wait_interval_sec=60,
            subnet_owner_uid=subnet_owner_uid,
            netuid=17,
            wallet=MagicMock(),
        )

    def test_no_leader_returns_owner_only(self) -> None:
        """When leader_uid is None, all weight goes to subnet owner."""
        svc = self._make_service(subnet_owner_uid=0)
        result = svc._calculate_weights(leader_uid=None, leader_weight=0.5)
        assert result.uids == [0]
        assert result.weights == [1.0]

    def test_leader_is_owner_returns_owner_only(self) -> None:
        """When leader IS the subnet owner, all weight goes to owner."""
        svc = self._make_service(subnet_owner_uid=5)
        result = svc._calculate_weights(leader_uid=5, leader_weight=0.8)
        assert result.uids == [5]
        assert result.weights == [1.0]

    def test_leader_different_from_owner(self) -> None:
        """When leader differs from owner, weight is split."""
        svc = self._make_service(subnet_owner_uid=0)
        result = svc._calculate_weights(leader_uid=7, leader_weight=0.6)
        assert result.uids == [0, 7]
        assert result.weights[0] == pytest.approx(0.4)  # owner_weight = 1 - 0.6
        assert result.weights[1] == pytest.approx(0.6)  # leader_weight

    def test_leader_full_weight(self) -> None:
        """Leader with weight 1.0 -> owner gets 0.0."""
        svc = self._make_service(subnet_owner_uid=0)
        result = svc._calculate_weights(leader_uid=3, leader_weight=1.0)
        assert result.weights[0] == pytest.approx(0.0)
        assert result.weights[1] == pytest.approx(1.0)

    def test_leader_zero_weight(self) -> None:
        """Leader with weight 0.0 -> owner gets 1.0."""
        svc = self._make_service(subnet_owner_uid=0)
        result = svc._calculate_weights(leader_uid=3, leader_weight=0.0)
        assert result.weights[0] == pytest.approx(1.0)
        assert result.weights[1] == pytest.approx(0.0)


class TestScheduleRetry:
    """Test the _schedule_retry method."""

    def test_retry_within_bounds(self) -> None:
        svc = TestCalculateWeights._make_service()
        current_time = 1000.0
        next_time = svc._schedule_retry(current_time)
        retry_interval = next_time - current_time
        assert 30 <= retry_interval <= 120
