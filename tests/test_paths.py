"""Tests for path resolution and safety validation (design 29)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kimi_sandbox.errors import (
    InvalidPathError,
    InvalidProjectError,
    KimiNotFoundError,
)
from kimi_sandbox.paths import (
    default_kimi_code_home,
    resolve_kimi,
    resolve_project_dir,
    resolve_unsafe_kimi_code_home,
    validate_path_relationships,
)


# --- resolve_project_dir -------------------------------------------------

def test_resolve_project_dir_relative(tmp_path, monkeypatch):
    sub = tmp_path / "proj"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert resolve_project_dir(".") == sub.resolve()


def test_resolve_project_dir_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "work"
    proj.mkdir()
    assert resolve_project_dir("~/work") == proj.resolve()


def test_resolve_project_dir_rejects_root():
    with pytest.raises(InvalidProjectError):
        resolve_project_dir("/")


def test_resolve_project_dir_rejects_real_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(InvalidProjectError):
        resolve_project_dir(str(tmp_path))


def test_resolve_project_dir_rejects_broad_dirs():
    for d in ("/etc", "/usr", "/var", "/tmp", "/home"):
        with pytest.raises(InvalidProjectError):
            resolve_project_dir(d)


def test_resolve_project_dir_missing(tmp_path):
    with pytest.raises(InvalidProjectError):
        resolve_project_dir(str(tmp_path / "does-not-exist"))


def test_resolve_project_dir_not_a_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(InvalidProjectError):
        resolve_project_dir(str(f))


# --- validate_path_relationships ----------------------------------------

def test_validate_rejects_project_contains_kimi_home(tmp_path):
    project = tmp_path / "proj"
    kimi_home = project / "kimi-code-home"
    state_root = tmp_path / "state"
    with pytest.raises(InvalidPathError):
        validate_path_relationships(
            project_dir=project,
            state_root=state_root,
            kimi_code_home=kimi_home,
            unsafe_kimi_code_home=False,
        )


def test_validate_rejects_kimi_home_contains_project(tmp_path):
    kimi_home = tmp_path / "home"
    project = kimi_home / "proj"
    state_root = tmp_path / "state"
    with pytest.raises(InvalidPathError):
        validate_path_relationships(
            project_dir=project,
            state_root=state_root,
            kimi_code_home=kimi_home,
            unsafe_kimi_code_home=True,
        )


def test_validate_rejects_state_root_inside_project(tmp_path):
    project = tmp_path / "proj"
    state_root = project / "state"
    kimi_home = state_root / "profiles" / "default" / "kimi-code-home"
    with pytest.raises(InvalidPathError):
        validate_path_relationships(
            project_dir=project,
            state_root=state_root,
            kimi_code_home=kimi_home,
            unsafe_kimi_code_home=False,
        )


def test_validate_rejects_project_inside_state_root(tmp_path):
    state_root = tmp_path / "state"
    project = state_root / "proj"
    kimi_home = tmp_path / "home"
    with pytest.raises(InvalidPathError):
        validate_path_relationships(
            project_dir=project,
            state_root=state_root,
            kimi_code_home=kimi_home,
            unsafe_kimi_code_home=False,
        )


def test_validate_accepts_disjoint(tmp_path):
    project = tmp_path / "proj"
    state_root = tmp_path / "state"
    kimi_home = state_root / "profiles" / "default" / "kimi-code-home"
    # Should not raise.
    validate_path_relationships(
        project_dir=project,
        state_root=state_root,
        kimi_code_home=kimi_home,
        unsafe_kimi_code_home=False,
    )


# --- resolve_unsafe_kimi_code_home --------------------------------------

def test_unsafe_rejects_real_kimi_code_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / ".kimi-code"
    real.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_unsafe_kimi_code_home(str(real))


def test_unsafe_rejects_real_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(InvalidPathError):
        resolve_unsafe_kimi_code_home(str(tmp_path))


def test_unsafe_rejects_broad_dirs():
    for d in ("/", "/etc", "/usr", "/var", "/tmp", "/home"):
        with pytest.raises(InvalidPathError):
            resolve_unsafe_kimi_code_home(d)


def test_unsafe_accepts_custom_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    (tmp_path / "realhome").mkdir()
    custom = tmp_path / "custom-home"
    assert resolve_unsafe_kimi_code_home(str(custom)) == custom.resolve()


# --- resolve_kimi --------------------------------------------------------

def test_resolve_kimi_explicit_missing(tmp_path):
    with pytest.raises(KimiNotFoundError):
        resolve_kimi(str(tmp_path / "nope"))


def test_resolve_kimi_not_executable(tmp_path):
    f = tmp_path / "kimi"
    f.write_text("x")
    os.chmod(f, 0o644)
    with pytest.raises(KimiNotFoundError):
        resolve_kimi(str(f))


def test_resolve_kimi_executable(tmp_path):
    f = tmp_path / "kimi"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o755)
    assert resolve_kimi(str(f)) == f.resolve()


# --- default_kimi_code_home ----------------------------------------------

def test_default_kimi_code_home_layout(tmp_path):
    state_root = tmp_path / "state"
    home = default_kimi_code_home(state_root, "work")
    assert home == (state_root / "profiles" / "work" / "kimi-code-home").resolve()


def test_default_kimi_code_home_rejects_slash_profile(tmp_path):
    with pytest.raises(InvalidPathError):
        default_kimi_code_home(tmp_path, "a/b")
