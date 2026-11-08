"""Static bash completion generation for the tesla CLI.

The generated script is self-contained: it never imports Python, runs the
app, or contacts the network while completing.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

Spec = tuple[str, ...]
NONE: Spec = ("none",)
FILE: Spec = ("file",)
DIR: Spec = ("dir",)


@dataclass
class _SpecRegistry:
    specs: list[Spec] = field(default_factory=list)
    indexes: dict[Spec, int] = field(default_factory=dict)

    def add(self, spec: Spec) -> int:
        if spec not in self.indexes:
            self.indexes[spec] = len(self.specs)
            self.specs.append(spec)
        return self.indexes[spec]


@dataclass
class _OptionInfo:
    names: list[str]
    arity: int
    specs: list[int]


@dataclass
class _ArgumentInfo:
    specs: list[int]
    variadic: bool = False


@dataclass
class _Node:
    node_id: int
    name: str
    command: click.Command
    options: list[_OptionInfo] = field(default_factory=list)
    arguments: list[_ArgumentInfo] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _spec_for_type(param_type: click.ParamType[Any]) -> Spec:
    if isinstance(param_type, click.Choice):
        return ("choices", *(str(choice) for choice in param_type.choices))
    if isinstance(param_type, click.Path):
        if not param_type.file_okay and param_type.dir_okay:
            return DIR
        return FILE
    if isinstance(param_type, click.File):
        return FILE
    return NONE


def _flatten_type(
    param_type: click.ParamType[Any],
    count: int,
) -> list[Spec]:
    if isinstance(param_type, click.Tuple):
        specs = [_spec_for_type(item) for item in param_type.types]
        if not specs:
            return [NONE] * count
        while len(specs) < count:
            specs.append(specs[-1])
        return specs[:count]
    return [_spec_for_type(param_type)] * count


def _option_arity(option: click.Option) -> int:
    if option.is_flag or option.count or option.nargs == 0:
        return 0
    if isinstance(option.nargs, int):
        return max(option.nargs, 1)
    return 1


def _collect_options(
    command: click.Command,
    registry: _SpecRegistry,
) -> list[_OptionInfo]:
    options: list[_OptionInfo] = []
    for param in command.params:
        if not isinstance(param, click.Option) or param.hidden:
            continue
        names = [*param.opts, *param.secondary_opts]
        arity = _option_arity(param)
        specs = [
            registry.add(spec)
            for spec in _flatten_type(param.type, max(arity, 1))
        ]
        options.append(_OptionInfo(names, arity, specs))
    help_names = command.context_settings.get("help_option_names", ["--help"])
    options.append(_OptionInfo(list(help_names), 0, []))
    return options


def _collect_arguments(
    command: click.Command,
    registry: _SpecRegistry,
) -> list[_ArgumentInfo]:
    arguments: list[_ArgumentInfo] = []
    for param in command.params:
        if not isinstance(param, click.Argument):
            continue
        if param.nargs == -1:
            spec = registry.add(_spec_for_type(param.type))
            arguments.append(_ArgumentInfo([spec], variadic=True))
            break
        count = param.nargs if isinstance(param.nargs, int) else 1
        specs = [
            registry.add(spec)
            for spec in _flatten_type(param.type, max(count, 1))
        ]
        arguments.append(_ArgumentInfo(specs))
    return arguments


def _walk(
    command: click.Command,
    name: str,
    parent: click.Context | None,
    registry: _SpecRegistry,
    counter: list[int],
) -> _Node:
    node = _Node(counter[0], name, command)
    counter[0] += 1
    node.options = _collect_options(command, registry)
    node.arguments = _collect_arguments(command, registry)
    if isinstance(command, click.Group):
        ctx = click.Context(command, parent=parent)
        for child_name in command.list_commands(ctx):
            child = command.get_command(ctx, child_name)
            if child is None or child.hidden:
                continue
            node.children.append(
                _walk(child, child_name, ctx, registry, counter)
            )
    return node


def _quote(value: str) -> str:
    return shlex.quote(value)


def _emit_helpers(prog: str) -> list[str]:
    return [
        "_tesla_add_choices() {",
        "    local cur=$1 prefix=$2",
        "    shift 2",
        "    local choice",
        '    for choice in "$@"; do',
        '        if [[ $choice == "$cur"* ]]; then',
        '            COMPREPLY+=("$prefix$choice")',
        "        fi",
        "    done",
        "}",
        "",
        "_tesla_add_files() {",
        "    local cur=$1 prefix=$2",
        "    if [[ -z $prefix ]] && declare -F _filedir >/dev/null; then",
        "        _filedir",
        "        return",
        "    fi",
        "    local line",
        "    while IFS= read -r line; do",
        "        if [[ -d $line && $line != */ ]]; then",
        '            line="$line/"',
        "        fi",
        '        COMPREPLY+=("$prefix$line")',
        '    done < <(compgen -f -- "$cur")',
        "}",
        "",
        "_tesla_add_dirs() {",
        "    local cur=$1 prefix=$2",
        "    if [[ -z $prefix ]] && declare -F _filedir >/dev/null; then",
        "        _filedir -d",
        "        return",
        "    fi",
        "    local line",
        "    while IFS= read -r line; do",
        "        if [[ $line != */ ]]; then",
        '            line="$line/"',
        "        fi",
        '        COMPREPLY+=("$prefix$line")',
        '    done < <(compgen -d -- "$cur")',
        "}",
        "",
    ]


def _emit_spec_functions(registry: _SpecRegistry) -> list[str]:
    lines: list[str] = []
    for spec_id, spec in enumerate(registry.specs):
        lines.append(f"_tesla_spec_{spec_id}() {{")
        lines.append("    local cur=$1 prefix=$2")
        kind = spec[0]
        if kind == "choices":
            choices = " ".join(_quote(choice) for choice in spec[1:])
            line = (
                f'    _tesla_add_choices "$cur" "$prefix" {choices}'
                if choices
                else "    :"
            )
            lines.append(line)
        elif kind == "file":
            lines.append('    _tesla_add_files "$cur" "$prefix"')
        elif kind == "dir":
            lines.append('    _tesla_add_dirs "$cur" "$prefix"')
        else:
            lines.append("    :")
        lines.append("}")
        lines.append("")
    return lines


def _emit_scan(
    node: _Node,
    options: list[_OptionInfo],
    indent: str = "    ",
) -> list[str]:
    lines: list[str] = []
    lines.append(f"{indent}if (( after_dd )); then")
    if node.children:
        lines.append(f"{indent}    if (( pos == 0 )); then")
        lines.append(f"{indent}        case $word in")
        for child in node.children:
            lines.append(
                f"{indent}            {_quote(child.name)}) "
                f'_tesla_cmd_{child.node_id} "$i"; return ;;'
            )
        lines.append(f"{indent}        esac")
        lines.append(f"{indent}    fi")
    lines.append(f"{indent}    ((pos++))")
    lines.append(f"{indent}    continue")
    lines.append(f"{indent}fi")
    lines.append(f'{indent}if [[ $word == "--" ]]; then')
    lines.append(f"{indent}    after_dd=1")
    lines.append(f"{indent}    continue")
    lines.append(f"{indent}fi")
    lines.append(f"{indent}if (( expect > 0 )); then")
    lines.append(f'{indent}    expect_types=("${{expect_types[@]:1}}")')
    lines.append(f"{indent}    ((expect--))")
    lines.append(f"{indent}    continue")
    lines.append(f"{indent}fi")
    lines.append(f"{indent}if [[ $word == --*=* ]]; then")
    lines.append(f"{indent}    continue")
    lines.append(f"{indent}fi")
    lines.append(f"{indent}if [[ $word == -* ]]; then")
    lines.append(f"{indent}    case $word in")
    for option in options:
        if option.arity == 0:
            continue
        patterns = "|".join(_quote(name) for name in option.names)
        specs = " ".join(str(spec) for spec in option.specs)
        lines.append(
            f"{indent}        {patterns}) "
            f"expect={option.arity}; expect_types=({specs}) ;;"
        )
    lines.append(f"{indent}    esac")
    lines.append(f"{indent}    continue")
    lines.append(f"{indent}fi")
    if node.children:
        lines.append(f"{indent}if (( pos == 0 )); then")
        lines.append(f"{indent}    case $word in")
        for child in node.children:
            lines.append(
                f"{indent}        {_quote(child.name)}) "
                f'_tesla_cmd_{child.node_id} "$i"; return ;;'
            )
        lines.append(f"{indent}    esac")
        lines.append(f"{indent}fi")
    lines.append(f"{indent}((pos++))")
    return lines


def _emit_options(
    node: _Node, options: list[_OptionInfo], indent: str
) -> list[str]:
    lines: list[str] = []
    names: list[str] = []
    for option in options:
        for name in option.names:
            if name not in names:
                names.append(name)
    if not names:
        return lines
    lines.append(f"{indent}local -a opts=(")
    for name in names:
        lines.append(f"{indent}    {_quote(name)}")
    lines.append(f"{indent})")
    lines.append(f"{indent}local opt")
    lines.append(f'{indent}for opt in "${{opts[@]}}"; do')
    lines.append(f'{indent}    if [[ $opt == "$cur"* ]]; then')
    lines.append(f'{indent}        COMPREPLY+=("$opt")')
    lines.append(f"{indent}    fi")
    lines.append(f"{indent}done")
    return lines


def _emit_positional(node: _Node, indent: str) -> list[str]:
    lines: list[str] = []
    if not node.arguments:
        return lines
    lines.append(f"{indent}case $pos in")
    position = 0
    variadic_spec: int | None = None
    for argument in node.arguments:
        if argument.variadic:
            variadic_spec = argument.specs[-1]
            break
        for spec in argument.specs:
            lines.append(f"{indent}    {position})")
            lines.append(f'{indent}        _tesla_spec_{spec} "$cur" ""')
            lines.append(f"{indent}        ;;")
            position += 1
    if variadic_spec is not None:
        lines.append(f"{indent}    *)")
        lines.append(f'{indent}        _tesla_spec_{variadic_spec} "$cur" ""')
        lines.append(f"{indent}        ;;")
    lines.append(f"{indent}esac")
    return lines


def _emit_command(node: _Node) -> list[str]:
    options = node.options
    lines = [
        f"_tesla_cmd_{node.node_id}() {{",
        "    local base=$1",
        "    local i word cur",
        "    local pos=0",
        "    local expect=0",
        "    local expect_types=()",
        "    local after_dd=0",
        "    cur=${COMP_WORDS[COMP_CWORD]}",
        "    for ((i=base+1; i<COMP_CWORD; i++)); do",
        "        word=${COMP_WORDS[i]}",
    ]
    lines += _emit_scan(node, options)
    lines += [
        "    done",
        '    if [[ $cur == "--" ]]; then',
        "        return",
        "    fi",
        "    if (( expect > 0 )); then",
        "        local spec=${expect_types[0]:-0}",
        '        local fn="_tesla_spec_$spec"',
        '        "$fn" "$cur" ""',
        "        return",
        "    fi",
        "    if (( after_dd == 0 )) && [[ $cur == --*=* ]]; then",
        "        local opt=${cur%%=*} val=${cur#*=}",
        "        case $opt in",
    ]
    for option in options:
        if option.arity == 0:
            continue
        patterns = "|".join(_quote(name) for name in option.names)
        spec = option.specs[0] if option.specs else 0
        lines.append(f"            {patterns})")
        lines.append(f'                _tesla_spec_{spec} "$val" "$opt="')
        lines.append("                compopt -o nospace")
        lines.append("                return")
        lines.append("                ;;")
    lines += [
        "        esac",
        "        return",
        "    fi",
        "    if (( after_dd == 0 )) && [[ $cur == -* ]]; then",
    ]
    lines += _emit_options(node, options, "        ")
    lines += [
        "        return",
        "    fi",
        "    if (( after_dd == 0 )); then",
    ]
    lines += _emit_options(node, options, "        ")
    lines += [
        "    fi",
    ]
    if node.children:
        lines.append("    if (( pos == 0 )); then")
        lines.append("        local -a subs=(")
        for child in node.children:
            lines.append(f"            {_quote(child.name)}")
        lines.append("        )")
        lines.append("        local sub")
        lines.append('        for sub in "${subs[@]}"; do')
        lines.append('            if [[ $sub == "$cur"* ]]; then')
        lines.append('                COMPREPLY+=("$sub")')
        lines.append("            fi")
        lines.append("        done")
        lines.append("    fi")
    lines += _emit_positional(node, "    ")
    lines.append("}")
    lines.append("")
    return lines


def _iter_nodes(node: _Node) -> list[_Node]:
    nodes = [node]
    for child in node.children:
        nodes.extend(_iter_nodes(child))
    return nodes


def generate_bash(
    group: click.Group,
    prog_name: str,
    version: str,
) -> str:
    """Return the complete bash completion script."""
    registry = _SpecRegistry()
    registry.add(NONE)
    root = _walk(group, prog_name, None, registry, [0])
    lines = [
        f"# bash completion for {prog_name} {version}",
        f"# Generated by {prog_name} --completions; do not edit.",
        "",
    ]
    lines += _emit_helpers(prog_name)
    lines += _emit_spec_functions(registry)
    for node in _iter_nodes(root):
        lines += _emit_command(node)
    lines += [
        "_tesla_complete() {",
        "    _tesla_cmd_0 0",
        "}",
        "",
        "complete -o nosort -o bashdefault -o default "
        f"-F _tesla_complete {_quote(prog_name)}",
        "",
    ]
    return "\n".join(lines)


def completion_path(prog_name: str) -> Path:
    """Resolve the bash-completion destination for ``prog_name``."""
    user_dir = os.environ.get("BASH_COMPLETION_USER_DIR", "")
    base: Path | None = None
    if user_dir:
        first = next((part for part in user_dir.split(os.pathsep) if part), "")
        if first:
            base = Path(first).expanduser()
    if base is None:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            base = Path(xdg).expanduser()
        else:
            base = Path("~/.local/share").expanduser()
    return (
        base.absolute()
        / "bash-completion"
        / "completions"
        / f"{prog_name}.bash"
    )


def _bash_version() -> tuple[int, int] | None:
    bash = shutil.which("bash")
    if bash is None:
        return None
    script = 'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'
    try:
        proc = subprocess.run(
            [bash, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        major, minor = proc.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except ValueError:
        return None


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _check_bash() -> None:
    version = _bash_version()
    if version is None:
        _warn("bash was not found; the script still requires bash >= 4.4")
        return
    if version < (4, 4):
        _warn(
            f"bash {version[0]}.{version[1]} is older than 4.4; "
            "the script needs bash >= 4.4"
        )
    if sys.platform == "darwin":
        _warn(
            "macOS /bin/bash is 3.2; install a newer bash "
            "(for example: brew install bash). zsh is not supported."
        )


def install_bash_completion(
    group: click.Group,
    prog_name: str,
    version: str,
) -> Path:
    """Write the script, print its path, and return the path."""
    script = generate_bash(group, prog_name, version)
    path = completion_path(prog_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{prog_name}-", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    print(path)
    print(
        f"To use it in this shell:\n  source {shlex.quote(str(path))}",
        file=sys.stderr,
    )
    _check_bash()
    return path
