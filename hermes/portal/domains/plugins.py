"""Plugins domain: what Hermes is extended with, read without running any of it.

Hermes' own rule is that plugins are discovered **without importing them** -- the loader
enumerates manifests, and model providers are found lazily precisely so they cannot be
instantiated twice.  This domain keeps to that rule strictly: it reads ``plugin.yaml``
files and directory listings, and never imports a plugin, never executes its code, and
never evaluates its manifest with a full YAML parser.  A plugin that would do something
on import cannot do it because a page was opened.

Sources, in the loader's own precedence order (later wins, so a user plugin can shadow
a bundled one):

* **bundled** -- ``<hermes root>/hermes-agent/plugins``: ten kind containers
  (``model-providers``, ``platforms``, ``web``, ``image_gen``, ``memory``, ...) plus
  top-level plugins;
* **user** -- ``<hermes root>/plugins`` and each profile's ``plugins/``.

Two things the page deliberately does not claim:

* **enablement.**  This config has no ``plugins.enabled`` list, so nothing is labelled
  enabled or disabled.  What it does have is ``known_plugin_toolsets`` per surface,
  which is a real signal and is shown as such -- a plugin named there is labelled
  "named by config", not "on".
* **secrets.**  41 manifests declare ``requires_env``.  Those are reported by *name*
  only: a plugin's requirements are useful, its credentials are not.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ...core import skill_trees
from ..model import (
    Collection,
    Count,
    Domain,
    Picker,
    Record,
    Source,
    build_collection,
)
from ..sources import (
    as_of,
    fmt_ago,
    hermes_root,
    human_size,
    path_source,
    read_text,
    snippet,
    truncate,
)

MANIFEST = "plugin.yaml"
MAX_DEPTH = 2
BODY_CAP = 8000
FILES_CAP = 60
PLUGINS_CAP = 400
DESCRIPTION_CHARS = 300
AGENT_DIR = "hermes-agent"

# Container directory -> the kind its plugins are, used only when a manifest does not
# declare one (12 of the 105 here).  The manifest always wins; the page says which.
CONTAINERS = {
    "model-providers": "model-provider",
    "platforms": "platform",
    "web": "backend",
    "memory": "memory-provider",
    "image_gen": "image-gen",
    "video_gen": "video-gen",
    "dashboard_auth": "dashboard-auth",
    "browser": "browser-backend",
    "cron_providers": "cron-provider",
    "observability": "observability",
}
LIST_KEYS = (
    "requires_env",
    "optional_env",
    "pip_dependencies",
    "provides_tools",
    "hooks",
)

_TOP_LEVEL = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
_LIST_ITEM = re.compile(r"^\s+-\s*(.+?)\s*$")
_INLINE_LIST = re.compile(r"^\[(.*)\]$")


@dataclass(frozen=True)
class PluginFile:
    """One file inside a plugin, by name and size (never by content)."""

    path: str
    size: int


@dataclass(frozen=True)
class Plugin:
    """One plugin, as its manifest and its directory describe it."""

    key: str
    name: str
    source: str
    container: str
    path: Path
    manifest: str = ""
    error: str = ""
    fields_seen: Mapping[str, str] = field(default_factory=dict)
    lists_seen: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    files: tuple[PluginFile, ...] = ()
    size: int = 0
    mtime: float | None = None

    def get(self, key: str, default: str = "") -> str:
        """A manifest value, trimmed."""
        return str(self.fields_seen.get(key, default)).strip()

    @property
    def label(self) -> str:
        """Display name: the manifest's label, else its name, else the directory."""
        return self.get("label") or self.get("name") or self.name

    @property
    def kind(self) -> str:
        """The manifest's kind, else the container's, else ``(none)``."""
        return self.get("kind") or CONTAINERS.get(self.container, "(none)")

    @property
    def kind_declared(self) -> bool:
        """``True`` when the manifest itself declared the kind."""
        return bool(self.get("kind"))

    @property
    def description(self) -> str:
        """One-line description (folded blocks are collapsed to one line)."""
        return " ".join(self.get("description").split())

    @property
    def requires_env(self) -> tuple[str, ...]:
        """Environment variable *names* the plugin needs (never values)."""
        return self.lists_seen.get("requires_env", ())

    @property
    def optional_env(self) -> tuple[str, ...]:
        """Environment variable names the plugin can use."""
        return self.lists_seen.get("optional_env", ())


