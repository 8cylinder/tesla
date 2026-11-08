import shlex
import stat
import subprocess
from pathlib import Path

import click
from click.testing import CliRunner

from tesla.cli import cli
from tesla.completions import (
    completion_path,
    generate_bash,
    install_bash_completion,
)


def make_group():
    @click.group()
    def app():
        """Test app."""

    @app.command()
    @click.option("--mode", type=click.Choice(["fast", "slow"]))
    @click.option("--path", type=click.Path(exists=False))
    @click.option(
        "--where",
        type=click.Path(exists=False, file_okay=False, dir_okay=True),
    )
    @click.option("--infile", type=click.File())
    @click.option("--when", type=click.DateTime())
    @click.option("--hidden", hidden=True)
    @click.option("--tag", multiple=True)
    @click.option("-v", "--verbose", count=True)
    @click.option(
        "--pair",
        nargs=2,
        type=click.Tuple([click.Choice(["a", "b"]), click.Path(exists=False)]),
    )
    @click.argument("kind", type=click.Choice(["one", "two"]))
    def run(
        mode,
        path,
        where,
        infile,
        when,
        hidden,
        tag,
        verbose,
        pair,
        kind,
    ):
        """Run the test command."""

    @app.command(hidden=True)
    def secret():
        """Hidden command."""

    @app.command()
    @click.argument("paths", nargs=-1, type=click.Path(exists=False))
    def many(paths):
        """Variadic paths."""

    return app


def write_script(tmp_path: Path) -> Path:
    script = tmp_path / "app.bash"
    script.write_text(generate_bash(make_group(), "app", "1.2.3"))
    return script


def complete(script: Path, words, cwd: Path | None = None):
    joined = " ".join(shlex.quote(word) for word in words)
    code = f"""
source "$1"
COMP_WORDS=({joined})
COMP_CWORD=$(( ${{#COMP_WORDS[@]}} - 1 ))
COMPREPLY=()
_tesla_complete
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    proc = subprocess.run(
        ["bash", "-c", code, "bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    return [line for line in proc.stdout.splitlines() if line]


def test_destination_prefers_bash_completion_user_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BASH_COMPLETION_USER_DIR", "/first:/second")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    expected = Path("/first/bash-completion/completions/tesla.bash")
    assert completion_path("tesla") == expected


def test_destination_uses_first_nonempty_user_dir(monkeypatch):
    monkeypatch.setenv("BASH_COMPLETION_USER_DIR", ":/first:/second")
    assert completion_path("tesla") == Path(
        "/first/bash-completion/completions/tesla.bash"
    )


def test_destination_falls_back_from_empty_user_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BASH_COMPLETION_USER_DIR", ":")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert completion_path("tesla") == (
        tmp_path / "xdg" / "bash-completion" / "completions" / "tesla.bash"
    )


def test_destination_uses_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("BASH_COMPLETION_USER_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert completion_path("tesla") == (
        tmp_path / "xdg" / "bash-completion" / "completions" / "tesla.bash"
    )


def test_destination_uses_home(monkeypatch, tmp_path):
    monkeypatch.delenv("BASH_COMPLETION_USER_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert completion_path("tesla") == (
        tmp_path
        / ".local"
        / "share"
        / "bash-completion"
        / "completions"
        / "tesla.bash"
    )


def test_install_creates_parents_and_overwrites(monkeypatch, tmp_path):
    monkeypatch.delenv("BASH_COMPLETION_USER_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    path = install_bash_completion(make_group(), "app", "1.2.3")
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.write_text("old")
    install_bash_completion(make_group(), "app", "1.2.3")
    assert "do not edit" in path.read_text()


def test_install_prints_path_to_stdout(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("BASH_COMPLETION_USER_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    path = install_bash_completion(make_group(), "app", "1.2.3")
    captured = capsys.readouterr()
    assert captured.out.strip() == str(path)
    assert "source" in captured.err


def test_script_has_header_and_no_python(tmp_path):
    text = generate_bash(make_group(), "app", "1.2.3")
    assert "# bash completion for app 1.2.3" in text
    assert "do not edit" in text
    assert "python" not in text.lower()


def test_script_passes_bash_syntax_check(tmp_path):
    script = write_script(tmp_path)
    proc = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_subcommand_completion(tmp_path):
    replies = complete(write_script(tmp_path), ["app", ""])
    assert "run" in replies
    assert "many" in replies
    assert "secret" not in replies


def test_option_and_short_option_completion(tmp_path):
    replies = complete(write_script(tmp_path), ["app", "run", "-"])
    assert "--help" in replies
    assert "--mode" in replies
    assert "-v" in replies
    assert "--hidden" not in replies


def test_choice_completion(tmp_path):
    replies = complete(write_script(tmp_path), ["app", "run", "--mode", ""])
    assert replies == ["fast", "slow"]


def test_choice_equals_completion(tmp_path):
    replies = complete(write_script(tmp_path), ["app", "run", "--mode=f"])
    assert replies == ["--mode=fast"]
    replies = complete(write_script(tmp_path), ["app", "run", "--mode="])
    assert replies == ["--mode=fast", "--mode=slow"]


def test_path_completion(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / "folder").mkdir()
    replies = complete(
        write_script(tmp_path),
        ["app", "run", "--path", ""],
        cwd=tmp_path,
    )
    assert "file.txt" in replies
    assert "folder/" in replies


def test_directory_completion(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    (tmp_path / "folder").mkdir()
    replies = complete(
        write_script(tmp_path),
        ["app", "run", "--where", ""],
        cwd=tmp_path,
    )
    assert "folder/" in replies
    assert "file.txt" not in replies


def test_file_completion(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    replies = complete(
        write_script(tmp_path),
        ["app", "run", "--infile", ""],
        cwd=tmp_path,
    )
    assert "file.txt" in replies


def test_datetime_completion_has_no_suggestions(tmp_path):
    replies = complete(write_script(tmp_path), ["app", "run", "--when", ""])
    assert replies == []


def test_tuple_option_completion(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    script = write_script(tmp_path)
    assert complete(script, ["app", "run", "--pair", ""]) == ["a", "b"]
    replies = complete(script, ["app", "run", "--pair", "a", ""], cwd=tmp_path)
    assert "file.txt" in replies


def test_variadic_argument_completion(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    replies = complete(
        write_script(tmp_path), ["app", "many", ""], cwd=tmp_path
    )
    assert "file.txt" in replies


def test_no_option_completion_after_double_dash(tmp_path):
    replies = complete(write_script(tmp_path), ["app", "--", ""])
    assert "run" in replies
    assert "--help" not in replies


def test_hidden_command_and_option_are_excluded(tmp_path):
    text = generate_bash(make_group(), "app", "1.2.3")
    assert "secret" not in text
    assert "--hidden" not in text


def test_old_bash_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BASH_COMPLETION_USER_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("tesla.completions._bash_version", lambda: (3, 2))
    install_bash_completion(make_group(), "app", "1.2.3")
    captured = capsys.readouterr()
    assert "older than 4.4" in captured.err


def test_missing_bash_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BASH_COMPLETION_USER_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("tesla.completions._bash_version", lambda: None)
    install_bash_completion(make_group(), "app", "1.2.3")
    captured = capsys.readouterr()
    assert "bash was not found" in captured.err


def test_cli_completions(monkeypatch, tmp_path):
    monkeypatch.delenv("BASH_COMPLETION_USER_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["--completions"])
    assert result.exit_code == 0
    path = Path(result.stdout.strip())
    assert path.exists()
    assert "source" in result.stderr
