"""Settings that restrict what the commands may do, for a host that runs agents unattended.

They come from TOML files and environment variables, never from command-line options, so that an agent cannot loosen
them on its own. Every source is read, and a restriction applies when any source sets it: a less restrictive value
elsewhere never loosens it. The sources:

- the system file: `/etc/glab-cli-extras/config.toml`, on Windows `%PROGRAMDATA%\\glab-cli-extras\\config.toml`;
- the user file: `$XDG_CONFIG_HOME/glab-cli-extras/config.toml` (default `~/.config`, macOS included), on Windows
  `%APPDATA%\\glab-cli-extras\\config.toml`;
- the file `GLAB_CLI_EXTRAS_CONFIG` names, which must exist;
- one environment variable per setting, `GLAB_CLI_EXTRAS_<SECTION>_<KEY>`, such as
  `GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS=reject`.

The files are strict: an unknown table, key or value is an error, so that a typo never leaves a restriction off.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP = "glab-cli-extras"
CONFIG_FILE = "config.toml"
CONFIG_ENV = "GLAB_CLI_EXTRAS_CONFIG"


class ConfigError(Exception):
    """A config file or a settings variable cannot be used. The message names the source."""


@dataclass(frozen=True)
class Setting:
    section: str
    key: str
    values: tuple[str, ...]
    """The allowed values, from the least to the most restrictive. The first one is the default."""

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"

    @property
    def env_var(self) -> str:
        return f"GLAB_CLI_EXTRAS_{self.section}_{self.key}".upper()

    @property
    def default(self) -> str:
        return self.values[0]


QUICK_ACTIONS = Setting("discussion", "quick_actions", ("allow", "reject"))
DRAFTS = Setting("discussion", "drafts", ("enabled", "disabled"))
SETTINGS: tuple[Setting, ...] = (QUICK_ACTIONS, DRAFTS)


@dataclass(frozen=True)
class Settings:
    """The value of each setting, with the source that set it, or the default."""

    values: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)

    def get(self, setting: Setting) -> str:
        return self.values.get(setting.name, (setting.default, None))[0]

    def source(self, setting: Setting) -> str | None:
        """Where the value came from: a file path or an environment variable, None for the default."""
        return self.values.get(setting.name, (setting.default, None))[1]

    def describe(self, setting: Setting) -> str:
        """`discussion.quick_actions = "reject" (from <source>)`, for an error message."""
        source = self.source(setting)
        return f'{setting.name} = "{self.get(setting)}"' + (f" (from {source})" if source else "")

    @property
    def quick_actions_rejected(self) -> bool:
        return self.get(QUICK_ACTIONS) == "reject"

    @property
    def drafts_disabled(self) -> bool:
        return self.get(DRAFTS) == "disabled"


def system_config_file() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA") or "C:\\ProgramData") / APP / CONFIG_FILE
    return Path("/etc") / APP / CONFIG_FILE


def user_config_file() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        # The XDG spec says to ignore a relative path.
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / APP / CONFIG_FILE


def _check_value(setting: Setting, value: Any, source: str) -> str:
    if not isinstance(value, str) or value not in setting.values:
        allowed = " or ".join(f'"{v}"' for v in setting.values)
        raise ConfigError(f"{source}: {setting.name} is {value!r}; set it to {allowed}")
    return value


def _read_file(path: Path) -> dict[str, str]:
    """The settings one file sets, by name. Every table, key and value must be known."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    known = {(s.section, s.key): s for s in SETTINGS}
    sections = sorted({s.section for s in SETTINGS})
    found: dict[str, str] = {}
    for section, table in data.items():
        if section not in sections or not isinstance(table, dict):
            raise ConfigError(f"{path}: unknown setting {section!r}; the known tables are {', '.join(sections)}")
        for key, value in table.items():
            setting = known.get((section, key))
            if setting is None:
                names = ", ".join(s.key for s in SETTINGS if s.section == section)
                raise ConfigError(f"{path}: unknown setting {section}.{key}; [{section}] knows {names}")
            found[setting.name] = _check_value(setting, value, str(path))
    return found


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read every source, and keep the most restrictive value of each setting."""
    env = os.environ if env is None else env
    sources: list[tuple[str, dict[str, str]]] = []
    for path in (system_config_file(), user_config_file()):
        if path.is_file():
            sources.append((str(path), _read_file(path)))
    explicit = env.get(CONFIG_ENV, "")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"{CONFIG_ENV} names {explicit}, which is not a file")
        sources.append((str(path), _read_file(path)))
    for setting in SETTINGS:
        value = env.get(setting.env_var, "")
        if value:
            sources.append((setting.env_var, {setting.name: _check_value(setting, value, setting.env_var)}))

    values: dict[str, tuple[str, str | None]] = {}
    for source, found in sources:
        for setting in SETTINGS:
            given = found.get(setting.name)
            if given is None:
                continue
            current = values.get(setting.name, (setting.default, None))[0]
            if setting.values.index(given) > setting.values.index(current):
                values[setting.name] = (given, source)
    return Settings(values)
