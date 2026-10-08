"""Temporary repositories only; no real key or patient data is used."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def guard(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[1]/"scripts/check_repo_safety.py"
    spec=importlib.util.spec_from_file_location("repo_guard",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    subprocess.run(["git","init","-q",str(tmp_path)],check=True)
    monkeypatch.setattr(module,"ROOT",tmp_path)
    monkeypatch.delenv("LLM_API_KEY",raising=False)
    monkeypatch.setattr(sys,"argv",["check_repo_safety.py","--staged"])
    return module,tmp_path


def stage(root,name,content):
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
    subprocess.run(["git","add",name],cwd=root,check=True)


def test_guard_accepts_plain_source(guard):
    module,root=guard;stage(root,"example.py",b"print('hello')\n")
    assert module.main()==0


@pytest.mark.parametrize("name",["data/example.json","private/example.md","outputs/trace.jsonl",".env.local","cases.csv"])
def test_guard_blocks_protected_paths(guard,name):
    module,root=guard;stage(root,name,b"synthetic")
    assert module.main()==1


def test_guard_detects_key_pattern_without_printing_value(guard,capsys):
    module,root=guard
    fake=b"sk-"+b"X"*30
    stage(root,"example.py",fake)
    assert module.main()==1
    assert fake.decode() not in capsys.readouterr().out


def test_guard_detects_active_provider_key_without_a_prefix(guard,monkeypatch,capsys):
    module,root=guard
    fake="synthetic-private-provider-credential"
    monkeypatch.setenv("LLM_API_KEY",fake)
    stage(root,"example.txt",fake.encode())
    assert module.main()==1
    assert fake not in capsys.readouterr().out
