from ara.forge.pack_draft import build_pack_draft
def test_detection_accepts_keyword_list():
    events = [
        {"channel": "propose.detection", "manifest_element_id": "m1",
         "body": {"report_section_keywords": ["finding", "vulnerability", ""]}},
        {"channel": "propose.detection", "manifest_element_id": "m1",
         "body": {"keyword": "risk"}},
    ]
    d = build_pack_draft(events, "p")
    kws = [i["data"]["keyword"] for i in d.items_for("detection")]
    assert kws == ["finding", "vulnerability", "risk"]
