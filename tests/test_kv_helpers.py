"""Tests for KV store path helper functions.

Tests cover both valid and edge cases for path parsing.
"""

import pytest

from automation.kv_helpers import get_nested_value, parse_path, set_nested_value


class TestParsePath:
    """Tests for parse_path() function."""

    def test_simple_dot_notation(self):
        """Simple dot-separated path."""
        assert parse_path("database.host") == ["database", "host"]

    def test_single_key(self):
        """Single key with no dots."""
        assert parse_path("key") == ["key"]

    def test_empty_string(self):
        """Empty string returns empty list."""
        assert parse_path("") == []

    def test_bracket_notation_double_quotes(self):
        """Bracket notation with double quotes."""
        assert parse_path('config["my.key"]') == ["config", "my.key"]

    def test_bracket_notation_single_quotes(self):
        """Bracket notation with single quotes."""
        assert parse_path("config['my.key']") == ["config", "my.key"]

    def test_bracket_notation_no_quotes(self):
        """Bracket notation without quotes."""
        assert parse_path("config[0]") == ["config", "0"]

    def test_mixed_notation(self):
        """Mix of dot and bracket notation."""
        assert parse_path('data["items"][0].name') == ["data", "items", "0", "name"]

    def test_consecutive_brackets(self):
        """Multiple consecutive brackets."""
        assert parse_path("arr[0][1]") == ["arr", "0", "1"]

    def test_numeric_keys(self):
        """Numeric keys in dot notation."""
        assert parse_path("data.0.1") == ["data", "0", "1"]

    def test_trailing_dot(self):
        """Trailing dot is ignored."""
        assert parse_path("foo.bar.") == ["foo", "bar"]

    def test_leading_dot(self):
        """Leading dot is ignored."""
        assert parse_path(".foo.bar") == ["foo", "bar"]

    def test_unclosed_bracket_raises(self):
        """Unclosed bracket raises ValueError."""
        with pytest.raises(ValueError, match="unclosed bracket"):
            parse_path("config[key")

    def test_empty_segments_ignored(self):
        """Empty segments from consecutive dots are ignored."""
        # Two consecutive dots should not create empty segment
        assert parse_path("foo..bar") == ["foo", "bar"]

    def test_bracket_at_end(self):
        """Bracket notation at end of path."""
        assert parse_path('config.database["host"]') == ["config", "database", "host"]


class TestGetNestedValue:
    """Tests for get_nested_value() function."""

    def test_simple_dict_access(self):
        """Access simple dict key."""
        obj = {"foo": "bar"}
        assert get_nested_value(obj, "foo") == "bar"

    def test_nested_dict_access(self):
        """Access nested dict."""
        obj = {"database": {"host": "localhost", "port": 5432}}
        assert get_nested_value(obj, "database.host") == "localhost"

    def test_list_index_access(self):
        """Access list by index."""
        obj = {"items": ["a", "b", "c"]}
        assert get_nested_value(obj, "items.1") == "b"

    def test_nested_list_access(self):
        """Access nested list."""
        obj = {"matrix": [[1, 2], [3, 4]]}
        assert get_nested_value(obj, "matrix.0.1") == 2

    def test_empty_path_returns_object(self):
        """Empty path returns the object itself."""
        obj = {"foo": "bar"}
        assert get_nested_value(obj, "") == obj

    def test_missing_key_raises(self):
        """Missing key raises KeyError."""
        obj = {"foo": "bar"}
        with pytest.raises(KeyError, match="not found"):
            get_nested_value(obj, "missing")

    def test_missing_nested_key_raises(self):
        """Missing nested key raises KeyError."""
        obj = {"foo": {"bar": "baz"}}
        with pytest.raises(KeyError, match="not found"):
            get_nested_value(obj, "foo.missing")

    def test_list_index_out_of_bounds_raises(self):
        """List index out of bounds raises KeyError."""
        obj = {"items": ["a", "b"]}
        with pytest.raises(KeyError, match="not found"):
            get_nested_value(obj, "items.5")

    def test_invalid_list_index_raises(self):
        """Non-numeric list index raises KeyError."""
        obj = {"items": ["a", "b"]}
        with pytest.raises(KeyError, match="not found"):
            get_nested_value(obj, "items.foo")

    def test_traverse_non_container_raises(self):
        """Traversing through a non-dict/list raises KeyError."""
        obj = {"foo": "bar"}
        with pytest.raises(KeyError, match="not found"):
            get_nested_value(obj, "foo.baz")

    def test_bracket_notation_with_dots(self):
        """Access key containing dots via bracket notation."""
        obj = {"config": {"my.key.with.dots": "value"}}
        assert get_nested_value(obj, 'config["my.key.with.dots"]') == "value"


class TestSetNestedValue:
    """Tests for set_nested_value() function."""

    def test_set_simple_key(self):
        """Set simple key."""
        obj: dict = {}
        set_nested_value(obj, "foo", "bar")
        assert obj == {"foo": "bar"}

    def test_set_nested_key(self):
        """Set nested key."""
        obj = {"database": {}}
        set_nested_value(obj, "database.host", "localhost")
        assert obj == {"database": {"host": "localhost"}}

    def test_create_intermediate_dicts(self):
        """Creates intermediate dicts as needed."""
        obj: dict = {}
        set_nested_value(obj, "a.b.c", "value")
        assert obj == {"a": {"b": {"c": "value"}}}

    def test_overwrite_existing_value(self):
        """Overwrite existing value."""
        obj = {"foo": "old"}
        set_nested_value(obj, "foo", "new")
        assert obj == {"foo": "new"}

    def test_returns_same_object(self):
        """Returns the same dict object (mutated in place)."""
        obj = {"foo": "bar"}
        result = set_nested_value(obj, "baz", "qux")
        assert result is obj

    def test_intermediate_non_dict_raises(self):
        """Setting through non-dict intermediate raises ValueError."""
        obj = {"foo": "bar"}
        with pytest.raises(ValueError, match="intermediate value is not a dict"):
            set_nested_value(obj, "foo.baz", "value")

    def test_bracket_notation_with_dots(self):
        """Set key containing dots via bracket notation."""
        obj = {"config": {}}
        set_nested_value(obj, 'config["my.key"]', "value")
        assert obj == {"config": {"my.key": "value"}}
