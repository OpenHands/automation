"""Tests for KV store helper functions.

Tests cover:
- Path parsing (valid and edge cases)
- Key validation (robustness against malicious/accidental inputs)
- Type validation (numeric, integer, list, dict)
"""

import pytest
from fastapi import HTTPException

from openhands.automation.kv_helpers import (
    _MAX_KEY_LENGTH,
    _MAX_PATH_DEPTH,
    get_nested_value,
    parse_path,
    require_dict,
    require_int,
    require_list,
    require_numeric,
    set_nested_value,
    validate_key,
)


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

    def test_path_at_max_depth_succeeds(self):
        """Path at exactly max depth succeeds."""
        path = ".".join(["a"] * _MAX_PATH_DEPTH)
        parts = parse_path(path)
        assert len(parts) == _MAX_PATH_DEPTH

    def test_path_exceeds_max_depth_raises(self):
        """Path exceeding max depth raises ValueError."""
        path = ".".join(["a"] * (_MAX_PATH_DEPTH + 1))
        with pytest.raises(ValueError, match="exceeds maximum depth"):
            parse_path(path)

    def test_very_deep_path_raises(self):
        """Very deep path raises with helpful error message."""
        path = ".".join(["x"] * 100)
        with pytest.raises(ValueError) as exc_info:
            parse_path(path)
        assert "100 segments" in str(exc_info.value)

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


# =============================================================================
# Key Validation Tests
# =============================================================================


