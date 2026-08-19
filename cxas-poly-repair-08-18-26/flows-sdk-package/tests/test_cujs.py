"""CUJ presets: cujs.yaml loading, defaults merge, aliases, app-dir defaults.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

import flows

FILE = {
    "version": 1,
    "variable_aliases": {"account": ["accountNumber", "account_id"]},
    "querystring_variables": ["mock_config_string"],
    "defaults": {
        "variables": {
            "mock_config_string": {
                "outage_status": "none",
                "convoy_status": "clear",
                "gateway_status": "clear",
            }
        }
    },
    "cujs": {
        "all_clear": {
            "description": "Everything healthy.",
            "aliases": ["clear"],
            "variables": {"account": "8069100230359946"},
        },
        "gateway_reboot": {
            "description": "Reboot offered.",
            "variables": {
                "account": "8069100230361003",
                "mock_config_string": {"gateway_status": "reboot"},
            },
        },
    },
}


def _write(tmp_path, data=None):
  path = tmp_path / "cujs.yaml"
  # sort_keys=False: querystring order follows the document, so the fixture must too.
  path.write_text(yaml.safe_dump(data if data is not None else FILE, sort_keys=False))
  return str(path)


def test_defaults_merge_under_each_cuj(tmp_path):
  cujs = flows.load_cujs(_write(tmp_path))
  # The CUJ overrides one key inside the querystring; the siblings survive.
  assert cujs["gateway_reboot"].variables["mock_config_string"] == (
      "outage_status=none&convoy_status=clear&gateway_status=reboot")
  assert cujs["all_clear"].variables["mock_config_string"] == (
      "outage_status=none&convoy_status=clear&gateway_status=clear")


def test_variable_aliases_fan_out(tmp_path):
  v = flows.load_cujs(_write(tmp_path))["gateway_reboot"].variables
  assert v["accountNumber"] == "8069100230361003"
  assert v["account_id"] == "8069100230361003"
  assert "account" not in v


def test_alias_lookup_and_names(tmp_path):
  cujs = flows.load_cujs(_write(tmp_path))
  assert cujs["clear"] is cujs["all_clear"]
  assert "clear" in cujs
  assert cujs.names() == ["all_clear", "gateway_reboot"]
  assert len(cujs) == 2


def test_unknown_name_lists_what_is_available(tmp_path):
  cujs = flows.load_cujs(_write(tmp_path))
  with pytest.raises(KeyError) as e:
    cujs["nope"]
  assert "gateway_reboot" in str(e.value)


def test_querystring_is_opt_in_so_object_variables_survive(tmp_path):
  data = {
      "querystring_variables": ["mock_config_string"],
      "cujs": {"x": {"variables": {"client_wifi_args": {"band": "5g"},
                                   "mock_config_string": {"a": "b"}}}},
  }
  v = flows.load_cujs(_write(tmp_path, data))["x"].variables
  assert v["client_wifi_args"] == {"band": "5g"}   # stays an object
  assert v["mock_config_string"] == "a=b"          # serialized


def test_defaults_merge_is_recursive(tmp_path):
  data = {
      "defaults": {"variables": {"event_data": {"caller": {"ani": "1", "vip": "no"}}}},
      "cujs": {"x": {"variables": {"event_data": {"caller": {"vip": "yes"}}}}},
  }
  v = flows.load_cujs(_write(tmp_path, data))["x"].variables
  assert v["event_data"] == {"caller": {"ani": "1", "vip": "yes"}}


def test_a_nested_querystring_value_is_rejected(tmp_path):
  data = {
      "querystring_variables": ["mock"],
      "cujs": {"x": {"variables": {"mock": {"a": {"b": "c"}}}}},
  }
  with pytest.raises(ValueError, match="must be a scalar"):
    flows.load_cujs(_write(tmp_path, data))


def test_malformed_yaml_is_a_value_error(tmp_path):
  path = tmp_path / "cujs.yaml"
  path.write_text("cujs:\n  x: {unclosed\n")
  with pytest.raises(ValueError):
    flows.load_cujs(str(path))


def test_scalars_are_stringified(tmp_path):
  data = {"cujs": {"x": {"variables": {"n": 42, "flag": True}}}}
  v = flows.load_cujs(_write(tmp_path, data))["x"].variables
  assert v == {"n": "42", "flag": "true"}


def test_duplicate_name_or_alias_is_rejected(tmp_path):
  data = {"cujs": {"a": {"aliases": ["shared"]}, "b": {"aliases": ["shared"]}}}
  with pytest.raises(ValueError, match="already used"):
    flows.load_cujs(_write(tmp_path, data))


def test_unknown_keys_are_rejected(tmp_path):
  with pytest.raises(ValueError, match="unknown top-level key"):
    flows.load_cujs(_write(tmp_path, {"cujs": {}, "typo": 1}))
  with pytest.raises(ValueError, match="unknown key"):
    flows.load_cujs(_write(tmp_path, {"cujs": {"a": {"variable": {}}}}))


def test_find_cujs_file_walks_up(tmp_path):
  _write(tmp_path)
  nested = tmp_path / "a" / "b"
  nested.mkdir(parents=True)
  assert flows.find_cujs_file(str(nested)) == str(tmp_path / "cujs.yaml")
  assert flows.load_cujs(start=str(nested)).names() == ["all_clear", "gateway_reboot"]


def test_env_var_wins_over_the_walk(tmp_path, monkeypatch):
  path = _write(tmp_path)
  monkeypatch.setenv("FLOWS_CUJS", path)
  other = tmp_path / "elsewhere"
  other.mkdir()
  assert flows.find_cujs_file(str(other)) == path


def test_missing_file_names_the_env_var(tmp_path, monkeypatch):
  monkeypatch.delenv("FLOWS_CUJS", raising=False)
  with pytest.raises(FileNotFoundError, match="FLOWS_CUJS"):
    flows.load_cujs(start=str(tmp_path))


def test_cuj_variables_shorthand(tmp_path):
  assert flows.cuj_variables("clear", _write(tmp_path))["accountNumber"] == "8069100230359946"


# --- apply_to_app_dir ---------------------------------------------------------
def _app_dir(tmp_path, declarations):
  d = tmp_path / "app"
  d.mkdir()
  (d / "app.json").write_text(json.dumps({"variableDeclarations": declarations}))
  return str(d)


def test_apply_sets_and_overrides_declaration_defaults(tmp_path):
  app_dir = _app_dir(tmp_path, [
      {"name": "accountNumber", "schema": {"type": "STRING", "default": ""}},
      {"name": "account_id", "schema": {"type": "STRING"}},
      {"name": "mock_config_string", "schema": {"type": "STRING"}},
  ])
  cuj = flows.load_cujs(_write(tmp_path))["gateway_reboot"]
  written = flows.apply_to_app_dir(app_dir, cuj)

  assert sorted(written) == ["accountNumber", "account_id", "mock_config_string"]
  with open(os.path.join(app_dir, "app.json")) as f:
    by_name = {d["name"]: d for d in json.load(f)["variableDeclarations"]}
  assert by_name["accountNumber"]["schema"]["default"] == "8069100230361003"
  assert by_name["accountNumber"]["schema"]["type"] == "STRING"   # not clobbered
  assert "gateway_status=reboot" in by_name["mock_config_string"]["schema"]["default"]


def test_apply_is_strict_about_undeclared_variables(tmp_path):
  app_dir = _app_dir(tmp_path, [{"name": "accountNumber", "schema": {}}])
  cuj = flows.load_cujs(_write(tmp_path))["gateway_reboot"]
  with pytest.raises(ValueError, match="account_id"):
    flows.apply_to_app_dir(app_dir, cuj)
  assert flows.apply_to_app_dir(app_dir, cuj, strict=False) == ["accountNumber"]


def test_apply_accepts_a_plain_dict(tmp_path):
  app_dir = _app_dir(tmp_path, [{"name": "x", "schema": {}}])
  assert flows.apply_to_app_dir(app_dir, {"x": "1"}) == ["x"]
