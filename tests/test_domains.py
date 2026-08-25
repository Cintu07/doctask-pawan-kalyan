"""A new industry is a data change, not a rewrite.

The brief lists "configuration over code, a new rule, court, client or format
should be a data change, not a rewrite" as one of the things that separates
strong work. This file is the proof rather than the claim: the same pipeline,
the same graph, the same store and the same review gate read Telangana land
records because a second json file exists.

No module in `src/analyst` mentions land, survey numbers or pattadar passbooks.
"""

from __future__ import annotations

import pathlib

import pytest

from analyst.domain import DomainError, available_domains, load_domain
from analyst.extract import extract_with_rules, use_domain
from analyst.ingest import ingest_directory, ingest_file
from analyst.service import Analyst

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def land_corpus(tmp_path):
    from make_land_corpus import build

    folder = tmp_path / "kondapur"
    build(folder)
    # The assigned parcel is a different property, so it is a different file.
    (folder / "07-passbook-survey-156-2.txt").unlink(missing_ok=True)
    return folder


@pytest.fixture
def land_playbook() -> pathlib.Path:
    return ROOT / "playbooks" / "land-title-diligence.json"


@pytest.fixture(autouse=True)
def _restore_domain():
    """Every test leaves the default domain in force for the next one."""
    yield
    use_domain("vendor-contracts")


