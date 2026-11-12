"""Skills: one line in the prompt, full instructions only when loaded.

Copied from dsh's `skill` + `tool-skill` pair and the single-agent-loop-kit's
`load_skill`, both because it is small and because it fixes a measured cost:
instruction text is re-sent on every request, so a 13,000-character schema and a
7,000-character instruction block were being paid for by requests that used
neither.

A skill is a directory with a ``SKILL.md`` whose frontmatter carries ``name`` and
``description``. The catalog costs one line per skill. Loading one appends its
body to the conversation as a user message, so it compacts and replays like any
other content rather than hiding outside the log.
"""
from __future__ import annotations

import re
from pathlib import Path

_FRONTMATTER = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n(?P<rest>.*)$", re.S)
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillError(RuntimeError):
    pass


class Skill:
    def __init__(self, name, description, body, path):
        self.name = name
        self.description = description
        self.body = body
        self.path = path

    def catalog_line(self):
        return "- %s: %s" % (self.name, self.description)


def _parse(path):
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if not match:
        raise SkillError("SKILL_FRONTMATTER_MISSING:" + str(path))
    fields = {}
    for line in match.group("body").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillError("SKILL_FRONTMATTER_LINE_INVALID:%s:%s" % (path, line))
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    name = fields.get("name")
    description = fields.get("description")
    if not name or not _NAME.match(name):
        raise SkillError("SKILL_NAME_INVALID:" + str(path))
    if not description:
        raise SkillError("SKILL_DESCRIPTION_REQUIRED:" + str(path))
    return Skill(name, description, match.group("rest").strip(), path)


class SkillRegistry:
    """Directory-backed registry. Duplicate names are rejected, not merged."""

    def __init__(self, directories=()):
        self.skills = {}
        self.directories = [Path(d) for d in directories]
        for directory in self.directories:
            self.discover(directory)

    def discover(self, directory):
        directory = Path(directory)
        if not directory.is_dir():
            return 0
        found = 0
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            manifest = child / "SKILL.md"
            if not manifest.is_file():
                continue
            skill = _parse(manifest)
            if skill.name in self.skills:
                raise SkillError("SKILL_NAME_DUPLICATE:" + skill.name)
            self.skills[skill.name] = skill
            found += 1
        return found

    def catalog(self):
        return "\n".join(skill.catalog_line()
                         for skill in sorted(self.skills.values(), key=lambda s: s.name))

    def names(self):
        return sorted(self.skills)

    def load(self, name):
        skill = self.skills.get(name)
        if skill is None:
            raise SkillError(
                "SKILL_UNKNOWN: %r. Available: %s"
                % (name, ", ".join(self.names()) or "(none)"))
        return skill