"""Tests for helipontos_sp.py — focusing on coordinate parsing and core logic."""

import math

import pytest

from helipontos_sp import dms_to_decimal


class TestDmsToDecimal:
    """Test GMS → decimal coordinate conversion."""

    def test_typical_anac_latitude(self):
        """023°35'10,0"S → approx -23.5861"""
        result = dms_to_decimal('023°35\'10,0"S')
        assert result is not None
        assert result < 0  # south
        assert math.isclose(result, -23.586111, abs_tol=0.001)

    def test_typical_anac_longitude(self):
        """046°39'12,0"W → approx -46.6533"""
        result = dms_to_decimal('046°39\'12,0"W')
        assert result is not None
        assert result < 0  # west
        assert math.isclose(result, -46.653333, abs_tol=0.001)

    def test_longitude_with_O(self):
        """046°40'20,0"O (O = Oeste) → negative"""
        result = dms_to_decimal('046°40\'20,0"O')
        assert result is not None
        assert result < 0
        assert math.isclose(result, -46.672222, abs_tol=0.001)

    def test_spaced_format(self):
        """023° 35' 10'' S"""
        result = dms_to_decimal("023° 35' 10'' S")
        assert result is not None
        assert result < 0
        assert math.isclose(result, -23.586111, abs_tol=0.001)

    def test_already_decimal(self):
        result = dms_to_decimal("-23.585")
        assert result is not None
        assert math.isclose(result, -23.585, abs_tol=0.001)

    def test_already_decimal_comma(self):
        result = dms_to_decimal("-23,585")
        assert result is not None
        assert math.isclose(result, -23.585, abs_tol=0.001)

    def test_none_input(self):
        assert dms_to_decimal(None) is None

    def test_empty_string(self):
        assert dms_to_decimal("") is None

    def test_nan_input(self):
        assert dms_to_decimal(float("nan")) is None

    def test_north_positive(self):
        result = dms_to_decimal('10°30\'00,0"N')
        assert result is not None
        assert result > 0
        assert math.isclose(result, 10.5, abs_tol=0.001)

    def test_east_positive(self):
        result = dms_to_decimal('046°39\'12,0"E')
        assert result is not None
        assert result > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