class TestThePackIsTheConfiguration:
    def test_both_industries_ship(self):
        assert {"vendor-contracts", "land-title"} <= set(available_domains())

    def test_no_module_hardcodes_an_industry(self):
        """The engine must not know what a survey number is."""
        offenders = []
        for path in (ROOT / "src" / "analyst").glob("*.py"):
            if path.name == "domain.py":
                continue  # the loader may name the packs in its docstring
            body = path.read_text(encoding="utf-8").lower()
            for word in ("pattadar", "survey_number", "liability_cap", "payment_terms_days"):
                if word in body:
                    offenders.append(f"{path.name} mentions {word}")
        assert not offenders, offenders

    def test_a_malformed_pack_is_refused(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('{"name": "x", "kinds": [], "subjects": [], "rules": [1]}', encoding="utf-8")
        with pytest.raises(DomainError):
            load_domain(bad)

    def test_a_pattern_without_a_value_group_is_refused(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"name":"x","kinds":[],"subjects":[{"key":"a","label":"A","unit":"text"}],'
            '"rules":[{"subject":"a","patterns":["no capture here"]}]}',
            encoding="utf-8",
        )
        with pytest.raises(DomainError, match=r"\(\?P<v>"):
            load_domain(bad)

    def test_a_rule_for_an_unknown_subject_is_refused(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"name":"x","kinds":[],"subjects":[{"key":"a","label":"A","unit":"text"}],'
            '"rules":[{"subject":"ghost","patterns":["(?P<v>x)"]}]}',
            encoding="utf-8",
        )
        with pytest.raises(DomainError, match="unknown subject"):
            load_domain(bad)


class TestLandTitleWorksOnTheSameEngine:
    def test_land_documents_are_classified(self, land_corpus):
        use_domain("land-title")
        documents, problems = ingest_directory(land_corpus)
        assert not problems
        kinds = {d.name: d.kind for d in documents}
        assert kinds["01-sale-deed-1998.txt"] == "sale_deed"
        assert kinds["03-pattadar-passbook-2015.txt"] == "pattadar_passbook"
        assert kinds["05-encumbrance-certificate-2024.txt"] == "encumbrance"
        assert kinds["04-dharani-mutation-2021.txt"] == "mutation"

    def test_the_extent_disagreement_is_found(self, land_corpus, tmp_path, land_playbook):
        """The disagreement that actually sends families to court."""
        analyst = Analyst(data_dir=tmp_path / "d")
        proposal = analyst.analyse(
            corpus_id="land", source_dir=land_corpus,
            playbook=land_playbook, domain="land-title",
        )
        extent = next(c for c in proposal.state.conflicts if c.subject == "extent_acres")
        values = sorted(str(c.value) for c in extent.claims)
        assert values == ["3.85", "4.00", "4.20"]

    def test_the_holder_disagreement_is_found(self, land_corpus, tmp_path, land_playbook):
        analyst = Analyst(data_dir=tmp_path / "d")
        proposal = analyst.analyse(
            corpus_id="land", source_dir=land_corpus,
            playbook=land_playbook, domain="land-title",
        )
        holder = next(c for c in proposal.state.conflicts if c.subject == "owner_name")
        assert len(holder.claims) >= 2

    def test_a_subsisting_mortgage_is_critical(self, land_corpus, tmp_path, land_playbook):
        analyst = Analyst(data_dir=tmp_path / "d")
        proposal = analyst.analyse(
            corpus_id="land", source_dir=land_corpus,
            playbook=land_playbook, domain="land-title",
        )
        enc = [f for f in proposal.state.findings if f.rule_id == "ENC-01"]
        assert enc and enc[0].severity == "critical"

    def test_assigned_land_is_caught(self, tmp_path, land_playbook):
        """The finding that saves a buyer their whole purchase price.

        Assigned land cannot be transferred at all. A buyer who misses that one
        line in the passbook loses the land and the money.
        """
        from make_land_corpus import ASSIGNED_LAND

        folder = tmp_path / "assigned"
        folder.mkdir()
        (folder / "passbook.txt").write_text(ASSIGNED_LAND, encoding="utf-8")

        analyst = Analyst(data_dir=tmp_path / "d")
        proposal = analyst.analyse(
            corpus_id="assigned", source_dir=folder,
            playbook=land_playbook, domain="land-title",
        )
        cls = [f for f in proposal.state.findings if f.rule_id == "CLS-01"]
        assert cls and cls[0].severity == "critical"

    def test_ordinary_patta_land_does_not_trip_the_rule(self, land_corpus, tmp_path, land_playbook):
        analyst = Analyst(data_dir=tmp_path / "d")
        proposal = analyst.analyse(
            corpus_id="land", source_dir=land_corpus,
            playbook=land_playbook, domain="land-title",
        )
        assert not [f for f in proposal.state.findings if f.rule_id == "CLS-01"]

    def test_the_broker_note_is_quarantined(self, tmp_path, land_playbook):
        """A broker asking the system not to report the mortgage."""
        from make_land_corpus import build

        folder = tmp_path / "full"
        build(folder)
        analyst = Analyst(data_dir=tmp_path / "d")
        proposal = analyst.analyse(
            corpus_id="full", source_dir=folder,
            playbook=land_playbook, domain="land-title",
        )
        assert any("quarantined" in n for n in proposal.notes)
        # And the mortgage it asked us to hide is still reported.
        assert [f for f in proposal.state.findings if f.rule_id == "ENC-01"]

    def test_every_land_row_with_a_value_cites_a_document(
        self, land_corpus, tmp_path, land_playbook
    ):
        analyst = Analyst(data_dir=tmp_path / "d")
        proposal = analyst.analyse(
            corpus_id="land", source_dir=land_corpus,
            playbook=land_playbook, domain="land-title",
        )
        for row in proposal.state.rows:
            if row.value != "not stated":
                assert row.citations, f"{row.subject} states a value with no source"

    def test_a_boundary_description_is_not_read_as_the_village(self, land_corpus):
        """Regression: "South: village cart track" was extracted as the village."""
        use_domain("land-title")
        deed = ingest_file(land_corpus / "01-sale-deed-1998.txt")
        village = [c for c in extract_with_rules(deed) if c.subject == "village"]
        assert village and village[0].value == "Kondapur"


class TestSwitchingBackIsClean:
    def test_vendor_contracts_still_work_after_a_land_run(
        self, land_corpus, corpus, tmp_path, playbook_path, land_playbook
    ):
        """The packs must not leak into each other."""
        analyst = Analyst(data_dir=tmp_path / "d")
        analyst.analyse(
            corpus_id="land", source_dir=land_corpus,
            playbook=land_playbook, domain="land-title",
        )
        proposal = analyst.analyse(
            corpus_id="vendor", source_dir=corpus,
            playbook=playbook_path, domain="vendor-contracts",
        )
        subjects = {r.subject for r in proposal.state.rows}
        assert "payment_terms_days" in subjects
        assert "survey_number" not in subjects
