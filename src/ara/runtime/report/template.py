"""WeaponReportTemplate schema + default offsec template.

A report template is a list of sections. Each section is either:

- ``static`` — rendered from a template_src string against the
  pre-built top-level context.
- ``finding_loop`` — rendered once per signed-off finding, with
  ``finding`` bound to the current finding dict.
- ``auto`` — section content is generated programmatically by a
  built-in section auto-generator (executive_summary,
  coverage_omissions, etc.).

v1 ships the offsec default below; v1.5 will add pack-supplied
templates extracted from example-report mats (1.4 §4.4).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


SectionKind = Literal["static", "finding_loop", "auto"]


@dataclass(frozen=True)
class SectionSpec:
    name: str
    kind: SectionKind
    template_src: str = ""           # for static + finding_loop
    auto_generator: str = ""         # for auto
    title: str = ""
    optional: bool = False           # if True, empty content is fine


@dataclass(frozen=True)
class WeaponReportTemplate:
    name: str
    version: str
    description: str
    sections: list[SectionSpec] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "sections": [asdict(s) for s in self.sections],
        }

    @classmethod
    def from_jsonable(cls, d: dict) -> "WeaponReportTemplate":
        return cls(
            name=d["name"], version=d.get("version", "1"),
            description=d.get("description", ""),
            sections=[SectionSpec(**s) for s in d.get("sections") or []],
        )


def write(path: Path, tpl: WeaponReportTemplate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tpl.to_jsonable(), sort_keys=True, indent=2))


def read(path: Path) -> WeaponReportTemplate:
    return WeaponReportTemplate.from_jsonable(json.loads(path.read_text()))


# ---------- offsec default ----------

_OFFSEC_FINDING_BLOCK = """\
### {{ finding.title }}

- **ID:** `{{ finding.id }}`
- **Category:** `{{ finding.category }}`
- **Severity:** `{{ finding.severity_name }}` (score {{ finding.severity_score }})
- **Affected assets:** {{ finding.affected_assets }}

**Impact**

- Confidentiality: {{ finding.impact.confidentiality }}
- Integrity: {{ finding.impact.integrity }}
- Availability: {{ finding.impact.availability }}

**Provenance**

{% for ref in finding.provenance_chain %}- `{{ ref }}`
{% endfor %}

**Evidence**

{% for ev in finding.evidence_refs %}- `{{ ev }}`
{% endfor %}

**Remediation**

{% if finding.fix_template_id %}Apply fix template `{{ finding.fix_template_id }}`.
{% endif %}{% if finding.fix_custom %}{{ finding.fix_custom.what_changed }}

Why it fixes: {{ finding.fix_custom.why_it_fixes }}

Verification: {{ finding.fix_custom.verification_method }}
{% endif %}

---
"""


def offsec_default() -> WeaponReportTemplate:
    return WeaponReportTemplate(
        name="offsec_default",
        version="1",
        description="v1 default offsec report template.",
        sections=[
            SectionSpec(
                name="header",
                kind="static",
                title="",
                template_src=(
                    "# Engagement report — {{ engagement.id }}\n\n"
                    "- Weapon: `{{ weapon.id }}`\n"
                    "- Rendered: {{ rendered_at_iso }}\n"
                    "- Findings: {{ findings_count }} signed off "
                    "({{ severity_dist.critical }} critical, "
                    "{{ severity_dist.high }} high, "
                    "{{ severity_dist.medium }} medium, "
                    "{{ severity_dist.low }} low, "
                    "{{ severity_dist.info }} info)\n\n"
                ),
            ),
            SectionSpec(
                name="executive_summary",
                kind="auto",
                auto_generator="executive_summary",
                title="Executive summary",
            ),
            SectionSpec(
                name="findings",
                kind="finding_loop",
                title="Findings",
                template_src=_OFFSEC_FINDING_BLOCK,
            ),
            SectionSpec(
                name="methodology",
                kind="auto",
                auto_generator="methodology",
                title="Methodology",
                optional=True,
            ),
            SectionSpec(
                name="coverage",
                kind="auto",
                auto_generator="coverage_omissions",
                title="Coverage and omissions",
                optional=True,
            ),
            SectionSpec(
                name="appendix_evidence",
                kind="auto",
                auto_generator="appendix_evidence",
                title="Appendix — evidence index",
                optional=True,
            ),
        ],
    )