@dataclass(frozen=True)
class _Snapshot:
    """Everything the domain read, once per process."""

    plugins: tuple[Plugin, ...] = ()
    manifestless: tuple[str, ...] = ()
    known_toolsets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    roots: tuple[tuple[str, Path], ...] = ()
    as_of: str = ""

    def by_key(self) -> dict[str, Plugin]:
        """Plugin key -> plugin."""
        return {plugin.key: plugin for plugin in self.plugins}

    def known_for(self, plugin: Plugin) -> tuple[str, ...]:
        """Which config surfaces name this plugin (by directory or manifest name)."""
        names = {plugin.name.lower(), plugin.get("name").lower()}
        return tuple(
            surface
            for surface, toolsets in self.known_toolsets.items()
            if names & {toolset.lower() for toolset in toolsets}
        )


def parse_manifest(text: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Read a ``plugin.yaml`` as text: top-level scalars and simple lists.

    A documented subset, not a YAML parser.  It handles the three shapes these
    manifests use -- ``key: value``, ``key:`` followed by ``- item`` lines, and the
    folded/literal blocks (``>``, ``|``) whose body is joined into one line -- and it
    ignores everything else, including nested mappings.  Nothing is executed and no
    value is interpreted beyond stripping quotes.
    """
    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    pending: str | None = None
    block: list[str] | None = None

    def flush_block() -> None:
        if pending is not None and block:
            scalars[pending] = " ".join(part.strip() for part in block if part.strip())

    for line in text.splitlines():
        if block is not None:
            if line.startswith((" ", "\t")):
                block.append(line)
                continue
            flush_block()
            block = None
        match = _TOP_LEVEL.match(line)
        if match and not line.startswith((" ", "\t")):
            key, value = match.group(1), match.group(2).strip()
            pending = None
            if value in (">", "|", ">-", "|-"):
                pending, block = key, []
            elif value:
                inline = _INLINE_LIST.match(value)
                if inline:
                    lists[key] = [
                        item.strip().strip("\"'") for item in inline.group(1).split(",")
                    ]
                else:
                    scalars[key] = value.strip("\"'")
            else:
                lists.setdefault(key, [])
            continue
        item = _LIST_ITEM.match(line)
        if item and lists:
            target = next(reversed(lists))
            lists[target].append(item.group(1).strip().strip("\"'"))
    flush_block()
    return scalars, {key: tuple(value) for key, value in lists.items() if value}


def _container_for(plugin_dir: Path) -> str:
    """The kind container a plugin sits in, or ``""`` when it sits at the top level.

    ``plugins/platforms/a2a`` is in the ``platforms`` container; ``plugins/kanban`` is
    not in one.  Getting this backwards would classify every container plugin as
    top-level and lose its kind.
    """
    return plugin_dir.parts[0] if len(plugin_dir.parts) >= 2 else ""


def _read_plugin(manifest: Path, root: Path, source: str) -> Plugin:
    """Read one plugin directory: its manifest and its file list."""
    plugin_dir = manifest.parent
    relative = plugin_dir.relative_to(root)
    name = plugin_dir.name
    container = _container_for(relative)
    text, _truncated, error = read_text(manifest, limit=200_000)
    scalars, lists = ({}, {}) if error else parse_manifest(text)
    files: list[PluginFile] = []
    size = 0
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            file_size = path.stat().st_size
        except OSError:
            continue
        size += file_size
        files.append(PluginFile(path=str(path.relative_to(plugin_dir)), size=file_size))
    try:
        mtime = manifest.stat().st_mtime
    except OSError:
        mtime = None
    prefix = "" if source == "bundled" else f"{source}/"
    key = f"{prefix}{container}/{name}" if container else f"{prefix}{name}"
    return Plugin(
        key=key,
        name=name,
        source=source,
        container=container,
        path=plugin_dir,
        manifest=text,
        error=error,
        fields_seen=scalars,
        lists_seen=lists,
        files=tuple(files),
        size=size,
        mtime=mtime,
    )


def _discover(root: Path, source: str) -> tuple[list[Plugin], list[str]]:
    """Every plugin under *root*, and the directories that are not one."""
    if not root.is_dir():
        return [], []
    plugins: list[Plugin] = []
    for manifest in sorted(root.rglob(MANIFEST)):
        # depth is measured on the plugin's own directory: <container>/<name> is two,
        # and the manifest file itself must not count or every container plugin is lost
        try:
            plugin_dir = manifest.parent.relative_to(root)
        except ValueError:
            continue
        if len(plugin_dir.parts) > MAX_DEPTH or "__pycache__" in plugin_dir.parts:
            continue
        plugins.append(_read_plugin(manifest, root, source))
    manifestless: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "__")):
            continue
        if (entry / MANIFEST).is_file():
            continue
        # a container holds plugins (or helper modules) rather than being one
        if entry.name in CONTAINERS:
            continue
        manifestless.append(f"{source}/{entry.name}")
    return plugins, manifestless


def _known_toolsets(root: Path) -> tuple[dict[str, tuple[str, ...]], str]:
    """``known_plugin_toolsets`` from ``config.yaml``: surface -> toolset names."""
    text, _truncated, error = read_text(root / "config.yaml", limit=400_000)
    if error:
        return {}, f"could not read config.yaml: {error}"
    lines = text.splitlines()
    found: dict[str, tuple[str, ...]] = {}
    inside = False
    surface: str | None = None
    for line in lines:
        if re.match(r"^known_plugin_toolsets\s*:", line):
            inside = True
            continue
        if inside:
            if line and not line.startswith((" ", "\t")):
                break
            match = re.match(r"^\s{2,}([A-Za-z0-9_]+)\s*:\s*$", line)
            if match:
                name = match.group(1)
                found.setdefault(name, ())
                surface = name
                continue
            item = _LIST_ITEM.match(line)
            if item and surface:
                found[surface] = (*found[surface], item.group(1).strip().strip("\"'"))
    return found, ""


def build_domain(
    hermes_home: Path | None = None, agent_dir: Path | None = None
) -> Domain:
    """Build the plugins domain.

    Args:
        hermes_home: Hermes home or profile directory.
        agent_dir: The agent installation holding the bundled ``plugins/`` tree;
            ``None`` uses ``<hermes root>/hermes-agent``.

    Returns:
        A :class:`~hermes.portal.model.Domain`.  Manifests and file lists are read once,
        on first use, and reused after.
    """
    state: dict[str, _Snapshot] = {}

    def roots() -> tuple[tuple[str, Path], ...]:
        root = hermes_root(hermes_home)
        install = Path(agent_dir) if agent_dir is not None else root / AGENT_DIR
        found: list[tuple[str, Path]] = [("bundled", install / "plugins")]
        found.append(("user", root / "plugins"))
        home = (
            Path(hermes_home)
            if hermes_home is not None
            else skill_trees.default_hermes_home()
        )
        for profile_dir in skill_trees.profile_dirs(home):
            found.append((f"user:{profile_dir.name}", profile_dir / "plugins"))
        return tuple(found)

    def snapshot() -> _Snapshot:
        if "value" in state:
            return state["value"]
        plugins: list[Plugin] = []
        manifestless: list[str] = []
        for source, directory in roots():
            found, loose = _discover(directory, source)
            plugins.extend(found)
            manifestless.extend(loose)
        known, _config_error = _known_toolsets(hermes_root(hermes_home))
        value = _Snapshot(
            plugins=tuple(plugins),
            manifestless=tuple(manifestless),
            known_toolsets=known,
            roots=roots(),
            as_of=as_of(),
        )
        state["value"] = value
        return value

    def sources(current: _Snapshot) -> tuple[Source, ...]:
        """Every root this domain looked in, present or not."""
        out: list[Source] = []
        for label, directory in current.roots[:4]:
            here = sum(
                1 for plugin in current.plugins if plugin.path.parent == directory
            )
            out.append(
                path_source(
                    f"{label} plugins",
                    directory,
                    note=f"{here} top-level plugin(s)" if here else "nothing here",
                )
            )
        return tuple(out)

    def _plugin_record(plugin: Plugin, current: _Snapshot) -> Record:
        """One plugin as a list row."""
        badges = [f"kind: {plugin.kind}", plugin.source]
        if not plugin.kind_declared:
            badges.append("kind inferred from its directory")
        named = current.known_for(plugin)
        if named:
            badges.append("named by config: " + ", ".join(named))
        if plugin.requires_env:
            badges.append(f"needs {len(plugin.requires_env)} env var(s)")
        if plugin.error:
            badges.append("manifest unreadable")
        version = plugin.get("version")
        return Record(
            id=plugin.key,
            title=plugin.label,
            subtitle=f"{plugin.kind} · {plugin.source}"
            + (f" · {version}" if version else "")
            + f" · {len(plugin.files)} file(s) · {human_size(plugin.size)}",
            badges=tuple(badges),
            fields=(
                ("plugin", plugin.name),
                ("kind", plugin.kind),
                ("source", plugin.source),
                ("container", plugin.container or "(top level)"),
                ("path", str(plugin.path)),
                ("files", str(len(plugin.files))),
                ("size", human_size(plugin.size)),
                ("manifest modified", fmt_ago(plugin.mtime)),
            ),
        )

    def _plugins_collection(current: _Snapshot) -> Collection:
        """Every discovered plugin, bundled first then user."""
        rows = sorted(
            current.plugins,
            key=lambda plugin: (plugin.source != "bundled", plugin.kind, plugin.name),
        )
        named = sum(1 for plugin in current.plugins if current.known_for(plugin))
        notes = [
            "a plugin is discovered from its plugin.yaml; nothing here is imported or "
            "executed",
            "the loader's precedence is later-wins, so a user plugin of the same name "
            "shadows the bundled one",
        ]
        if current.manifestless:
            notes.append(
                f"{len(current.manifestless)} director(y/ies) carry no {MANIFEST}"
                " and so are not plugins by that contract (they are listed below)"
            )
        if not current.plugins:
            notes.append(
                f"no {MANIFEST} found under any plugins directory; the bundled tree is "
                f"expected at <hermes root>/{AGENT_DIR}/plugins"
            )
        return build_collection(
            "plugins",
            "Plugins",
            "Every plugin the loader can discover, with the kind, source and files its "
            "manifest and directory describe.",
            f"directories holding a {MANIFEST}",
            [_plugin_record(plugin, current) for plugin in rows],
            cap=PLUGINS_CAP,
            sources=sources(current),
            picker=_picker(current),
            extra_counts=(
                Count(len(current.plugins), "plugins with a manifest"),
                Count(named, "named by config.yaml"),
                Count(
                    sum(1 for plugin in current.plugins if plugin.requires_env),
                    "plugins declaring required env vars",
                ),
                Count(len(current.manifestless), f"directories without a {MANIFEST}"),
            ),
            notes=tuple(notes),
            as_of=current.as_of,
        )

    def _picker(current: _Snapshot) -> Picker:
        """A dropdown over the kinds that are actually present."""
        counter = Counter(plugin.kind for plugin in current.plugins)
        return Picker(
            query_key="kind",
            label="Kind",
            options=tuple(
                (kind, f"{kind} ({count})") for kind, count in counter.most_common()
            ),
            all_label=f"All kinds ({len(current.plugins)})",
            selected="",
        )

    def _kinds_collection(current: _Snapshot) -> Collection:
        """One record per kind."""
        by_kind: dict[str, list[Plugin]] = {}
        for plugin in current.plugins:
            by_kind.setdefault(plugin.kind, []).append(plugin)
        records = []
        for kind, group in sorted(
            by_kind.items(), key=lambda row: (-len(row[1]), row[0])
        ):
            declared = sum(1 for plugin in group if plugin.kind_declared)
            total = sum(plugin.size for plugin in group)
            records.append(
                Record(
                    id=kind,
                    title=kind,
                    subtitle=f"{len(group)} plugin(s) · {human_size(total)}"
                    + ("" if declared == len(group) else f" · {declared} declare it"),
                    badges=(
                        f"{len(group)} plugins",
                        "declared in manifest"
                        if declared == len(group)
                        else "partly inferred",
                    ),
                    fields=(
                        ("kind", kind),
                        ("plugins", str(len(group))),
                        ("declared in manifest", str(declared)),
                        ("size", human_size(total)),
                        ("examples", ", ".join(p.name for p in group[:6])),
                    ),
                )
            )
        return build_collection(
            "kinds",
            "Kinds",
            "What sorts of plugin the tree holds, and how each kind declares itself.",
            "distinct plugin kinds (a manifest's kind, else its directory)",
            records,
            sources=sources(current),
            notes=(
                "where a manifest does not declare a kind it is inferred from the "
                "directory it sits in, and the plugin's row says so",
            ),
            as_of=current.as_of,
        )

    def _needs_collection(current: _Snapshot) -> Collection:
        """Plugins that declare requirements, by environment variable name only."""
        rows = [
            plugin
            for plugin in current.plugins
            if plugin.requires_env
            or plugin.optional_env
            or plugin.get("pip_dependencies")
        ]
        rows.sort(
            key=lambda plugin: (not plugin.requires_env, plugin.kind, plugin.name)
        )
        records = [
            Record(
                id=plugin.key,
                title=plugin.label,
                subtitle=f"{plugin.kind} · "
                + (
                    f"needs {', '.join(plugin.requires_env)}"
                    if plugin.requires_env
                    else ""
                )
                + (
                    f" · optional {', '.join(plugin.optional_env)}"
                    if plugin.optional_env
                    else ""
                ),
                badges=(
                    f"{len(plugin.requires_env)} required"
                    if plugin.requires_env
                    else "no required env",
                    f"{len(plugin.optional_env)} optional",
                )
                + (("pip deps",) if plugin.get("pip_dependencies") else ()),
                fields=(
                    ("requires", ", ".join(plugin.requires_env) or "\u2014"),
                    ("optional", ", ".join(plugin.optional_env) or "\u2014"),
                    (
                        "pip",
                        ", ".join(plugin.lists_seen.get("pip_dependencies", ()))
                        or "\u2014",
                    ),
                ),
            )
            for plugin in rows
        ]
        return build_collection(
            "needs",
            "What they need",
            "Plugins that declare environment variables or dependencies. Names only: "
            "the portal never reads a secret's value.",
            "plugins declaring requires_env, optional_env or pip_dependencies",
            records,
            cap=PLUGINS_CAP,
            sources=sources(current),
            as_of=current.as_of,
        )

    def collections(filters: Mapping[str, str] | None = None) -> Sequence[Collection]:
        """Drill-down collections.  ``?kind=`` narrows to one kind."""
        current = snapshot()
        wanted = ((filters or {}).get("kind") or "").strip()
        if wanted:
            current = _Snapshot(
                plugins=tuple(p for p in current.plugins if p.kind == wanted),
                manifestless=current.manifestless,
                known_toolsets=current.known_toolsets,
                roots=current.roots,
                as_of=current.as_of,
            )
        plugins = _plugins_collection(current)
        if wanted:
            everything = snapshot().plugins
            counter = Counter(plugin.kind for plugin in everything)
            hidden = len(everything) - len(current.plugins)
            # a filtered count has to say what it counts: the rule is no longer "every
            # plugin", it is "every plugin of this kind", and the whole is reported too
            plugins = build_collection(
                "plugins",
                "Plugins",
                plugins.description,
                f"plugins with a {MANIFEST} in kind {wanted!r}",
                list(plugins.records),
                cap=PLUGINS_CAP,
                sources=plugins.sources,
                picker=Picker(
                    query_key="kind",
                    label="Kind",
                    options=tuple(
                        (kind, f"{kind} ({count})")
                        for kind, count in counter.most_common()
                    ),
                    all_label=f"All kinds ({len(everything)})",
                    selected=wanted,
                ),
                extra_counts=(
                    *plugins.extra_counts,
                    Count(hidden, f"hidden by the kind filter (of {len(everything)})"),
                ),
                notes=plugins.notes
                + (() if current.plugins else (f"no plugin of kind {wanted!r}",)),
                as_of=plugins.as_of,
            )
        return [plugins, _kinds_collection(current), _needs_collection(current)]

    def overview() -> Collection:
        """Headline: how many plugins, of which kinds, and which config names."""
        current = snapshot()
        kinds = Counter(plugin.kind for plugin in current.plugins)
        named = [plugin for plugin in current.plugins if current.known_for(plugin)]
        bundled = sum(1 for plugin in current.plugins if plugin.source == "bundled")
        files = sum(len(plugin.files) for plugin in current.plugins)
        size = sum(plugin.size for plugin in current.plugins)
        notes = []
        if not current.plugins:
            notes.append(
                f"no {MANIFEST} found: the bundled tree is expected at <hermes root>/"
                f"{AGENT_DIR}/plugins"
            )
        if current.known_toolsets:
            described = ", ".join(
                f"{surface}: {', '.join(toolsets)}"
                for surface, toolsets in sorted(current.known_toolsets.items())
            )
            notes.append(
                f"config.yaml names plugin toolsets per surface -- {described}"
            )
        notes.append(
            "nothing on this page is enabled or disabled by the portal: only config "
            "names plugins, and no plugin is imported to find out"
        )
        return build_collection(
            "overview",
            "Plugins",
            "What Hermes is extended with: bundled and user plugins, their kinds, what "
            "they need, and which ones config.yaml names.",
            f"plugins with a {MANIFEST}",
            # every plugin is the record set -- the display cap decides how many show.
            # (Slicing here before build_collection is the count-vs-sample bug: the
            # headline then reads the size of the sample, not of the tree.)
            [
                _plugin_record(plugin, current)
                for plugin in sorted(
                    current.plugins,
                    key=lambda item: (
                        not current.known_for(item),
                        item.kind,
                        item.name,
                    ),
                )
            ],
            cap=5,
            sources=sources(current),
            extra_counts=(
                Count(len(current.plugins), "plugins with a manifest"),
                Count(len(kinds), "distinct kinds"),
                Count(bundled, "shipped with Hermes"),
                Count(len(current.plugins) - bundled, "user or profile plugins"),
                Count(files, "files inside those plugins"),
                Count(size, "bytes inside those plugins"),
            ),
            metrics=(
                ("Plugins", str(len(current.plugins))),
                ("Kinds", str(len(kinds))),
                ("Bundled", str(bundled)),
                ("Named by config", str(len(named))),
            ),
            notes=tuple(notes),
            as_of=current.as_of,
        )

    def detail(record_id: str) -> Record | None:
        """One plugin: its manifest, its directory, and what it declares."""
        current = snapshot()
        plugin = current.by_key().get(record_id)
        if plugin is None:
            return None
        named = current.known_for(plugin)
        badges = [f"kind: {plugin.kind}", plugin.source]
        if named:
            badges.append("named by config: " + ", ".join(named))
        if plugin.error:
            badges.append("manifest unreadable")
        return Record(
            id=plugin.key,
            title=plugin.label,
            subtitle=plugin.description or f"{plugin.kind} · {plugin.source}",
            badges=tuple(badges),
            fields=(
                ("directory", plugin.name),
                ("manifest name", plugin.get("name") or "\u2014"),
                ("kind", plugin.kind + ("" if plugin.kind_declared else " (inferred)")),
                ("version", plugin.get("version") or "\u2014"),
                ("author", plugin.get("author") or "\u2014"),
                ("source", plugin.source),
                ("container", plugin.container or "(top level)"),
                ("path", str(plugin.path)),
                ("files", str(len(plugin.files))),
                ("size", human_size(plugin.size)),
                ("declares", ", ".join(sorted(plugin.fields_seen)) or "\u2014"),
                ("requires env", ", ".join(plugin.requires_env) or "\u2014"),
                ("optional env", ", ".join(plugin.optional_env) or "\u2014"),
                ("hooks", ", ".join(plugin.lists_seen.get("hooks", ())) or "\u2014"),
                (
                    "provided tools",
                    ", ".join(plugin.lists_seen.get("provides_tools", ())) or "\u2014",
                ),
                ("modified", fmt_ago(plugin.mtime)),
            ),
            links=(("/plugins", "All plugins"),),
            body=truncate(plugin.manifest or "(no manifest text)", BODY_CAP),
        )

    def detail_sections(record_id: str) -> Sequence[Collection]:
        """Behind a plugin: its files, and its same-kind neighbours."""
        current = snapshot()
        plugin = current.by_key().get(record_id)
        if plugin is None:
            return []
        sections = [
            build_collection(
                "files",
                f"Files in {plugin.label}",
                "The plugin's own files, by size. Contents are not read: this page "
                "never imports or executes a plugin.",
                f"files under {plugin.path.name}",
                [
                    Record(
                        id=f"{plugin.key}/{item.path}",
                        title=item.path,
                        subtitle=human_size(item.size),
                        badges=(human_size(item.size),),
                    )
                    for item in plugin.files[:FILES_CAP]
                ],
                cap=FILES_CAP,
                as_of=current.as_of,
            )
        ]
        siblings = [
            other
            for other in current.plugins
            if other.kind == plugin.kind and other.key != plugin.key
        ]
        if siblings:
            sections.append(
                build_collection(
                    "same-kind",
                    f"Other {plugin.kind} plugins",
                    "The rest of this kind, so you can see what a slot could hold "
                    "instead.",
                    f"{plugin.kind} plugins other than this one",
                    [_plugin_record(other, current) for other in siblings[:20]],
                    cap=20,
                    as_of=current.as_of,
                )
            )
        if current.manifestless:
            sections.append(
                build_collection(
                    "manifestless",
                    f"Directories without a {MANIFEST}",
                    "The loader needs a manifest to treat a directory as a plugin; "
                    "these have none, so they are not discoverable as one.",
                    f"directories under the plugins roots with no {MANIFEST}",
                    [
                        Record(
                            id=f"manifestless/{name}",
                            title=name.split("/")[-1],
                            subtitle=name.rsplit("/", 1)[0],
                            badges=("no manifest",),
                        )
                        for name in current.manifestless
                    ],
                    as_of=current.as_of,
                )
            )
        return sections

    def search(query: str, limit: int) -> Sequence[Record]:
        """Find plugins by name, label, kind, author or description."""
        wanted = query.strip().lower()
        if not wanted:
            return []
        current = snapshot()
        hits: list[Record] = []
        for plugin in current.plugins:
            haystack = " ".join(
                (
                    plugin.name,
                    plugin.label,
                    plugin.kind,
                    plugin.get("author"),
                    plugin.description,
                    " ".join(plugin.requires_env),
                )
            ).lower()
            position = haystack.find(wanted)
            if position < 0:
                continue
            hits.append(
                Record(
                    id=plugin.key,
                    title=plugin.label,
                    subtitle=snippet(plugin.description, 120)
                    if plugin.description
                    else f"{plugin.kind} · {plugin.source}",
                    badges=(plugin.kind, plugin.source),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    return Domain(
        key="plugins",
        title="Plugins",
        summary="What Hermes is extended with, read from manifests without importing "
        "any of it.",
        overview=overview,
        collections=collections,
        detail=detail,
        search=search,
        detail_sections=detail_sections,
    )