class TestValidateKey:
    """Tests for validate_key() function.

    Validates that key names are safe for storage and retrieval.
    Protects against accidental, malicious, and ignorant clients.
    """

    # --- Valid keys ---

    def test_simple_key(self):
        """Simple alphanumeric key is valid."""
        assert validate_key("my_key") == "my_key"

    def test_key_with_dots(self):
        """Key with dots is valid (dots are only special in paths)."""
        assert validate_key("config.json") == "config.json"

    def test_key_with_hyphens(self):
        """Key with hyphens is valid."""
        assert validate_key("my-key-name") == "my-key-name"

    def test_key_with_spaces(self):
        """Key with internal spaces is valid."""
        assert validate_key("my key") == "my key"

    def test_unicode_key(self):
        """Unicode characters in keys are valid."""
        assert validate_key("日本語キー") == "日本語キー"
        assert validate_key("emoji_🔑") == "emoji_🔑"

    def test_max_length_key(self):
        """Key at exactly max length is valid."""
        key = "a" * _MAX_KEY_LENGTH
        assert validate_key(key) == key

    def test_numeric_key(self):
        """Numeric string key is valid."""
        assert validate_key("12345") == "12345"

    # --- Invalid keys: Empty/Whitespace ---

    def test_empty_key_rejected(self):
        """Empty string key is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("")
        assert exc_info.value.status_code == 400
        assert "cannot be empty" in exc_info.value.detail

    def test_whitespace_only_key_rejected(self):
        """Whitespace-only key is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("   ")
        assert exc_info.value.status_code == 400
        assert "whitespace-only" in exc_info.value.detail

    def test_tabs_only_key_rejected(self):
        """Tab-only key is rejected as whitespace."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("\t\t")
        assert exc_info.value.status_code == 400
        # Tabs are control characters, so this might fail on control char check first
        # Either error message is acceptable

    # --- Invalid keys: Too long ---

    def test_key_exceeds_max_length_rejected(self):
        """Key exceeding max length is rejected."""
        key = "a" * (_MAX_KEY_LENGTH + 1)
        with pytest.raises(HTTPException) as exc_info:
            validate_key(key)
        assert exc_info.value.status_code == 400
        assert "exceeds" in exc_info.value.detail
        assert str(_MAX_KEY_LENGTH) in exc_info.value.detail

    def test_very_long_key_rejected(self):
        """Very long key is rejected with helpful error."""
        key = "x" * 1000
        with pytest.raises(HTTPException) as exc_info:
            validate_key(key)
        assert exc_info.value.status_code == 400
        assert "1000 given" in exc_info.value.detail

    # --- Invalid keys: Control characters ---

    def test_null_byte_in_key_rejected(self):
        """Key containing null byte is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("key\x00value")
        assert exc_info.value.status_code == 400
        assert "control character" in exc_info.value.detail
        assert "\\x00" in exc_info.value.detail

    def test_newline_in_key_rejected(self):
        """Key containing newline is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("key\nvalue")
        assert exc_info.value.status_code == 400
        assert "control character" in exc_info.value.detail
        assert "\\x0a" in exc_info.value.detail

    def test_carriage_return_in_key_rejected(self):
        """Key containing carriage return is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("key\rvalue")
        assert exc_info.value.status_code == 400
        assert "control character" in exc_info.value.detail

    def test_tab_in_key_rejected(self):
        """Key containing tab is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("key\tvalue")
        assert exc_info.value.status_code == 400
        assert "control character" in exc_info.value.detail
        assert "\\x09" in exc_info.value.detail

    def test_delete_char_in_key_rejected(self):
        """Key containing DEL character (0x7F) is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("key\x7fvalue")
        assert exc_info.value.status_code == 400
        assert "control character" in exc_info.value.detail
        assert "\\x7f" in exc_info.value.detail

    def test_bell_char_in_key_rejected(self):
        """Key containing bell character is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("key\x07value")
        assert exc_info.value.status_code == 400
        assert "control character" in exc_info.value.detail

    def test_control_char_position_reported(self):
        """Error message includes position of control character."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("abc\x00def")
        assert "position 3" in exc_info.value.detail

    # --- Edge cases that SHOULD be allowed ---

    def test_leading_space_allowed(self):
        """Leading space is allowed (not whitespace-only)."""
        # This might be surprising, but " key" has content
        assert validate_key(" key") == " key"

    def test_trailing_space_allowed(self):
        """Trailing space is allowed."""
        assert validate_key("key ") == "key "

    def test_path_traversal_string_allowed(self):
        """Path traversal strings are allowed (no filesystem, just strings)."""
        # These look suspicious but are harmless as KV keys
        assert validate_key("../../../etc/passwd") == "../../../etc/passwd"
        assert validate_key("..") == ".."

    def test_javascript_prototype_names_allowed(self):
        """JavaScript prototype pollution names are allowed."""
        # These are valid key names, just be careful in JS clients
        assert validate_key("__proto__") == "__proto__"
        assert validate_key("constructor") == "constructor"
        assert validate_key("toString") == "toString"

    def test_slashes_allowed(self):
        """Slashes are allowed in keys."""
        assert validate_key("path/to/key") == "path/to/key"

    # --- Invalid keys: Reserved prefix ---

    def test_dollar_prefix_rejected(self):
        """Key starting with $ is rejected (reserved for system use)."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("$version")
        assert exc_info.value.status_code == 400
        assert "reserved" in exc_info.value.detail.lower()

    def test_dollar_prefix_any_name_rejected(self):
        """Any key starting with $ is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            validate_key("$anything")
        assert exc_info.value.status_code == 400
        assert "reserved" in exc_info.value.detail.lower()

    def test_dollar_in_middle_allowed(self):
        """Dollar sign in middle of key is allowed."""
        assert validate_key("my$key") == "my$key"

    def test_dollar_at_end_allowed(self):
        """Dollar sign at end of key is allowed."""
        assert validate_key("key$") == "key$"


# =============================================================================
# Type Validation Tests
# =============================================================================


