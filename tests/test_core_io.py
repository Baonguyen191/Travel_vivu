import json

import pytest

from core import fixtures as fx
from core import io
from core.__main__ import main as cli
from core.types import Problem, Schedule


def test_save_load_roundtrip(tmp_path):
    problem = fx.sample_problem(days=2)
    path = tmp_path / "p.json"
    io.save(problem, path)
    env = json.loads(path.read_text(encoding="utf-8"))
    assert env["schema"] == "Problem" and env["version"] == io.SCHEMA_VERSION
    assert io.load(path) == problem
    assert io.load(path, Problem) == problem


def test_load_rejects_wrong_type_version_and_bad_data(tmp_path):
    path = tmp_path / "s.json"
    io.save(fx.sample_schedule(), path)
    with pytest.raises(io.SchemaError, match="cần Problem"):
        io.load(path, "Problem")
    env = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(io.SchemaError, match="phiên bản"):
        io.from_envelope({**env, "version": 99})
    env["data"]["visits"][0]["end"] = "07:00"
    with pytest.raises(io.SchemaError, match="kết thúc"):
        io.from_envelope(env)
    with pytest.raises(io.SchemaError, match="schema lạ"):
        io.from_envelope({"schema": "Hotel", "version": 1, "data": {}})
    with pytest.raises(io.SchemaError, match="JSON hỏng"):
        io.loads("{")
    with pytest.raises(io.SchemaError):
        io.envelope({"not": "core"})


def test_jsonl_reports_line_number(tmp_path):
    path = tmp_path / "many.jsonl"
    assert io.save_jsonl([fx.sample_schedule(), fx.sample_schedule()], path) == 2
    assert len(io.load_jsonl(path, Schedule)) == 2
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"schema": "Schedule", "version": 1, "data": {"visits": [], "source": "x"}}\n')
    with pytest.raises(io.SchemaError, match="dòng 3"):
        io.load_jsonl(path)


def test_cli_schema_example_check(tmp_path, capsys):
    assert cli(["schema"]) == 0 and "Problem" in capsys.readouterr().out
    assert cli(["schema", "Place"]) == 0 and "opening_hours" in capsys.readouterr().out
    assert cli(["schema", "Hotel"]) == 2
    capsys.readouterr()
    assert cli(["example", "Problem"]) == 0
    out = capsys.readouterr().out
    path = tmp_path / "ex.json"
    path.write_text(out, encoding="utf-8")
    assert cli(["check", str(path), "--type", "Problem"]) == 0
    assert "OK: 1 Problem" in capsys.readouterr().out
    path.write_text(out.replace('"08:00"', '"8h"', 1), encoding="utf-8")
    assert cli(["check", str(path)]) == 2