class TestRequireNumeric:
    """Tests for require_numeric() function.

    Protects against type confusion, especially the Python quirk
    where bool is a subclass of int.
    """

    # --- Valid numeric values ---

    def test_integer_accepted(self):
        """Integer values are accepted."""
        assert require_numeric(42) == 42
        assert require_numeric(0) == 0
        assert require_numeric(-1) == -1

    def test_float_accepted(self):
        """Float values are accepted."""
        assert require_numeric(3.14) == 3.14
        assert require_numeric(0.0) == 0.0
        assert require_numeric(-1.5) == -1.5

    def test_large_integer_accepted(self):
        """Large integers are accepted."""
        big = 10**100
        assert require_numeric(big) == big

    def test_scientific_notation_accepted(self):
        """Scientific notation floats are accepted."""
        assert require_numeric(1e10) == 1e10
        assert require_numeric(1e-10) == 1e-10

    # --- Boolean rejection (critical!) ---

    def test_true_rejected(self):
        """Boolean True is rejected even though bool is subclass of int."""
        with pytest.raises(HTTPException) as exc_info:
            require_numeric(True)
        assert exc_info.value.status_code == 400
        assert "boolean" in exc_info.value.detail

    def test_false_rejected(self):
        """Boolean False is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_numeric(False)
        assert exc_info.value.status_code == 400
        assert "boolean" in exc_info.value.detail

    # --- Other non-numeric types ---

    def test_string_rejected(self):
        """String values are rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_numeric("42")
        assert exc_info.value.status_code == 400
        assert "not numeric" in exc_info.value.detail

    def test_numeric_string_rejected(self):
        """Numeric-looking strings are rejected (no coercion)."""
        with pytest.raises(HTTPException) as exc_info:
            require_numeric("3.14")
        assert exc_info.value.status_code == 400

    def test_none_rejected(self):
        """None is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_numeric(None)
        assert exc_info.value.status_code == 400

    def test_list_rejected(self):
        """List is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_numeric([1, 2, 3])
        assert exc_info.value.status_code == 400

    def test_dict_rejected(self):
        """Dict is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_numeric({"value": 42})
        assert exc_info.value.status_code == 400


class TestRequireInt:
    """Tests for require_int() function.

    Stricter than require_numeric - used for incr/decr operations
    where float arithmetic could cause precision loss.
    """

    # --- Valid integer values ---

    def test_integer_accepted(self):
        """Integer values are accepted."""
        assert require_int(42) == 42
        assert require_int(0) == 0
        assert require_int(-1) == -1

    def test_large_integer_accepted(self):
        """Large integers are accepted."""
        big = 10**100
        assert require_int(big) == big

    # --- Float rejection (critical for incr/decr!) ---

    def test_float_rejected(self):
        """Float values are rejected with helpful message."""
        with pytest.raises(HTTPException) as exc_info:
            require_int(3.14)
        assert exc_info.value.status_code == 400
        assert "float" in exc_info.value.detail
        assert "integer" in exc_info.value.detail

    def test_whole_number_float_rejected(self):
        """Even whole-number floats like 1.0 are rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_int(1.0)
        assert exc_info.value.status_code == 400
        assert "float" in exc_info.value.detail

    def test_zero_float_rejected(self):
        """0.0 is rejected (use 0 instead)."""
        with pytest.raises(HTTPException) as exc_info:
            require_int(0.0)
        assert exc_info.value.status_code == 400

    # --- Boolean rejection ---

    def test_true_rejected(self):
        """Boolean True is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_int(True)
        assert exc_info.value.status_code == 400
        assert "boolean" in exc_info.value.detail

    def test_false_rejected(self):
        """Boolean False is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_int(False)
        assert exc_info.value.status_code == 400
        assert "boolean" in exc_info.value.detail

    # --- Other types ---

    def test_string_rejected(self):
        """String values are rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_int("42")
        assert exc_info.value.status_code == 400
        assert "not an integer" in exc_info.value.detail

    def test_none_rejected(self):
        """None is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_int(None)
        assert exc_info.value.status_code == 400


class TestRequireList:
    """Tests for require_list() function."""

    def test_empty_list_accepted(self):
        """Empty list is accepted."""
        assert require_list([]) == []

    def test_list_with_items_accepted(self):
        """List with items is accepted."""
        assert require_list([1, 2, 3]) == [1, 2, 3]

    def test_nested_list_accepted(self):
        """Nested list is accepted."""
        assert require_list([[1], [2]]) == [[1], [2]]

    def test_dict_rejected(self):
        """Dict is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_list({})
        assert exc_info.value.status_code == 400
        assert "not a list" in exc_info.value.detail

    def test_string_rejected(self):
        """String is rejected (even though iterable)."""
        with pytest.raises(HTTPException) as exc_info:
            require_list("hello")
        assert exc_info.value.status_code == 400

    def test_tuple_rejected(self):
        """Tuple is rejected (we want explicit list type)."""
        with pytest.raises(HTTPException) as exc_info:
            require_list((1, 2, 3))
        assert exc_info.value.status_code == 400


class TestRequireDict:
    """Tests for require_dict() function."""

    def test_empty_dict_accepted(self):
        """Empty dict is accepted."""
        assert require_dict({}) == {}

    def test_dict_with_items_accepted(self):
        """Dict with items is accepted."""
        assert require_dict({"key": "value"}) == {"key": "value"}

    def test_list_rejected(self):
        """List is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_dict([])
        assert exc_info.value.status_code == 400
        assert "not an object" in exc_info.value.detail

    def test_string_rejected(self):
        """String is rejected."""
        with pytest.raises(HTTPException) as exc_info:
            require_dict("hello")
        assert exc_info.value.status_code == 400
